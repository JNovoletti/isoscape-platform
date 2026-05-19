"""
backend/python_scripts/utils.py

Utilitários compartilhados para gen_rasters.py, run_isoscape.py e run_assign.py.
"""

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

import geopandas as gpd
import pandas as pd


# =============================================================================
# Logging
# =============================================================================

def setup_logging(output_dir: Path, job_id: str, script_name: str = "script") -> logging.Logger:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "log.txt"

    logger = logging.getLogger(f"{script_name}_{job_id}")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    return logger


def log_msg(logger: logging.Logger, message: str, level: str = "INFO"):
    getattr(logger, level.lower(), logger.info)(message)


# =============================================================================
# Metrics
# =============================================================================

class MetricsCollector:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.data: Dict[str, Any] = {
            "job_id":    job_id,
            "timestamp": datetime.now().isoformat(),
        }

    def add(self, key: str, value: Any):
        self.data[key] = value

    def to_dict(self) -> Dict[str, Any]:
        return self.data

    def save(self, output_path: Path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, default=str)


# =============================================================================
# Geospatial I/O
# =============================================================================

def load_shapefile(shapefile_path) -> gpd.GeoDataFrame:
    """
    Carrega shapefile. Se CRS ausente, assume SIRGAS 2000 (EPSG:4674).
    """
    path = Path(shapefile_path)
    if not path.exists():
        raise FileNotFoundError(f"Shapefile não encontrado: {path}")

    gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4674")
    return gdf


def load_dataset(
    dataset_path,
    lat_col: str = "latitude",
    lon_col: str = "longitude",
    response_col: str = "response",
) -> Tuple[pd.DataFrame, gpd.GeoDataFrame, pd.Series]:
    """
    Carrega CSV ou XLSX e retorna (df_original, GeoDataFrame, response_series).

    O GeoDataFrame sempre tem colunas 'latitude', 'longitude', 'response'
    (renomeadas a partir de lat_col / lon_col / response_col).
    Linhas com NA na resposta são removidas (espelha o tratamento do R).
    """
    path = Path(dataset_path)
    if not path.exists():
        raise FileNotFoundError(f"Dataset não encontrado: {path}")

    ext = path.suffix.lower()
    if ext == ".csv":
        df = pd.read_csv(path)
    elif ext in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    else:
        raise ValueError(f"Formato não suportado: {ext}")

    # Validar colunas obrigatórias
    missing = [c for c in [lat_col, lon_col, response_col] if c not in df.columns]
    if missing:
        raise ValueError(f"Colunas ausentes no dataset: {', '.join(missing)}")

    # Selecionar e renomear
    df_sel = df[[lat_col, lon_col, response_col]].copy()
    df_sel = df_sel.rename(columns={
        lat_col:      "latitude",
        lon_col:      "longitude",
        response_col: "response",
    })

    # Converter para numérico
    for col in ("latitude", "longitude", "response"):
        df_sel[col] = pd.to_numeric(df_sel[col], errors="coerce")

    # Remover NA na resposta (espelha o R: d13.amz <- amz_var_clim[!is.na(d13C_wood), ])
    df_sel = df_sel[df_sel["response"].notna()].reset_index(drop=True)

    gdf = gpd.GeoDataFrame(
        df_sel,
        geometry=gpd.points_from_xy(df_sel["longitude"], df_sel["latitude"]),
        crs="EPSG:4674",
    )

    return df, gdf, gdf["response"]


# =============================================================================
# CLI helpers
# =============================================================================

def parse_csv_list(value: str, default: Optional[List[str]] = None) -> List[str]:
    if not value or not str(value).strip():
        return default or []
    return [v.strip() for v in str(value).split(",") if v.strip()]


def ensure_output_dir(output_dir) -> Path:
    p = Path(output_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_file_prefix(shapefile_path) -> str:
    stem = Path(shapefile_path).stem
    return re.sub(r"[^A-Za-z0-9_]", "_", stem)