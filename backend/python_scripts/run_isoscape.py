#!/usr/bin/env python
"""
backend/python_scripts/run_isoscape.py

Equivalente Python do run_isoscape.R.
Treina Random Forest sobre amostras + rasters preditores e gera isoscape.

Paridade com o R:
  - VSURF   → RecursiveFeatureEliminator (sklearn RF importances)
  - ranger quantile_rf → GradientBoostingRegressor com quantile loss (sklearn)
  - bootstrap         → 50 RFs treinados em amostras bootstrap

Uso:
    python run_isoscape.py \\
      --job-id abc-123 \\
      --dataset-path /data/datasets/amostras.csv \\
      --raster-dir   /data/rasters/project1/ \\
      --output-dir   /data/isoscapes/abc-123/ \\
      --response-col d13C \\
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
import rioxarray
import xarray as xr
from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_squared_error, r2_score

sys.path.insert(0, str(Path(__file__).parent.parent))

from python_scripts.utils import (
    setup_logging, log_msg, MetricsCollector,
    load_dataset, ensure_output_dir
)

warnings.filterwarnings("ignore")

TARGET_CRS = "EPSG:4674"


# =============================================================================
# Seleção de variáveis — equivalente ao VSURF
# =============================================================================

class RecursiveFeatureEliminator:
    """
    Eliminação recursiva de features com Random Forest.
    Espelha o comportamento do VSURF (threshold → interp → pred).

    Estratégia:
      1. Treina RF completo → importâncias
      2. Elimina features com importância < threshold (média/10)
      3. Itera até convergir ou atingir n_min
    """

    def __init__(self, n_min: int = 3, n_estimators: int = 300, random_state: int = 42):
        self.n_min        = n_min
        self.n_estimators = n_estimators
        self.random_state = random_state
        self.selected_    = []
        self.importances_ = {}

    def fit(self, X: np.ndarray, y: np.ndarray, feature_names: List[str]) -> "RecursiveFeatureEliminator":
        features = list(feature_names)
        X_df = pd.DataFrame(X, columns=feature_names)

        for iteration in range(20):  # máximo 20 iterações
            if len(features) <= self.n_min:
                break

            X_cur = X_df[features].values
            rf = RandomForestRegressor(
                n_estimators=self.n_estimators,
                max_features="sqrt",
                min_samples_leaf=5,
                random_state=self.random_state,
                n_jobs=-1,
            )
            rf.fit(X_cur, y)

            imp = dict(zip(features, rf.feature_importances_))
            threshold = np.mean(list(imp.values())) / 10.0

            new_features = [f for f in features if imp[f] >= threshold]
            if len(new_features) < self.n_min:
                new_features = sorted(features, key=lambda f: imp[f], reverse=True)[: self.n_min]

            if set(new_features) == set(features):
                break  # Convergiu

            features = new_features

        # RF final com features selecionadas
        X_final = X_df[features].values
        rf_final = RandomForestRegressor(
            n_estimators=500,
            max_features="sqrt",
            min_samples_leaf=5,
            random_state=self.random_state,
            n_jobs=-1,
        )
        rf_final.fit(X_final, y)

        self.importances_ = dict(zip(features, rf_final.feature_importances_))
        self.selected_ = sorted(features, key=lambda f: self.importances_[f], reverse=True)
        return self

    def get_selected(self) -> List[str]:
        return self.selected_


# =============================================================================
# Extração de valores nos pontos amostrais
# =============================================================================

def extract_values_at_points(
    raster_paths: List[Path],
    gdf: gpd.GeoDataFrame,
    logger: logging.Logger,
) -> pd.DataFrame:
    """
    Extrai valores dos rasters nos pontos do GeoDataFrame.
    Usa rasterio para ler pixel por pixel (mais robusto que rioxarray.sel).

    Returns:
        DataFrame com uma coluna por raster (nome = stem do arquivo).
    """
    records = {r.stem: [] for r in raster_paths}

    for raster_path in raster_paths:
        with rasterio.open(raster_path) as src:
            # Reprojetar pontos para o CRS do raster
            points_reproj = gdf.to_crs(src.crs)
            nodata = src.nodata if src.nodata is not None else -9999

            vals = []
            for geom in points_reproj.geometry:
                try:
                    row, col = rowcol(src.transform, geom.x, geom.y)
                    # Checar bounds
                    if 0 <= row < src.height and 0 <= col < src.width:
                        val = src.read(1)[row, col]
                        vals.append(float(val) if val != nodata else np.nan)
                    else:
                        vals.append(np.nan)
                except Exception:
                    vals.append(np.nan)

            records[raster_path.stem] = vals

    return pd.DataFrame(records)


# =============================================================================
# Predição espacial
# =============================================================================

def predict_raster(
    raster_paths: List[Path],
    feature_names: List[str],
    rf_model: RandomForestRegressor,
    logger: logging.Logger,
) -> Tuple[np.ndarray, dict, np.ndarray]:
    """
    Faz predição espacial usando o RF sobre o stack de rasters.

    Returns:
        (pred_2d, raster_meta, valid_mask_2d)
        valid_mask_2d: pixels que tinham dados em todos os preditores
    """
    # Usa o primeiro raster como referência de grade/meta
    ref_path = next(p for p in raster_paths if p.stem == feature_names[0])
    with rasterio.open(ref_path) as src:
        meta     = src.meta.copy()
        nodata   = src.nodata if src.nodata is not None else -9999
        height   = src.height
        width    = src.width

    n_pixels = height * width
    X_raster = np.full((n_pixels, len(feature_names)), np.nan, dtype=np.float32)

    path_map = {p.stem: p for p in raster_paths}

    for i, feat in enumerate(feature_names):
        if feat not in path_map:
            log_msg(logger, f"  [!] Raster para '{feat}' não encontrado — preenchido com NaN", "WARNING")
            continue
        with rasterio.open(path_map[feat]) as src:
            nd = src.nodata if src.nodata is not None else -9999
            arr = src.read(1).astype(np.float32).flatten()
            arr[arr == nd] = np.nan
            X_raster[:, i] = arr

    # Máscara de pixels válidos (sem NaN em nenhum preditor)
    valid_mask = ~np.any(np.isnan(X_raster), axis=1)

    pred_flat = np.full(n_pixels, np.nan, dtype=np.float32)
    if valid_mask.sum() > 0:
        pred_flat[valid_mask] = rf_model.predict(X_raster[valid_mask]).astype(np.float32)

    pred_2d      = pred_flat.reshape(height, width)
    valid_mask2d = valid_mask.reshape(height, width)

    return pred_2d, meta, valid_mask2d, X_raster


# =============================================================================
# Incerteza: Quantile RF (via GBR com quantile loss — sklearn)
# =============================================================================

def uncertainty_quantile_rf(
    raster_paths: List[Path],
    feature_names: List[str],
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_raster: np.ndarray,
    valid_mask: np.ndarray,
    height: int,
    width:  int,
    random_state: int,
    logger: logging.Logger,
) -> np.ndarray:
    """
    Estima incerteza via quantile regression (quantis 0.16 e 0.84 ≈ ±1σ).
    Usa GradientBoostingRegressor com loss='quantile' (sklearn nativo).

    Retorna array 2D com (q84 - q16) / 2.
    """
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

    unc_flat = np.full(height * width, np.nan, dtype=np.float32)
    if valid_mask.sum() > 0:
        lo = gbr_lo.predict(X_raster[valid_mask]).astype(np.float32)
        hi = gbr_hi.predict(X_raster[valid_mask]).astype(np.float32)
        unc_flat[valid_mask] = (hi - lo) / 2.0

    return unc_flat.reshape(height, width)


# =============================================================================
# Incerteza: Bootstrap
# =============================================================================

def uncertainty_bootstrap(
    raster_paths: List[Path],
    feature_names: List[str],
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_raster: np.ndarray,
    valid_mask: np.ndarray,
    height: int,
    width:  int,
    n_bootstrap: int,
    random_state: int,
    logger: logging.Logger,
) -> np.ndarray:
    """
    Estima incerteza via desvio padrão de 50 modelos bootstrap.
    """
    np.random.seed(random_state)
    n_valid = valid_mask.sum()

    if n_valid == 0:
        return np.full((height, width), np.nan, dtype=np.float32)

    boot_preds = np.zeros((n_bootstrap, n_valid), dtype=np.float32)
    X_valid    = X_raster[valid_mask]

    for b in range(n_bootstrap):
        idx   = np.random.choice(len(X_train), size=len(X_train), replace=True)
        X_b   = X_train[idx]
        y_b   = y_train[idx]
        rf_b  = RandomForestRegressor(
            n_estimators=200, max_features="sqrt",
            min_samples_leaf=5, random_state=b, n_jobs=-1
        )
        rf_b.fit(X_b, y_b)
        boot_preds[b] = rf_b.predict(X_valid).astype(np.float32)

        if (b + 1) % 10 == 0:
            log_msg(logger, f"  [→] Bootstrap: {b + 1}/{n_bootstrap}", "INFO")

    unc_flat = np.full(height * width, np.nan, dtype=np.float32)
    unc_flat[valid_mask] = np.std(boot_preds, axis=0)
    return unc_flat.reshape(height, width)


# =============================================================================
# Salvar raster de saída
# =============================================================================

def save_raster(array: np.ndarray, meta: dict, out_path: Path, nodata: float = -9999.0):
    """Salva array 2D como GeoTIFF."""
    out_meta = meta.copy()
    out_meta.update({
        "count":    1,
        "dtype":    "float32",
        "nodata":   nodata,
        "compress": "lzw",
    })
    arr_out = array.copy()
    arr_out[np.isnan(arr_out)] = nodata

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_path, "w", **out_meta) as dst:
        dst.write(arr_out, 1)


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Gera isoscape via Random Forest (equiv. run_isoscape.R)"
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

    args = parser.parse_args()

    output_dir = ensure_output_dir(Path(args.output_dir))
    logger     = setup_logging(output_dir, args.job_id, "run_isoscape")
    metrics    = MetricsCollector(args.job_id)

    np.random.seed(args.seed)

    log_msg(logger, f"[→] Job iniciado: {args.job_id}", "INFO")
    log_msg(logger, f"[→] Dataset: {args.dataset_path}", "INFO")
    log_msg(logger, f"[→] Rasters: {args.raster_dir}", "INFO")
    log_msg(logger, f"[→] Resolução: {args.resolution} arc-min", "INFO")
    log_msg(logger, f"[→] Incerteza: {args.uncertainty}", "INFO")

    try:
        # ── 1. Leitura do dataset ──────────────────────────────────────────────
        log_msg(logger, "[→] Lendo dataset...", "INFO")
        df, gdf, response_series = load_dataset(
            args.dataset_path,
            lat_col=args.lat_col,
            lon_col=args.lon_col,
            response_col=args.response_col,
        )
        log_msg(logger, f"[✓] Dataset lido: {len(df)} linhas", "INFO")

        # ── 2. Carregar rasters ────────────────────────────────────────────────
        log_msg(logger, "[→] Carregando rasters...", "INFO")
        raster_dir   = Path(args.raster_dir)
        raster_files = sorted(raster_dir.glob("*.tif"))

        if not raster_files:
            raise FileNotFoundError(f"Nenhum .tif encontrado em: {raster_dir}")

        log_msg(logger, f"[→] Arquivos .tif encontrados: {len(raster_files)}", "INFO")
        log_msg(logger, f"[→] Primeiro raster: {raster_files[0].name}", "INFO")
        log_msg(logger, f"[✓] Rasters carregados: {len(raster_files)} camadas", "INFO")

        # ── 3. Extração nos pontos ─────────────────────────────────────────────
        log_msg(logger, "[→] Extraindo valores dos rasters nos pontos amostrais...", "INFO")
        extracted_df = extract_values_at_points(raster_files, gdf, logger)

        # Montar df_model
        df_model = pd.concat(
            [gdf[["latitude", "longitude", "response"]].reset_index(drop=True),
             extracted_df.reset_index(drop=True)],
            axis=1,
        ).dropna()

        if len(df_model) == 0:
            raise ValueError(
                "Nenhuma linha restou após remoção de NAs. "
                "Verifique se os pontos estão dentro da área dos rasters."
            )

        predictor_cols = extracted_df.columns.tolist()
        log_msg(logger, f"[✓] Extração concluída: {len(df_model)} linhas, "
                        f"{len(predictor_cols)} preditoras", "INFO")

        X_all = df_model[predictor_cols].values
        y_all = df_model["response"].values

        # ── 4. Seleção de variáveis (VSURF equiv.) ─────────────────────────────
        log_msg(logger, "[→] Executando seleção de variáveis (RFE equiv. VSURF)...", "INFO")
        log_msg(logger, "    (esta etapa pode demorar alguns minutos)", "INFO")

        selector = RecursiveFeatureEliminator(
            n_min=3, n_estimators=300, random_state=args.seed
        )
        try:
            selector.fit(X_all, y_all, predictor_cols)
            pred_vars = selector.get_selected()
        except Exception as e:
            log_msg(logger, f"[!] Seleção de variáveis falhou: {e} — usando todas", "WARNING")
            pred_vars = predictor_cols

        log_msg(logger, f"[✓] Variáveis selecionadas ({len(pred_vars)}): "
                        f"{', '.join(pred_vars)}", "INFO")

        # ── 5. Treinar Random Forest ───────────────────────────────────────────
        log_msg(logger, "[→] Ajustando Random Forest (ntree=500)...", "INFO")

        X_sel = df_model[pred_vars].values
        y_sel = df_model["response"].values

        X_train, X_test, y_train, y_test = train_test_split(
            X_sel, y_sel, test_size=0.2, random_state=args.seed
        )

        rf_final = RandomForestRegressor(
            n_estimators=500,
            max_features="sqrt",
            min_samples_leaf=5,
            random_state=args.seed,
            n_jobs=-1,
        )
        rf_final.fit(X_train, y_train)

        y_pred = rf_final.predict(X_test)
        mse    = float(mean_squared_error(y_test, y_pred))
        r2     = float(r2_score(y_test, y_pred))

        log_msg(logger, f"[✓] RF ajustado. MSE = {mse:.4f} | R² = {r2:.4f}", "INFO")

        # ── 6. Predição espacial ───────────────────────────────────────────────
        log_msg(logger, "[→] Gerando isoscape (predição espacial)...", "INFO")

        # Filtrar apenas os rasters dos preditores selecionados
        path_map    = {p.stem: p for p in raster_files}
        pred_paths  = [path_map[v] for v in pred_vars if v in path_map]

        if not pred_paths:
            raise ValueError("Nenhum raster encontrado para as variáveis selecionadas.")

        pred_2d, raster_meta, valid_mask, X_raster = predict_raster(
            pred_paths, pred_vars, rf_final, logger
        )
        height, width = pred_2d.shape
        log_msg(logger, "[✓] Isoscape gerado", "INFO")

        # ── 7. Mapa de incerteza ───────────────────────────────────────────────
        log_msg(logger, f"[→] Calculando incerteza via {args.uncertainty}...", "INFO")

        X_train_full, _, y_train_full, _ = train_test_split(
            X_sel, y_sel, test_size=0.2, random_state=args.seed
        )

        if args.uncertainty == "quantile_rf":
            unc_2d = uncertainty_quantile_rf(
                pred_paths, pred_vars,
                X_train_full, y_train_full,
                X_raster, valid_mask,
                height, width,
                args.seed, logger,
            )
        else:
            unc_2d = uncertainty_bootstrap(
                pred_paths, pred_vars,
                X_train_full, y_train_full,
                X_raster, valid_mask,
                height, width,
                n_bootstrap=50,
                random_state=args.seed,
                logger=logger,
            )

        log_msg(logger, "[✓] Mapa de incerteza gerado", "INFO")

        # ── 8. Salvar outputs ─────────────────────────────────────────────────
        log_msg(logger, "[→] Salvando rasters...", "INFO")

        iso_path = output_dir / "isoscape.tif"
        unc_path = output_dir / "uncertainty.tif"

        save_raster(pred_2d, raster_meta, iso_path)
        save_raster(unc_2d,  raster_meta, unc_path)

        log_msg(logger, f"[✓] isoscape.tif salvo em: {iso_path}", "INFO")
        log_msg(logger, f"[✓] uncertainty.tif salvo em: {unc_path}", "INFO")

        # ── 9. Salvar metrics.json ─────────────────────────────────────────────
        metrics.add("MSE",              mse)
        metrics.add("R2",               r2)
        metrics.add("threshold_vars",   [])       # compatibilidade com VSURF
        metrics.add("interp_vars",      [])
        metrics.add("pred_vars",        pred_vars)
        metrics.add("isoscape_path",    str(iso_path))
        metrics.add("uncertainty_path", str(unc_path))

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