# from celery import shared_task

# @shared_task
# def hello_task(name: str) -> str:
#     return f"Olá, {name}! Celery funcionando."
# apps/jobs/tasks.py

import subprocess
import json
from pathlib import Path
from datetime import datetime

from celery import shared_task
from django.conf import settings


def _update_job(job, **fields):
    """Atalho para salvar campos específicos do Job."""
    for k, v in fields.items():
        setattr(job, k, v)
    job.save(update_fields=list(fields.keys()))


def _run_rscript(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


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

    cmd = [
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

    # Inclui bio-layers só se o usuário selecionou a variável "bio"
    if "bio" in cfg.get("variables", []) and cfg.get("bio_layers"):
        cmd += ["--bio-layers", ",".join(cfg["bio_layers"])]

    try:
        _update_job(job, progress_step="1/2 — Rodando gen_rasters.R")
        proc = _run_rscript(cmd)

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

        # Ler metrics.json para registrar os RasterLayers no banco
        metrics_path = output_dir / "metrics.json"
        metrics      = json.loads(metrics_path.read_text())

        study_area = job.config.get("study_area_id")
        from apps.shapefiles.models import StudyArea
        sa = StudyArea.objects.get(id=study_area)

        resolution = str(cfg.get("resolution", "5"))

        for file_path in metrics["generated_files"]:
            # Extrair nome da variável do nome do arquivo
            # Padrão: {prefix}_{res}arc_{var}_mean.tif  ou  {prefix}_{res}arc_bio{N}.tif
            stem = Path(file_path).stem                      # ex: amazonia_5arc_tavg_mean
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
            progress_step="2/2 — Concluído",
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

    cmd = [
        "Rscript",
        str(Path(settings.BASE_DIR) / "r_scripts" / "run_isoscape.R"),
        "--job-id",       str(job_id),
        "--dataset-path", cfg["dataset_path"],
        "--raster-dir",   cfg["raster_dir"],
        "--output-dir",   str(output_dir),
        "--response-col", cfg["response_col"],
        "--lat-col",      cfg["lat_col"],
        "--lon-col",      cfg["lon_col"],
        "--uncertainty",  cfg.get("uncertainty", "quantile_rf"),
        "--resolution",   str(cfg.get("resolution", "5")),
    ]

    try:
        _update_job(job, progress_step="1/7 — Iniciando run_isoscape.R")
        proc = _run_rscript(cmd)

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
                "threshold_vars": metrics["threshold_vars"],
                "interp_vars":    metrics["interp_vars"],
                "pred_vars":      metrics["pred_vars"],
            },
            model_type=cfg.get("model_type", "random_forest"),
            uncertainty_method=cfg.get("uncertainty", "quantile_rf"),
        )

        _update_job(
            job,
            status=Job.Status.COMPLETED,
            log=proc.stdout,
            progress_step="7/7 — Concluído",
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
# Helper — verificar se já existem rasters para uma StudyArea + resolução
# Usado pela view antes de decidir se dispara gen_rasters_task
# =============================================================================

def rasters_exist(study_area_id: int, resolution: str) -> bool:
    from apps.rasters.models import RasterLayer
    return RasterLayer.objects.filter(
        study_area_id=study_area_id,
        resolution=resolution,
    ).exists()