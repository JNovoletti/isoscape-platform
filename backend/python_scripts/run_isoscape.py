#!/usr/bin/env python
"""
backend/python_scripts/run_isoscape.py

Equivalente Python do run_isoscape.R (e dos scripts 02_extr_dados_raster.R +
03_integracao_ML.R do curso). Treina Random Forest sobre amostras + rasters
preditores e gera isoscape + mapa de incerteza.

Paridade fiel com 03_integracao_ML.R:
  - VSURF aproximado via VSURFApprox (3 etapas: threshold → interp → pred)
    com mesmos parâmetros: ntree=500, nfor.thres=20, nfor.interp=100,
    nfor.pred=10, nsd=1
  - RandomForestRegressor com n_estimators=2000 para o modelo principal
  - Split estratificado p=0.8 (espelha caret::createDataPartition)
  - Quantile RF via `quantile-forest` (RandomForestQuantileRegressor) —
    equivalente direto ao ranger quantreg do R
  - Bootstrap fallback com 50 iterações

Sufixo "_py" nos outputs diferencia da versão R ("_r"):
  isoscape_py.tif | uncertainty_py.tif | dataset_with_vars_py.csv

Uso:
    python run_isoscape.py \\
      --job-id abc-123 \\
      --dataset-path /data/datasets/madeiras.csv \\
      --raster-dir   /data/rasters/project1/ \\
      --output-dir   /data/isoscapes/abc-123/ \\
      --response-col d13C_wood \\
      --lat-col      latitude \\
      --lon-col      longitude \\
      --uncertainty  quantile_rf \\
      --resolution   5 \\
      --seed         1350
"""

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Tuple
import logging
import warnings

import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.transform import rowcol
from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
from sklearn.model_selection import StratifiedShuffleSplit
from sklearn.metrics import mean_squared_error, r2_score

# quantile-forest é equivalente direto ao ranger quantreg do R
try:
    from quantile_forest import RandomForestQuantileRegressor
    HAS_QUANTILE_FOREST = True
except ImportError:
    HAS_QUANTILE_FOREST = False

sys.path.insert(0, str(Path(__file__).parent.parent))

from python_scripts.utils import (
    setup_logging, log_msg, MetricsCollector,
    load_dataset, ensure_output_dir
)

warnings.filterwarnings("ignore")

TARGET_CRS  = "EPSG:4674"
NODATA_OUT  = -9999.0


# =============================================================================
# Helper: extração bilinear de raster nos pontos
# =============================================================================

def _bilinear_sample(src, x: float, y: float, nodata: float) -> float:
    """
    Amostragem bilinear de um raster aberto em (x, y) — equivalente ao
    method="bilinear" do terra::extract.
    """
    # Converter (x,y) → coordenadas fracionárias de pixel
    inv = ~src.transform
    px, py = inv * (x, y)  # fractional col, row
    col_f = px - 0.5  # rasterio: pixel centers em 0.5
    row_f = py - 0.5

    c0 = int(np.floor(col_f)); c1 = c0 + 1
    r0 = int(np.floor(row_f)); r1 = r0 + 1
    if not (0 <= r0 and r1 < src.height and 0 <= c0 and c1 < src.width):
        return np.nan

    win = src.read(1, window=((r0, r1 + 1), (c0, c1 + 1))).astype(np.float64)
    win = np.where(win == nodata, np.nan, win)
    if np.any(np.isnan(win)):
        return np.nan

    dx = col_f - c0
    dy = row_f - r0
    val = (
        win[0, 0] * (1 - dx) * (1 - dy)
        + win[0, 1] * dx * (1 - dy)
        + win[1, 0] * (1 - dx) * dy
        + win[1, 1] * dx * dy
    )
    return float(val)


def extract_values_at_points(
    raster_paths: List[Path],
    gdf: gpd.GeoDataFrame,
    logger: logging.Logger,
    method: str = "bilinear",
) -> pd.DataFrame:
    """
    Extrai valores dos rasters nos pontos do GeoDataFrame.

    method='bilinear' espelha terra::extract(..., method="bilinear")
    method='simple'   espelha terra::extract(..., method="simple")
    """
    # Nomes das colunas: usar o stem do arquivo SEM o sufixo de engine
    # para que o nome bata com a versão R.
    def col_name(p: Path) -> str:
        stem = p.stem
        for suf in ("_py", "_r"):
            if stem.endswith(suf):
                return stem[: -len(suf)]
        return stem

    records = {col_name(r): [] for r in raster_paths}

    for raster_path in raster_paths:
        cname = col_name(raster_path)
        with rasterio.open(raster_path) as src:
            points_reproj = gdf.to_crs(src.crs)
            nodata = src.nodata if src.nodata is not None else NODATA_OUT

            vals: List[float] = []
            if method == "bilinear":
                for geom in points_reproj.geometry:
                    try:
                        vals.append(_bilinear_sample(src, geom.x, geom.y, nodata))
                    except Exception:
                        vals.append(np.nan)
            else:
                band = src.read(1)
                for geom in points_reproj.geometry:
                    try:
                        row, col = rowcol(src.transform, geom.x, geom.y)
                        if 0 <= row < src.height and 0 <= col < src.width:
                            v = band[row, col]
                            vals.append(float(v) if v != nodata else np.nan)
                        else:
                            vals.append(np.nan)
                    except Exception:
                        vals.append(np.nan)

            records[cname] = vals

    return pd.DataFrame(records)


# =============================================================================
# Seleção de variáveis — equivalente aproximado ao VSURF do R
# =============================================================================

class VSURFApprox:
    """
    Aproximação Python do VSURF do R, seguindo as 3 etapas do paper original
    (Genuer, Poggi & Tuleau-Malot 2010):

      1) THRESHOLD (eliminação): treina `nfor_thres` RFs, calcula importância
         média de permutação por variável, ordena, e mantém apenas variáveis
         com importância > limiar baseado no desvio padrão das importâncias
         das variáveis menos importantes (nsd × sd das últimas k variáveis).

      2) INTERPRETATION: a partir das variáveis-threshold, treina `nfor_interp`
         RFs incrementais (1ª, 1ª+2ª, ...) e escolhe o conjunto que minimiza
         o OOB error (com tolerância nsd × sd).

      3) PREDICTION: a partir do conjunto de interpretação, faz seleção
         forward removendo variáveis cuja remoção mantém OOB error similar.

    Parâmetros e defaults espelham o 03_integracao_ML.R:
      ntree=500, nfor_thres=20, nfor_interp=100, nfor_pred=10, nsd=1
    """

    def __init__(
        self,
        ntree: int = 500,
        nfor_thres: int = 20,
        nfor_interp: int = 100,
        nfor_pred: int = 10,
        nsd: float = 1.0,
        min_features: int = 1,
        random_state: int = 1350,
    ):
        self.ntree        = ntree
        self.nfor_thres   = nfor_thres
        self.nfor_interp  = nfor_interp
        self.nfor_pred    = nfor_pred
        self.nsd          = nsd
        self.min_features = min_features
        self.random_state = random_state

        self.threshold_vars_: List[str] = []
        self.interp_vars_:    List[str] = []
        self.pred_vars_:      List[str] = []

    # ── Importância média de permutação (espelha o que o RF do R reporta) ────
    def _mean_permutation_importance(
        self, X: np.ndarray, y: np.ndarray, n_runs: int
    ) -> np.ndarray:
        n_feat = X.shape[1]
        imps = np.zeros((n_runs, n_feat), dtype=np.float64)
        for k in range(n_runs):
            rf = RandomForestRegressor(
                n_estimators=self.ntree,
                max_features=max(1, n_feat // 3),  # ~mtry default do R (p/3 p/ regressão)
                min_samples_leaf=5,
                random_state=self.random_state + k,
                n_jobs=-1,
                oob_score=False,
            )
            rf.fit(X, y)
            imps[k] = rf.feature_importances_
        return imps.mean(axis=0)

    def _oob_error(self, X: np.ndarray, y: np.ndarray, n_runs: int) -> Tuple[float, float]:
        """Retorna (média OOB MSE, desvio padrão) ao longo de n_runs RFs."""
        n_feat = max(1, X.shape[1])
        errs = []
        for k in range(n_runs):
            rf = RandomForestRegressor(
                n_estimators=self.ntree,
                max_features=max(1, n_feat // 3),
                min_samples_leaf=5,
                random_state=self.random_state + k,
                n_jobs=-1,
                oob_score=True,
                bootstrap=True,
            )
            rf.fit(X, y)
            errs.append(float(mean_squared_error(y, rf.oob_prediction_)))
        return float(np.mean(errs)), float(np.std(errs))

    def fit(self, X: np.ndarray, y: np.ndarray, feature_names: List[str]) -> "VSURFApprox":
        feature_names = list(feature_names)
        X_df = pd.DataFrame(X, columns=feature_names)

        # ── 1. THRESHOLD ─────────────────────────────────────────────────────
        imp_means = self._mean_permutation_importance(X, y, self.nfor_thres)
        # Ordenar por importância média (descendente)
        order = np.argsort(imp_means)[::-1]
        sorted_features    = [feature_names[i] for i in order]
        sorted_importances = imp_means[order]

        # Threshold: nsd × sd das importâncias DAS VARIÁVEIS MENOS IMPORTANTES
        # (paper original: usa as últimas p/2 variáveis para estimar o ruído)
        k_tail = max(1, len(sorted_importances) // 2)
        threshold = self.nsd * np.std(sorted_importances[-k_tail:])

        thres_mask = sorted_importances > threshold
        if not thres_mask.any():
            self.threshold_vars_ = [sorted_features[0]]
        else:
            self.threshold_vars_ = [
                f for f, keep in zip(sorted_features, thres_mask) if keep
            ]

        # ── 2. INTERPRETATION ────────────────────────────────────────────────
        # Adiciona variáveis em ordem de importância e escolhe o conjunto com
        # OOB error mínimo, com tolerância nsd × sd.
        oob_errs: List[float] = []
        oob_sds:  List[float] = []
        for n in range(1, len(self.threshold_vars_) + 1):
            sub = self.threshold_vars_[:n]
            mu, sd = self._oob_error(
                X_df[sub].values, y,
                n_runs=max(1, self.nfor_interp // max(1, len(self.threshold_vars_))),
            )
            oob_errs.append(mu)
            oob_sds.append(sd)

        oob_errs = np.array(oob_errs)
        oob_sds  = np.array(oob_sds)
        best     = int(np.argmin(oob_errs))
        thresh   = oob_errs[best] + self.nsd * oob_sds[best]

        # Pega o MENOR conjunto com OOB ≤ thresh
        candidates = np.where(oob_errs <= thresh)[0]
        chosen_idx = int(candidates.min()) if len(candidates) > 0 else best
        self.interp_vars_ = self.threshold_vars_[: chosen_idx + 1]

        # ── 3. PREDICTION ────────────────────────────────────────────────────
        # Forward elimination dentro do conjunto de interpretação: começa com
        # 1 variável e adiciona apenas se reduz OOB significativamente.
        pred_vars = [self.interp_vars_[0]]
        baseline_mu, baseline_sd = self._oob_error(
            X_df[pred_vars].values, y, n_runs=self.nfor_pred,
        )
        for v in self.interp_vars_[1:]:
            cand = pred_vars + [v]
            mu, sd = self._oob_error(
                X_df[cand].values, y, n_runs=self.nfor_pred,
            )
            # Aceita a variável se reduzir OOB em pelo menos nsd × sd
            if mu < baseline_mu - self.nsd * baseline_sd:
                pred_vars   = cand
                baseline_mu = mu
                baseline_sd = sd

        self.pred_vars_ = pred_vars
        return self


# =============================================================================
# Split estratificado (espelho do caret::createDataPartition)
# =============================================================================

def stratified_split_continuous(
    df: pd.DataFrame,
    response_col: str = "response",
    test_size: float = 0.2,
    random_state: int = 1350,
    n_bins: int = 5,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Split estratificado por quantis da variável resposta contínua.
    Equivalente ao caret::createDataPartition(p=1-test_size, strata=response):
    o caret bina a resposta contínua em quartis (ou similar) e amostra
    estratificadamente.
    """
    # Binagem por quantis (pd.qcut com duplicates="drop" para evitar erros
    # quando há poucos valores únicos)
    try:
        strata = pd.qcut(df[response_col], q=n_bins, labels=False, duplicates="drop")
    except Exception:
        strata = pd.cut(df[response_col], bins=min(n_bins, len(df) // 2),
                        labels=False, duplicates="drop")

    # Fallback: se ainda houver NaN ou bin único, faz split aleatório simples
    if strata.isna().any() or strata.nunique() < 2:
        from sklearn.model_selection import train_test_split
        tr, te = train_test_split(df, test_size=test_size, random_state=random_state)
        return tr.reset_index(drop=True), te.reset_index(drop=True)

    splitter = StratifiedShuffleSplit(
        n_splits=1, test_size=test_size, random_state=random_state,
    )
    idx_train, idx_test = next(splitter.split(df, strata))
    return df.iloc[idx_train].reset_index(drop=True), df.iloc[idx_test].reset_index(drop=True)


# =============================================================================
# Predição espacial
# =============================================================================

def build_raster_design_matrix(
    raster_paths: List[Path],
    feature_names: List[str],
    logger: logging.Logger,
) -> Tuple[np.ndarray, dict, np.ndarray, int, int]:
    """
    Monta a matriz de design X_raster (n_pixels x n_features) lendo os rasters
    preditores. Pixels com qualquer NaN em qualquer preditor ficam fora da
    máscara válida.

    Retorna (X_raster, meta_ref, valid_mask, height, width).
    """
    # Map: nome interno (sem sufixo de engine) → path
    def name_of(p: Path) -> str:
        stem = p.stem
        for suf in ("_py", "_r"):
            if stem.endswith(suf):
                return stem[: -len(suf)]
        return stem

    path_map = {name_of(p): p for p in raster_paths}

    # Raster de referência (primeira feature) define a grade
    ref_name = feature_names[0]
    if ref_name not in path_map:
        raise KeyError(f"Raster da feature de referência não encontrado: {ref_name}")
    ref_path = path_map[ref_name]
    with rasterio.open(ref_path) as src:
        meta   = src.meta.copy()
        height = src.height
        width  = src.width

    n_pixels = height * width
    X_raster = np.full((n_pixels, len(feature_names)), np.nan, dtype=np.float32)

    for i, feat in enumerate(feature_names):
        if feat not in path_map:
            log_msg(logger, f"  [!] Raster para '{feat}' não encontrado — preenchido com NaN", "WARNING")
            continue
        with rasterio.open(path_map[feat]) as src:
            nd = src.nodata if src.nodata is not None else NODATA_OUT
            arr = src.read(1).astype(np.float32).flatten()
            arr[arr == nd] = np.nan
            X_raster[:, i] = arr

    valid_mask = ~np.any(np.isnan(X_raster), axis=1)
    return X_raster, meta, valid_mask, height, width


def predict_raster(
    X_raster: np.ndarray,
    valid_mask: np.ndarray,
    height: int,
    width: int,
    rf_model,
) -> np.ndarray:
    """Aplica o modelo sobre os pixels válidos."""
    pred_flat = np.full(height * width, np.nan, dtype=np.float32)
    if valid_mask.sum() > 0:
        pred_flat[valid_mask] = rf_model.predict(X_raster[valid_mask]).astype(np.float32)
    return pred_flat.reshape(height, width)


# =============================================================================
# Incerteza: Quantile RF — via quantile-forest (equivalente ao ranger quantreg)
# =============================================================================

def uncertainty_quantile_rf(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_raster: np.ndarray,
    valid_mask: np.ndarray,
    height: int,
    width:  int,
    random_state: int,
    logger: logging.Logger,
    n_estimators: int = 500,
) -> np.ndarray:
    """
    Estima incerteza via quantile regression forest (Q0.16 e Q0.84 ≈ ±1σ).

    Quando `quantile-forest` está disponível, usa RandomForestQuantileRegressor
    (equivalente direto ao ranger::ranger(..., quantreg=TRUE)).

    Fallback: usa GBR com loss=quantile (menos fiel, mas funciona sem deps).

    Retorna array 2D com (Q0.84 - Q0.16) / 2.
    """
    unc_flat = np.full(height * width, np.nan, dtype=np.float32)
    if valid_mask.sum() == 0:
        return unc_flat.reshape(height, width)

    X_valid = X_raster[valid_mask]

    if HAS_QUANTILE_FOREST:
        log_msg(logger, "[→] Treinando RandomForestQuantileRegressor (num.trees = 500)...", "INFO")
        qrf = RandomForestQuantileRegressor(
            n_estimators=n_estimators,
            max_features=max(1, X_train.shape[1] // 3),
            min_samples_leaf=5,
            random_state=random_state,
            n_jobs=-1,
        )
        qrf.fit(X_train, y_train)
        log_msg(logger, "[→] Predizendo quantis 0.16 e 0.84...", "INFO")
        # Retorna shape (n_samples, 2) — uma coluna por quantil
        qpred = qrf.predict(X_valid, quantiles=[0.16, 0.84])
        lo = qpred[:, 0].astype(np.float32)
        hi = qpred[:, 1].astype(np.float32)
    else:
        log_msg(logger, "[!] quantile-forest não disponível — usando GBR quantile (fallback)", "WARNING")
        log_msg(logger, "[→] Treinando GBR quantile 0.16...", "INFO")
        gbr_lo = GradientBoostingRegressor(
            loss="quantile", alpha=0.16,
            n_estimators=200, max_depth=4,
            learning_rate=0.05, random_state=random_state,
        )
        gbr_lo.fit(X_train, y_train)
        log_msg(logger, "[→] Treinando GBR quantile 0.84...", "INFO")
        gbr_hi = GradientBoostingRegressor(
            loss="quantile", alpha=0.84,
            n_estimators=200, max_depth=4,
            learning_rate=0.05, random_state=random_state,
        )
        gbr_hi.fit(X_train, y_train)
        lo = gbr_lo.predict(X_valid).astype(np.float32)
        hi = gbr_hi.predict(X_valid).astype(np.float32)

    unc_flat[valid_mask] = (hi - lo) / 2.0
    return unc_flat.reshape(height, width)


# =============================================================================
# Incerteza: Bootstrap
# =============================================================================

def uncertainty_bootstrap(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_raster: np.ndarray,
    valid_mask: np.ndarray,
    height: int,
    width: int,
    n_bootstrap: int,
    random_state: int,
    logger: logging.Logger,
) -> np.ndarray:
    """Estima incerteza via desvio padrão de N modelos bootstrap."""
    rng = np.random.default_rng(random_state)
    n_valid = int(valid_mask.sum())

    if n_valid == 0:
        return np.full((height, width), np.nan, dtype=np.float32)

    boot_preds = np.zeros((n_bootstrap, n_valid), dtype=np.float32)
    X_valid    = X_raster[valid_mask]

    for b in range(n_bootstrap):
        idx = rng.choice(len(X_train), size=len(X_train), replace=True)
        rf_b = RandomForestRegressor(
            n_estimators=500,
            max_features=max(1, X_train.shape[1] // 3),
            min_samples_leaf=5,
            random_state=b,
            n_jobs=-1,
        )
        rf_b.fit(X_train[idx], y_train[idx])
        boot_preds[b] = rf_b.predict(X_valid).astype(np.float32)

        if (b + 1) % 10 == 0:
            log_msg(logger, f"  [→] Bootstrap: {b + 1}/{n_bootstrap}", "INFO")

    unc_flat = np.full(height * width, np.nan, dtype=np.float32)
    unc_flat[valid_mask] = np.std(boot_preds, axis=0)
    return unc_flat.reshape(height, width)


# =============================================================================
# Salvar raster de saída
# =============================================================================

def save_raster(array: np.ndarray, meta: dict, out_path: Path,
                nodata: float = NODATA_OUT):
    """Salva array 2D como GeoTIFF (single band)."""
    out_meta = meta.copy()
    out_meta.update({
        "count":    1,
        "dtype":    "float32",
        "nodata":   nodata,
        "compress": "lzw",
    })
    arr_out = array.copy().astype(np.float32)
    arr_out[np.isnan(arr_out)] = nodata

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_path, "w", **out_meta) as dst:
        dst.write(arr_out, 1)


def save_raster_multiband(arrays: List[np.ndarray], names: List[str],
                          meta: dict, out_path: Path,
                          nodata: float = NODATA_OUT):
    """Salva múltiplas bandas em um único GeoTIFF (espelha o c() do terra)."""
    out_meta = meta.copy()
    out_meta.update({
        "count":    len(arrays),
        "dtype":    "float32",
        "nodata":   nodata,
        "compress": "lzw",
    })
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_path, "w", **out_meta) as dst:
        for i, arr in enumerate(arrays, start=1):
            a = arr.copy().astype(np.float32)
            a[np.isnan(a)] = nodata
            dst.write(a, i)
            dst.set_band_description(i, names[i - 1])


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Gera isoscape via Random Forest (equiv. run_isoscape.R / 03_integracao_ML.R)"
    )
    parser.add_argument("--job-id",       required=True)
    parser.add_argument("--dataset-path", required=True)
    parser.add_argument("--raster-dir",   required=True)
    parser.add_argument("--output-dir",   required=True)
    parser.add_argument("--response-col", required=True)
    parser.add_argument("--lat-col",      default="latitude")
    parser.add_argument("--lon-col",      default="longitude")
    parser.add_argument("--uncertainty",  default="quantile_rf",
                        choices=["quantile_rf", "bootstrap"])
    parser.add_argument("--resolution",   type=float, default=5)
    parser.add_argument("--seed",         type=int,   default=1350)
    parser.add_argument("--raster-suffix", default="_py",
                        help="Filtro de sufixo no nome dos rasters (default: _py). "
                             "Use '' para todos.")
    parser.add_argument("--extract-method", default="bilinear",
                        choices=["bilinear", "simple"])
    parser.add_argument("--n-estimators-main", type=int, default=2000,
                        help="ntree do RF principal (default 2000 — espelha 03_integracao_ML.R)")

    args = parser.parse_args()

    output_dir = ensure_output_dir(Path(args.output_dir))
    logger     = setup_logging(output_dir, args.job_id, "run_isoscape")
    metrics    = MetricsCollector(args.job_id)

    np.random.seed(args.seed)

    log_msg(logger, f"[→] Job iniciado: {args.job_id}", "INFO")
    log_msg(logger, f"[→] Dataset: {args.dataset_path}", "INFO")
    log_msg(logger, f"[→] Rasters: {args.raster_dir}", "INFO")
    log_msg(logger,
            f"[→] Sufixo de filtro: {args.raster_suffix or '(nenhum — todos)'}",
            "INFO")
    log_msg(logger, f"[→] Resolução: {args.resolution} arc-min", "INFO")
    log_msg(logger, f"[→] Incerteza: {args.uncertainty}", "INFO")
    log_msg(logger, f"[→] Seed: {args.seed}", "INFO")
    log_msg(logger, "[→] Engine: Python", "INFO")
    if not HAS_QUANTILE_FOREST and args.uncertainty == "quantile_rf":
        log_msg(logger,
                "[!] quantile-forest não instalado — incerteza usará GBR fallback. "
                "Para paridade total com o R, instale: pip install quantile-forest",
                "WARNING")

    try:
        # ── 1. Leitura do dataset ──────────────────────────────────────────────
        log_msg(logger, "[→] Lendo dataset...", "INFO")
        df, gdf, response_series = load_dataset(
            args.dataset_path,
            lat_col=args.lat_col,
            lon_col=args.lon_col,
            response_col=args.response_col,
        )
        log_msg(logger,
                f"[✓] Dataset lido: {len(df)} linhas brutas → {len(gdf)} válidas (NA removido)",
                "INFO")

        # ── 2. Carregar rasters ────────────────────────────────────────────────
        log_msg(logger, "[→] Carregando rasters...", "INFO")
        raster_dir   = Path(args.raster_dir)

        # Filtro por sufixo
        if args.raster_suffix:
            raster_files = sorted(raster_dir.glob(f"*{args.raster_suffix}.tif"))
            if not raster_files:
                log_msg(logger,
                        f"[!] Nenhum raster com sufixo '{args.raster_suffix}' — usando todos os .tif",
                        "WARNING")
                raster_files = sorted(raster_dir.glob("*.tif"))
        else:
            raster_files = sorted(raster_dir.glob("*.tif"))

        if not raster_files:
            raise FileNotFoundError(f"Nenhum .tif encontrado em: {raster_dir}")

        log_msg(logger, f"[→] Arquivos .tif encontrados: {len(raster_files)}", "INFO")
        log_msg(logger, f"[→] Primeiro raster: {raster_files[0].name}", "INFO")

        # ── 3. Extração nos pontos (espelho do 02_extr_dados_raster.R) ────────
        log_msg(logger,
                f"[→] Extraindo valores dos rasters nos pontos (method={args.extract_method})...",
                "INFO")
        extracted_df = extract_values_at_points(
            raster_files, gdf, logger, method=args.extract_method,
        )

        # Montar df_model (espelha cbind(dados, extracted[,-1]) + na.omit)
        df_model = pd.concat(
            [gdf[["latitude", "longitude", "response"]].reset_index(drop=True),
             extracted_df.reset_index(drop=True)],
            axis=1,
        ).dropna().reset_index(drop=True)

        if len(df_model) == 0:
            raise ValueError(
                "Nenhuma linha restou após remoção de NAs. "
                "Verifique se os pontos estão dentro da área dos rasters."
            )

        predictor_cols = extracted_df.columns.tolist()
        log_msg(logger,
                f"[✓] Extração concluída: {len(df_model)} linhas, "
                f"{len(predictor_cols)} preditoras: {', '.join(predictor_cols)}",
                "INFO")

        # Salvar dataset combinado (equivalente ao madeira_amz_var_clim.xlsx do curso)
        combined_csv = output_dir / "dataset_with_vars_py.csv"
        df_model.to_csv(combined_csv, index=False)
        log_msg(logger, f"[✓] Dataset combinado salvo: {combined_csv.name}", "INFO")

        X_all = df_model[predictor_cols].values.astype(np.float32)
        y_all = df_model["response"].values.astype(np.float32)

        # ── 4. Seleção de variáveis (VSURFApprox) ─────────────────────────────
        log_msg(logger, "[→] Executando VSURFApprox para seleção de variáveis...", "INFO")
        log_msg(logger,
                "    (ntree=500, nfor.thres=20, nfor.interp=100, nfor.pred=10, nsd=1)",
                "INFO")
        log_msg(logger, "    (esta etapa pode demorar alguns minutos)", "INFO")

        try:
            selector = VSURFApprox(
                ntree=500, nfor_thres=20, nfor_interp=100, nfor_pred=10,
                nsd=1.0, random_state=args.seed,
            )
            selector.fit(X_all, y_all, predictor_cols)
            threshold_vars = selector.threshold_vars_
            interp_vars    = selector.interp_vars_
            pred_vars      = selector.pred_vars_
        except Exception as e:
            log_msg(logger, f"[!] VSURFApprox falhou: {e} — usando todas", "WARNING")
            threshold_vars = predictor_cols
            interp_vars    = predictor_cols
            pred_vars      = predictor_cols

        if not pred_vars:
            log_msg(logger, "[!] Pred vars vazio — usando todas as preditoras", "WARNING")
            pred_vars = predictor_cols

        log_msg(logger, f"[✓] VSURF — Threshold ({len(threshold_vars)}): {', '.join(threshold_vars)}", "INFO")
        log_msg(logger, f"[✓] VSURF — Interp   ({len(interp_vars)}): {', '.join(interp_vars)}", "INFO")
        log_msg(logger, f"[✓] VSURF — Pred     ({len(pred_vars)}): {', '.join(pred_vars)}", "INFO")

        # ── 5. RF principal (ntree=2000, espelha 03_integracao_ML.R) ─────────
        log_msg(logger,
                f"[→] Ajustando Random Forest principal (ntree={args.n_estimators_main})...",
                "INFO")

        X_sel = df_model[pred_vars].values.astype(np.float32)
        y_sel = df_model["response"].values.astype(np.float32)

        rf_main = RandomForestRegressor(
            n_estimators=args.n_estimators_main,
            max_features=max(1, len(pred_vars) // 3),
            min_samples_leaf=5,
            random_state=args.seed,
            n_jobs=-1,
            oob_score=True,
        )
        rf_main.fit(X_sel, y_sel)

        # % Var Explicada via OOB (espelha o "% Var explained" do randomForest::print)
        var_y = float(np.var(y_sel))
        var_explained = 100.0 * (1.0 - float(mean_squared_error(y_sel, rf_main.oob_prediction_)) / var_y) if var_y > 0 else 0.0
        log_msg(logger, f"[✓] RF principal ajustado | % Var Explicada (OOB): {var_explained:.2f}%", "INFO")

        # ── 6. Split treino/teste (caret::createDataPartition p=0.8 stratified) ─
        log_msg(logger,
                "[→] Split treino/teste estratificado (p = 0.8) — espelha caret::createDataPartition...",
                "INFO")
        train_df, test_df = stratified_split_continuous(
            df_model, response_col="response",
            test_size=0.2, random_state=args.seed,
        )
        log_msg(logger,
                f"[→] Treino: {len(train_df)} linhas | Teste: {len(test_df)} linhas",
                "INFO")

        X_train = train_df[pred_vars].values.astype(np.float32)
        y_train = train_df["response"].values.astype(np.float32)
        X_test  = test_df[pred_vars].values.astype(np.float32)
        y_test  = test_df["response"].values.astype(np.float32)

        rf_eval = RandomForestRegressor(
            n_estimators=500,
            max_features=max(1, len(pred_vars) // 3),
            min_samples_leaf=5,
            random_state=args.seed,
            n_jobs=-1,
        )
        rf_eval.fit(X_train, y_train)

        y_pred = rf_eval.predict(X_test)
        mse    = float(mean_squared_error(y_test, y_pred))
        r2     = float(r2_score(y_test, y_pred))
        log_msg(logger, f"[✓] Avaliação no teste — MSE = {mse:.4f} | R² = {r2:.4f}", "INFO")

        # ── 7. Predição espacial (isoscape) ───────────────────────────────────
        log_msg(logger, "[→] Gerando isoscape (predição espacial)...", "INFO")

        X_raster, raster_meta, valid_mask, height, width = build_raster_design_matrix(
            raster_files, pred_vars, logger,
        )
        iso_2d = predict_raster(X_raster, valid_mask, height, width, rf_main)
        log_msg(logger,
                f"[✓] Isoscape gerado | pixels válidos: {int(valid_mask.sum()):,}",
                "INFO")

        # ── 8. Mapa de incerteza ──────────────────────────────────────────────
        log_msg(logger, f"[→] Calculando incerteza via {args.uncertainty}...", "INFO")

        if args.uncertainty == "quantile_rf":
            unc_2d = uncertainty_quantile_rf(
                X_sel, y_sel, X_raster, valid_mask,
                height, width, args.seed, logger,
                n_estimators=500,
            )
        else:
            unc_2d = uncertainty_bootstrap(
                X_sel, y_sel, X_raster, valid_mask,
                height, width,
                n_bootstrap=50,
                random_state=args.seed,
                logger=logger,
            )

        log_msg(logger, "[✓] Mapa de incerteza gerado", "INFO")

        # ── 9. Salvar outputs (sufixo _py) ────────────────────────────────────
        log_msg(logger, "[→] Salvando rasters...", "INFO")

        iso_path      = output_dir / "isoscape_py.tif"
        unc_path      = output_dir / "uncertainty_py.tif"
        combined_path = output_dir / "isoscape_combined_py.tif"

        save_raster(iso_2d, raster_meta, iso_path)
        save_raster(unc_2d, raster_meta, unc_path)
        # Combined (2 bandas) — espelha c(isoscape$lyr1, isoscape$sd) do curso
        save_raster_multiband(
            [iso_2d, unc_2d],
            ["isoscape", "sd"],
            raster_meta, combined_path,
        )

        log_msg(logger, f"[✓] isoscape_py.tif salvo em: {iso_path}", "INFO")
        log_msg(logger, f"[✓] uncertainty_py.tif salvo em: {unc_path}", "INFO")
        log_msg(logger, f"[✓] isoscape_combined_py.tif (2 bandas) em: {combined_path}", "INFO")

        # ── 10. Importância das variáveis ─────────────────────────────────────
        imp_df = pd.DataFrame({
            "variable": pred_vars,
            "importance": rf_main.feature_importances_,
        }).sort_values("importance", ascending=False)
        imp_path = output_dir / "variable_importance_py.csv"
        imp_df.to_csv(imp_path, index=False)
        log_msg(logger, "[✓] variable_importance_py.csv salvo", "INFO")

        # ── 11. metrics.json ──────────────────────────────────────────────────
        metrics.add("engine",            "python")
        metrics.add("MSE",                mse)
        metrics.add("R2",                 r2)
        metrics.add("var_explained_oob",  var_explained)
        metrics.add("threshold_vars",     threshold_vars)
        metrics.add("interp_vars",        interp_vars)
        metrics.add("pred_vars",          pred_vars)
        metrics.add("isoscape_path",      str(iso_path))
        metrics.add("uncertainty_path",   str(unc_path))
        metrics.add("combined_path",      str(combined_path))
        metrics.add("importance_path",    str(imp_path))
        metrics.add("dataset_extracted",  str(combined_csv))
        metrics.add("n_samples",          len(df_model))
        metrics.add("n_train",            len(train_df))
        metrics.add("n_test",             len(test_df))
        metrics.add("quantile_forest_used", HAS_QUANTILE_FOREST and args.uncertainty == "quantile_rf")

        metrics.save(output_dir / "metrics.json")
        log_msg(logger, f"[✓] metrics.json salvo em: {output_dir / 'metrics.json'}", "INFO")
        log_msg(logger, "[★] Job concluído com sucesso", "INFO")
        sys.exit(0)

    except Exception as e:
        import traceback
        log_msg(logger, f"[!] Erro fatal: {e}", "ERROR")
        log_msg(logger, traceback.format_exc(), "ERROR")
        metrics.add("error", str(e))
        metrics.save(output_dir / "metrics.json")
        sys.exit(1)


if __name__ == "__main__":
    main()