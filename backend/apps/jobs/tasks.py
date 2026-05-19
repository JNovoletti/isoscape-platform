# apps/jobs/tasks.py

import subprocess
import json
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional

from celery import shared_task
from django.conf import settings


# =============================================================================
# Helpers internos
# =============================================================================

def _update_job(job, **fields):
    """Salva campos específicos do Job atomicamente."""
    for k, v in fields.items():
        setattr(job, k, v)
    job.save(update_fields=list(fields.keys()))


def _run_script(cmd: list[str]) -> subprocess.CompletedProcess:
    """Executa um script (R ou Python) e retorna o resultado."""
    return subprocess.run(cmd, capture_output=True, text=True)


def _build_python_cmd(script_type: str, job_id: int, cfg: dict, output_dir: Path) -> Optional[list[str]]:
    """
    Constrói o comando Python para o script_type dado.
    Retorna None se a engine configurada for 'r'.
    """
    engine = str(cfg.get("execution_engine", settings.EXECUTION_ENGINE)).lower()

    if engine == "r":
        return None

    if engine != "python":
        raise ValueError(f"execution_engine inválido: {engine!r}. Use 'r' ou 'python'.")

    base_dir = Path(settings.BASE_DIR) / "python_scripts"

    if script_type == "prepare_dataset":
        cmd = [
            sys.executable,
            str(base_dir / "prepare_dataset.py"),
            "--job-id",          str(job_id),
            "--input",           cfg["input_path"],
            "--output",          cfg["output_path"],
            "--output-dir",      str(output_dir),
            "--lat-col",         cfg.get("lat_col", "latitude"),
            "--lon-col",         cfg.get("lon_col", "longitude"),
            "--value-cols",      ",".join(cfg["value_cols"]),
            "--agg-method",      cfg.get("agg_method", "mean"),
            "--coord-precision", str(cfg.get("coord_precision", 6)),
        ]
        if cfg.get("keep_cols"):
            cmd += ["--keep-cols", ",".join(cfg["keep_cols"])]
        return cmd

    if script_type == "gen_rasters":
        cmd = [
            sys.executable,
            str(base_dir / "gen_rasters.py"),
            "--job-id",        str(job_id),
            "--shapefile",     cfg["shapefile_path"],
            "--output-dir",    str(output_dir),
            "--worldclim-dir", str(settings.DATA_WORLDCLIM_DIR),
            "--variables",     ",".join(cfg["variables"]),
            "--resolution",    str(cfg.get("resolution", "5")),
            "--skip-existing", str(cfg.get("skip_existing", True)).lower(),
        ]
        if "bio" in cfg.get("variables", []) and cfg.get("bio_layers"):
            cmd += ["--bio-layers", ",".join(cfg["bio_layers"])]
        return cmd

    if script_type == "run_isoscape":
        cmd = [
            sys.executable,
            str(base_dir / "run_isoscape.py"),
            "--job-id",       str(job_id),
            "--dataset-path", cfg["dataset_path"],
            "--raster-dir",   cfg["raster_dir"],
            "--output-dir",   str(output_dir),
            "--response-col", cfg["response_col"],
            "--lat-col",      cfg.get("lat_col", "latitude"),
            "--lon-col",      cfg.get("lon_col", "longitude"),
            "--uncertainty",  cfg.get("uncertainty", "quantile_rf"),
            "--resolution",   str(cfg.get("resolution", "5")),
            "--seed",         str(cfg.get("seed", 1350)),
        ]
        # Por padrão, Python lê apenas rasters _py.tif (gerados por gen_rasters.py).
        # Se quiser usar rasters do R, passar raster_suffix="_r" no config.
        if "raster_suffix" in cfg:
            cmd += ["--raster-suffix", str(cfg["raster_suffix"])]
        return cmd

    if script_type == "run_assign":
        cmd = [
            sys.executable,
            str(base_dir / "run_assign.py"),
            "--job-id",         str(job_id),
            "--isoscape-path",  cfg["isoscape_path"],
            "--unknown-path",   cfg["unknown_path"],
            "--regions-shp",    cfg["regions_shp"],
            "--regions-field",  cfg.get("regions_field", "ADM1_PT"),
            "--output-dir",     str(output_dir),
            "--response-col",   cfg.get("response_col", "d13C_wood"),
            "--area-threshold", str(cfg.get("area_threshold", 0.5)),
            "--prob-threshold", str(cfg.get("prob_threshold", 0.95)),
            "--seed",           str(cfg.get("seed", 1350)),
        ]
        if cfg.get("regions_filter"):
            cmd += ["--regions-filter", ",".join(cfg["regions_filter"])]
        return cmd

    raise ValueError(f"script_type inválido: {script_type!r}")


def _build_r_cmd(script_type: str, job_id: int, cfg: dict, output_dir: Path) -> list[str]:
    """Constrói o comando R para o script_type dado."""
    r_scripts_dir = Path(settings.BASE_DIR) / "r_scripts"

    if script_type == "gen_rasters":
        cmd = [
            "Rscript",
            str(r_scripts_dir / "gen_rasters.R"),
            "--job-id",        str(job_id),
            "--shapefile",     cfg["shapefile_path"],
            "--output-dir",    str(output_dir),
            "--worldclim-dir", str(settings.DATA_WORLDCLIM_DIR),
            "--variables",     ",".join(cfg["variables"]),
            "--resolution",    str(cfg.get("resolution", "5")),
            "--skip-existing", str(cfg.get("skip_existing", True)),
        ]
        if "bio" in cfg.get("variables", []) and cfg.get("bio_layers"):
            cmd += ["--bio-layers", ",".join(cfg["bio_layers"])]
        return cmd

    if script_type == "run_isoscape":
        cmd = [
            "Rscript",
            str(r_scripts_dir / "run_isoscape.R"),
            "--job-id",       str(job_id),
            "--dataset-path", cfg["dataset_path"],
            "--raster-dir",   cfg["raster_dir"],
            "--output-dir",   str(output_dir),
            "--response-col", cfg["response_col"],
            "--lat-col",      cfg.get("lat_col", "latitude"),
            "--lon-col",      cfg.get("lon_col", "longitude"),
            "--uncertainty",  cfg.get("uncertainty", "quantile_rf"),
            "--resolution",   str(cfg.get("resolution", "5")),
            "--seed",         str(cfg.get("seed", 1350)),
        ]
        if "raster_suffix" in cfg:
            cmd += ["--raster-suffix", str(cfg["raster_suffix"])]
        return cmd

    if script_type == "run_assign":
        cmd = [
            "Rscript",
            str(r_scripts_dir / "run_assign.R"),
            "--job-id",         str(job_id),
            "--isoscape-path",  cfg["isoscape_path"],
            "--unknown-path",   cfg["unknown_path"],
            "--regions-shp",    cfg["regions_shp"],
            "--regions-field",  cfg.get("regions_field", "ADM1_PT"),
            "--output-dir",     str(output_dir),
            "--response-col",   cfg.get("response_col", "d13C_wood"),
            "--area-threshold", str(cfg.get("area_threshold", 0.5)),
            "--prob-threshold", str(cfg.get("prob_threshold", 0.95)),
            "--seed",           str(cfg.get("seed", 1350)),
        ]
        if cfg.get("regions_filter"):
            cmd += ["--regions-filter", ",".join(cfg["regions_filter"])]
        return cmd

    raise ValueError(f"script_type inválido: {script_type!r}")


def _execute(python_cmd: Optional[list[str]], r_cmd: list[str]) -> tuple[subprocess.CompletedProcess, str]:
    """
    Executa o script escolhido, com fallback para R se configurado.

    Retorna (CompletedProcess, engine_usada).
    engine_usada é uma das strings: 'python', 'r', 'r (fallback)'.
    """
    if python_cmd is None:
        # Engine R selecionada explicitamente
        return _run_script(r_cmd), "r"

    # Engine Python — tenta primeiro
    proc = _run_script(python_cmd)
    if proc.returncode == 0:
        return proc, "python"

    # Python falhou — verificar se fallback está ativo
    if getattr(settings, "SCRIPT_ENGINE_FALLBACK", False):
        return _run_script(r_cmd), "r (fallback)"

    return proc, "python"


def _make_log(engine: str, proc: subprocess.CompletedProcess) -> str:
    """Monta string de log combinando indicador de engine e stdout."""
    return f"[engine] {engine}\n" + (proc.stdout or "")


def _make_error(proc: subprocess.CompletedProcess) -> str:
    """Prefere stderr; cai para stdout se stderr estiver vazio."""
    return (proc.stderr or proc.stdout or "").strip()


# =============================================================================
# Task 0 — Pré-processamento de dataset (agrega duplicatas por lat/lon)
# =============================================================================

@shared_task(bind=True)
def prepare_dataset_task(self, job_id: int):
    """
    Agrega múltiplas amostras na mesma coordenada (lat/lon) em uma única
    observação. Espera no config:

      - input_path:       caminho do CSV/XLSX original (ex: madeiras.csv)
      - output_path:      caminho do CSV agregado (será criado/sobrescrito)
      - lat_col:          nome da coluna de latitude  (default 'latitude')
      - lon_col:          nome da coluna de longitude (default 'longitude')
      - value_cols:       lista de colunas a agregar  (ex: ['d13C_wood'])
      - keep_cols:        lista de colunas a preservar via 'first'
                          (ex: ['Site', 'Family'])
      - agg_method:       'mean' (default) | 'median' | 'min' | 'max' | 'sum'
      - coord_precision:  casas decimais ao arredondar coordenadas (default 6)

    Esta task é Python-only por design: agregação é trivial e fazer em R duplica
    código sem ganho científico — R e Python downstream leem o mesmo CSV.
    """
    from apps.jobs.models import Job

    job = Job.objects.get(id=job_id)
    _update_job(job, status=Job.Status.RUNNING, celery_task_id=self.request.id)

    cfg = job.config
    # Forçar engine python (essa task não tem versão R)
    cfg = {**cfg, "execution_engine": "python"}

    output_dir = settings.DATA_PREPARED_DIR / str(job_id) \
        if hasattr(settings, "DATA_PREPARED_DIR") \
        else Path(cfg["output_path"]).parent / f".prepare_{job_id}"
    output_dir.mkdir(parents=True, exist_ok=True)

    python_cmd = _build_python_cmd("prepare_dataset", job_id, cfg, output_dir)

    try:
        _update_job(job, progress_step="1/1 — Agregando duplicatas")

        # prepare_dataset é Python-only — chamar _run_script direto,
        # sem passar pelo _execute (que tem semântica de fallback R).
        proc = _run_script(python_cmd)
        full_log = _make_log("python", proc)

        if proc.returncode != 0:
            _update_job(
                job,
                status=Job.Status.FAILED,
                log=full_log,
                error_message=_make_error(proc),
                finished_at=datetime.now(),
            )
            return

        _update_job(
            job,
            status=Job.Status.COMPLETED,
            log=full_log,
            progress_step="1/1 — Concluído",
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
# Task 1 — Geração de rasters (WorldClim → recorte pelo shapefile)
# =============================================================================

@shared_task(bind=True)
def gen_rasters_task(self, job_id: int):
    from apps.jobs.models import Job
    from apps.rasters.models import RasterLayer
    from apps.shapefiles.models import StudyArea

    job = Job.objects.get(id=job_id)
    _update_job(job, status=Job.Status.RUNNING, celery_task_id=self.request.id)

    cfg        = job.config
    output_dir = settings.DATA_RASTERS_DIR / str(job.project_id) / cfg["study_area_name"]
    output_dir.mkdir(parents=True, exist_ok=True)

    r_cmd      = _build_r_cmd("gen_rasters", job_id, cfg, output_dir)
    python_cmd = _build_python_cmd("gen_rasters", job_id, cfg, output_dir)

    try:
        _update_job(job, progress_step="1/2 — Executando gen_rasters")

        proc, engine = _execute(python_cmd, r_cmd)
        full_log     = _make_log(engine, proc)

        if proc.returncode != 0:
            _update_job(
                job,
                status=Job.Status.FAILED,
                log=full_log,
                error_message=_make_error(proc),
                finished_at=datetime.now(),
            )
            return

        # Ler metrics.json para registrar RasterLayers
        metrics_path = output_dir / "metrics.json"
        metrics      = json.loads(metrics_path.read_text())

        study_area_id = cfg["study_area_id"]
        sa            = StudyArea.objects.get(id=study_area_id)
        resolution    = str(cfg.get("resolution", "5"))
        # Sufixo de engine: "_r" ou "_py" — usado para diferenciar arquivos no banco
        engine_suffix = "_r" if engine.startswith("r") else "_py"

        for file_path in metrics["generated_files"]:
            stem  = Path(file_path).stem
            # Remove o sufixo de engine para extrair o nome da variável
            stem_clean = stem
            for suf in ("_py", "_r"):
                if stem_clean.endswith(suf):
                    stem_clean = stem_clean[: -len(suf)]
                    break
            parts = stem_clean.split(f"_{resolution}arc_", 1)
            variable_base = parts[1].replace("_mean", "") if len(parts) == 2 else stem_clean
            # Variable inclui o sufixo de engine para diferenciar registros no banco
            variable = f"{variable_base}{engine_suffix}"

            RasterLayer.objects.update_or_create(
                study_area=sa,
                variable=variable,
                resolution=resolution,
                defaults={"file_path": file_path},
            )

        _update_job(
            job,
            status=Job.Status.COMPLETED,
            log=full_log,
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

    r_cmd      = _build_r_cmd("run_isoscape", job_id, cfg, output_dir)
    python_cmd = _build_python_cmd("run_isoscape", job_id, cfg, output_dir)

    try:
        _update_job(job, progress_step="1/7 — Iniciando run_isoscape")

        proc, engine = _execute(python_cmd, r_cmd)
        full_log     = _make_log(engine, proc)

        if proc.returncode != 0:
            _update_job(
                job,
                status=Job.Status.FAILED,
                log=full_log,
                error_message=_make_error(proc),
                finished_at=datetime.now(),
            )
            return

        metrics_path = output_dir / "metrics.json"
        metrics      = json.loads(metrics_path.read_text())

        engine_label = "r" if engine.startswith("r") else "python"

        Isoscape.objects.create(
            job=job,
            project=job.project,
            name=f"Isoscape — Job {job_id} ({engine_label})",
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
            log=full_log,
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
# Task 3 — Atribuição de origem (Bayes + isoscape)
# =============================================================================

@shared_task(bind=True)
def run_assign_task(self, job_id: int):
    """
    Atribuição de origem geográfica para amostras desconhecidas.
    Equivalente backend do 04_assign.R do curso.

    Espera no config:
      - execution_engine: "r" ou "python"
      - isoscape_path:    caminho do isoscape combinado (2 bandas: predição + sd)
                          Ex: /data/isoscapes/123/isoscape_combined_py.tif
      - unknown_path:     CSV/XLSX com amostras desconhecidas (precisa de ID e response-col)
      - regions_shp:      shapefile das regiões candidatas (ex: UFs)
      - regions_field:    campo do shapefile para identificar regiões (default ADM1_PT)
      - regions_filter:   lista opcional de nomes de regiões (default: todas)
      - response_col:     nome da coluna isotópica nos unknowns (default d13C_wood)
      - area_threshold:   threshold por área para qtlRaster (default 0.5)
      - prob_threshold:   threshold por probabilidade para qtlRaster (default 0.95)
    """
    from apps.jobs.models import Job

    job = Job.objects.get(id=job_id)
    _update_job(job, status=Job.Status.RUNNING, celery_task_id=self.request.id)

    cfg        = job.config
    output_dir = settings.DATA_ASSIGNMENTS_DIR / str(job_id) \
        if hasattr(settings, "DATA_ASSIGNMENTS_DIR") \
        else settings.DATA_ISOSCAPES_DIR / "assignments" / str(job_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    r_cmd      = _build_r_cmd("run_assign", job_id, cfg, output_dir)
    python_cmd = _build_python_cmd("run_assign", job_id, cfg, output_dir)

    try:
        _update_job(job, progress_step="1/3 — Iniciando run_assign")

        proc, engine = _execute(python_cmd, r_cmd)
        full_log     = _make_log(engine, proc)

        if proc.returncode != 0:
            _update_job(
                job,
                status=Job.Status.FAILED,
                log=full_log,
                error_message=_make_error(proc),
                finished_at=datetime.now(),
            )
            return

        # metrics.json já contém os caminhos dos pd_maps / qtl_maps / odds_csv
        # Modelo Assignment é opcional — registrar se existir.
        try:
            from apps.assignments.models import Assignment  # type: ignore
            metrics_path = output_dir / "metrics.json"
            metrics      = json.loads(metrics_path.read_text())
            engine_label = "r" if engine.startswith("r") else "python"

            Assignment.objects.create(
                job=job,
                project=job.project,
                name=f"Assignment — Job {job_id} ({engine_label})",
                output_dir=str(output_dir),
                metrics=metrics,
            )
        except ImportError:
            # App Assignments não existe ainda — sem problema
            pass
        except Exception as e:
            # Erro ao criar Assignment não deve invalidar o job
            full_log += f"\n[!] Falha ao criar Assignment no banco: {e}"

        _update_job(
            job,
            status=Job.Status.COMPLETED,
            log=full_log,
            progress_step="3/3 — Concluído",
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
# =============================================================================

def rasters_exist(study_area_id: int, resolution: str, engine: str = "any") -> bool:
    """
    engine: "r", "python" ou "any" (default).
    Filtra por sufixo "_r" / "_py" na coluna variable.
    """
    from apps.rasters.models import RasterLayer
    qs = RasterLayer.objects.filter(
        study_area_id=study_area_id,
        resolution=resolution,
    )
    if engine == "r":
        qs = qs.filter(variable__endswith="_r")
    elif engine == "python":
        qs = qs.filter(variable__endswith="_py")
    return qs.exists()