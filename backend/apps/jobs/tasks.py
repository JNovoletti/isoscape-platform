# apps/jobs/tasks.py

import subprocess
import json
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional

from celery import shared_task
from django.conf import settings


def _update_job(job, **fields):
    """Atalho para salvar campos específicos do Job."""
    for k, v in fields.items():
        setattr(job, k, v)
    job.save(update_fields=list(fields.keys()))


def _run_script(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a script (R or Python) and return result."""
    return subprocess.run(cmd, capture_output=True, text=True)


def _get_execution_command(script_type: str, job_id: int, job_config: dict,
                          output_dir: Path) -> Optional[list[str]]:
    """
    Build command to execute script based on EXECUTION_ENGINE setting.
    
    Args:
        script_type: "gen_rasters" or "run_isoscape"
        job_id: Job ID
        job_config: Job configuration dict
        output_dir: Output directory path
        
    Returns:
        Command list for subprocess.run(), or None if engine is 'r' (use old code)
    """
    engine = settings.EXECUTION_ENGINE.lower()
    
    if engine == "python":
        # Build Python command
        base_dir = Path(settings.BASE_DIR) / "python_scripts"
        
        if script_type == "gen_rasters":
            cmd = [
                sys.executable,
                str(base_dir / "gen_rasters.py"),
                "--job-id", str(job_id),
                "--shapefile", job_config["shapefile_path"],
                "--output-dir", str(output_dir),
                "--worldclim-dir", str(settings.DATA_WORLDCLIM_DIR),
                "--variables", ",".join(job_config["variables"]),
                "--resolution", str(job_config.get("resolution", "5")),
                "--skip-existing", str(job_config.get("skip_existing", True)).lower(),
            ]
            
            # Add bio-layers if specified
            if "bio" in job_config.get("variables", []) and job_config.get("bio_layers"):
                cmd += ["--bio-layers", ",".join(job_config["bio_layers"])]
        
        elif script_type == "run_isoscape":
            cmd = [
                sys.executable,
                str(base_dir / "run_isoscape.py"),
                "--job-id", str(job_id),
                "--dataset-path", job_config["dataset_path"],
                "--raster-dir", job_config["raster_dir"],
                "--output-dir", str(output_dir),
                "--response-col", job_config["response_col"],
                "--lat-col", job_config.get("lat_col", "latitude"),
                "--lon-col", job_config.get("lon_col", "longitude"),
                "--uncertainty", job_config.get("uncertainty", "quantile_rf"),
                "--resolution", str(job_config.get("resolution", "5")),
            ]
        
        else:
            return None
        
        return cmd
    
    elif engine == "r":
        return None  # Return None to indicate R engine (use original R script logic)
    
    else:
        raise ValueError(f"Invalid EXECUTION_ENGINE: {engine}")


def _run_with_fallback(job, script_type: str, python_cmd: Optional[list[str]],
                      r_cmd: list[str], metrics_path: Path) -> tuple[subprocess.CompletedProcess, str]:
    """
    Execute script, with fallback to R if Python fails (if enabled).
    
    Args:
        job: Job instance
        script_type: "gen_rasters" or "run_isoscape"
        python_cmd: Python command (None if using R)
        r_cmd: R command (fallback)
        metrics_path: Path to metrics.json
        
    Returns:
        Tuple of (CompletedProcess result, engine_used)
    """
    engine_used = "r"  # default
    proc = None
    
    # Try Python engine if configured
    if python_cmd is not None:
        engine_used = "python"
        proc = _run_script(python_cmd)
        
        # If Python failed and fallback enabled, try R
        if proc.returncode != 0 and settings.SCRIPT_ENGINE_FALLBACK:
            engine_used = "r (fallback)"
            proc = _run_script(r_cmd)
    else:
        # R engine
        proc = _run_script(r_cmd)
    
    return proc, engine_used


# =============================================================================
# Task 1 — Geração de rasters (WorldClim → recorte pelo shapefile)
# =============================================================================

@shared_task(bind=True)
def gen_rasters_task(self, job_id: int):
    from apps.jobs.models import Job
    from apps.rasters.models import RasterLayer

    job = Job.objects.get(id=job_id)
    _update_job(job, status=Job.Status.RUNNING, celery_task_id=self.request.id)

    cfg        = job.config
    output_dir = settings.DATA_RASTERS_DIR / str(job.project_id) / cfg["study_area_name"]
    output_dir.mkdir(parents=True, exist_ok=True)

    # Build R command (fallback)
    r_cmd = [
        "Rscript",
        str(Path(settings.BASE_DIR) / "r_scripts" / "gen_rasters.R"),
        "--job-id",        str(job_id),
        "--shapefile",     cfg["shapefile_path"],
        "--output-dir",    str(output_dir),
        "--worldclim-dir", str(settings.DATA_WORLDCLIM_DIR),
        "--variables",     ",".join(cfg["variables"]),
        "--resolution",    str(cfg.get("resolution", "5")),
        "--skip-existing", str(cfg.get("skip_existing", True)),
    ]

    if "bio" in cfg.get("variables", []) and cfg.get("bio_layers"):
        r_cmd += ["--bio-layers", ",".join(cfg["bio_layers"])]

    # Build Python command (primary) or None if using R
    python_cmd = _get_execution_command("gen_rasters", job_id, cfg, output_dir)

    try:
        _update_job(job, progress_step="1/2 — Running gen_rasters")
        
        # Execute with fallback
        proc, engine_used = _run_with_fallback(
            job, "gen_rasters", python_cmd, r_cmd,
            output_dir / "metrics.json"
        )

        job.log = proc.stdout
        if proc.returncode != 0:
            _update_job(
                job,
                status=Job.Status.FAILED,
                log=proc.stdout,
                error_message=proc.stderr,
                finished_at=datetime.now(),
            )
            return

        # Read metrics.json to register RasterLayers
        metrics_path = output_dir / "metrics.json"
        metrics      = json.loads(metrics_path.read_text())

        study_area = job.config.get("study_area_id")
        from apps.shapefiles.models import StudyArea
        sa = StudyArea.objects.get(id=study_area)

        resolution = str(cfg.get("resolution", "5"))

        for file_path in metrics["generated_files"]:
            stem = Path(file_path).stem
            parts = stem.split(f"_{resolution}arc_", 1)
            variable = parts[1].replace("_mean", "") if len(parts) == 2 else stem

            RasterLayer.objects.update_or_create(
                study_area=sa,
                variable=variable,
                resolution=resolution,
                defaults={"file_path": file_path},
            )

        _update_job(
            job,
            status=Job.Status.COMPLETED,
            log=proc.stdout,
            progress_step="2/2 — Complete",
            finished_at=datetime.now(),
        )

    except Exception as exc:
        _update_job(
            job,
            status=Job.Status.FAILED,
            error_message=str(exc),
            finished_at=datetime.now(),
        )
        raise


# =============================================================================
# Task 2 — Geração de isoscape (Random Forest + incerteza)
# =============================================================================

@shared_task(bind=True)
def run_isoscape_task(self, job_id: int):
    from apps.jobs.models import Job
    from apps.isoscapes.models import Isoscape

    job = Job.objects.get(id=job_id)
    _update_job(job, status=Job.Status.RUNNING, celery_task_id=self.request.id)

    cfg        = job.config
    output_dir = settings.DATA_ISOSCAPES_DIR / str(job_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Build R command (fallback)
    r_cmd = [
        "Rscript",
        str(Path(settings.BASE_DIR) / "r_scripts" / "run_isoscape.R"),
        "--job-id",       str(job_id),
        "--dataset-path", cfg["dataset_path"],
        "--raster-dir",   cfg["raster_dir"],
        "--output-dir",   str(output_dir),
        "--response-col", cfg["response_col"],
        "--lat-col",      cfg.get("lat_col", "latitude"),
        "--lon-col",      cfg.get("lon_col", "longitude"),
        "--uncertainty",  cfg.get("uncertainty", "quantile_rf"),
        "--resolution",   str(cfg.get("resolution", "5")),
    ]

    # Build Python command (primary) or None if using R
    python_cmd = _get_execution_command("run_isoscape", job_id, cfg, output_dir)

    try:
        _update_job(job, progress_step="1/7 — Initiating run_isoscape")
        
        # Execute with fallback
        proc, engine_used = _run_with_fallback(
            job, "run_isoscape", python_cmd, r_cmd,
            output_dir / "metrics.json"
        )

        job.log = proc.stdout
        if proc.returncode != 0:
            _update_job(
                job,
                status=Job.Status.FAILED,
                log=proc.stdout,
                error_message=proc.stderr,
                finished_at=datetime.now(),
            )
            return

        metrics_path = output_dir / "metrics.json"
        metrics      = json.loads(metrics_path.read_text())

        Isoscape.objects.create(
            job=job,
            project=job.project,
            name=f"Isoscape — Job {job_id}",
            isoscape_path=metrics["isoscape_path"],
            uncertainty_path=metrics["uncertainty_path"],
            mse=metrics["MSE"],
            r_squared=metrics["R2"],
            selected_vars={
                "threshold_vars": metrics.get("threshold_vars", []),
                "interp_vars":    metrics.get("interp_vars", []),
                "pred_vars":      metrics.get("pred_vars", []),
            },
            model_type=cfg.get("model_type", "random_forest"),
            uncertainty_method=cfg.get("uncertainty", "quantile_rf"),
        )

        _update_job(
            job,
            status=Job.Status.COMPLETED,
            log=proc.stdout,
            progress_step="7/7 — Complete",
            finished_at=datetime.now(),
        )

    except Exception as exc:
        _update_job(
            job,
            status=Job.Status.FAILED,
            error_message=str(exc),
            finished_at=datetime.now(),
        )
        raise
# Helper — verificar se já existem rasters para uma StudyArea + resolução
# Usado pela view antes de decidir se dispara gen_rasters_task
# =============================================================================

def rasters_exist(study_area_id: int, resolution: str) -> bool:
    from apps.rasters.models import RasterLayer
    return RasterLayer.objects.filter(
        study_area_id=study_area_id,
        resolution=resolution,
    ).exists()