"""
compare_engines.py
==================

Compara as saídas do run_isoscape para as engines R e Python contra o
ground truth (coluna `response` = d13C_wood medido nas amostras).

Mostra três análises:

  1. ETAPA DE EXTRAÇÃO: valores extraídos dos rasters nos pontos amostrais
     (R vs Python no mesmo ponto). Mede se a etapa pré-modelo é consistente
     entre engines.

  2. PREDIÇÃO DO RF vs MEDIDO: para cada amostra com ground truth disponível,
     compara o `d13C_wood` medido com a predição do RF (extraída do isoscape
     nas mesmas coordenadas). Calcula MSE, MAE, R², bias e ranking.

  3. COMPARAÇÃO PIXEL-A-PIXEL DO ISOSCAPE: estatísticas globais dos rasters
     (mean, sd, range) e diferença média entre os mapas R e Python.

Uso (dentro do `make shell` ou direto no worker):
    python compare_engines.py <iso_r_id> <iso_py_id>

Ex:
    python compare_engines.py 16 17
"""

import sys
import json
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import rowcol


ISO_BASE = Path("/data/isoscapes")


# =============================================================================
# Helpers
# =============================================================================

def find_csv(job_dir: Path) -> Path | None:
    """Acha o dataset_with_vars_{engine}.csv dentro do diretório do job."""
    for name in ("dataset_with_vars_r.csv", "dataset_with_vars_py.csv"):
        p = job_dir / name
        if p.exists():
            return p
    return None


def find_isoscape(job_dir: Path) -> Path | None:
    for name in ("isoscape_r.tif", "isoscape_py.tif"):
        p = job_dir / name
        if p.exists():
            return p
    return None


def load_metrics(job_dir: Path) -> dict:
    p = job_dir / "metrics.json"
    return json.loads(p.read_text()) if p.exists() else {}


def normalize_colnames(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remove o sufixo de engine das colunas preditoras para que R e Python
    fiquem comparáveis: 'amazonia_legal_10arc_tavg_mean_r' → 'tavg_mean'
    e 'amazonia_legal_10arc_tavg_mean_py' → 'tavg_mean'.
    """
    def clean(col: str) -> str:
        c = col
        for suf in ("_r", "_py"):
            if c.endswith(suf):
                c = c[: -len(suf)]
                break
        # Tira o prefixo do shapefile e resolução (10arc, 5arc, etc.)
        # Mantém apenas a parte da variável (tavg_mean, bio1, etc.)
        import re
        m = re.search(r"_\d+(?:\.\d+)?arc_(.+)$", c)
        return m.group(1) if m else c
    return df.rename(columns={c: clean(c) for c in df.columns})


def sample_raster_at(raster_path: Path, df: pd.DataFrame,
                     lat_col: str = "latitude",
                     lon_col: str = "longitude") -> np.ndarray:
    """
    Faz amostragem do raster nos pontos (lon, lat) do df.
    Retorna array de mesmo tamanho que df, com NaN onde estiver fora ou nodata.
    """
    vals = np.full(len(df), np.nan, dtype=np.float64)
    if not raster_path.exists():
        return vals

    with rasterio.open(raster_path) as src:
        nodata = src.nodata if src.nodata is not None else -9999.0
        # Reprojetar pontos para o CRS do raster, se necessário
        import geopandas as gpd
        gdf = gpd.GeoDataFrame(
            df, geometry=gpd.points_from_xy(df[lon_col], df[lat_col]),
            crs="EPSG:4674",
        )
        if gdf.crs != src.crs:
            gdf = gdf.to_crs(src.crs)

        band = src.read(1)
        for i, geom in enumerate(gdf.geometry):
            try:
                row, col = rowcol(src.transform, geom.x, geom.y)
                if 0 <= row < src.height and 0 <= col < src.width:
                    v = band[row, col]
                    if v != nodata and not np.isnan(v):
                        vals[i] = float(v)
            except Exception:
                pass
    return vals


def metrics_summary(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    """MSE, RMSE, MAE, R², bias, n válidos."""
    mask = ~np.isnan(y_true) & ~np.isnan(y_pred)
    if mask.sum() < 2:
        return {"n": int(mask.sum())}
    yt = y_true[mask]; yp = y_pred[mask]
    err = yp - yt
    ss_res = float(np.sum(err ** 2))
    ss_tot = float(np.sum((yt - yt.mean()) ** 2))
    return {
        "n":    int(mask.sum()),
        "MSE":  float(np.mean(err ** 2)),
        "RMSE": float(np.sqrt(np.mean(err ** 2))),
        "MAE":  float(np.mean(np.abs(err))),
        "R2":   float(1 - ss_res / ss_tot) if ss_tot > 0 else float("nan"),
        "bias": float(np.mean(err)),    # médio (pred - real). Negativo = subestima.
    }


# =============================================================================
# Main
# =============================================================================

def main(iso_r_id: int, iso_py_id: int):
    dir_r  = ISO_BASE / str(iso_r_id)
    dir_py = ISO_BASE / str(iso_py_id)

    print("=" * 70)
    print(f"Comparando: R job {iso_r_id} vs Python job {iso_py_id}")
    print("=" * 70)

    # ── Localizar CSVs e isoscapes ────────────────────────────────────────────
    csv_r  = find_csv(dir_r)
    csv_py = find_csv(dir_py)
    iso_r  = find_isoscape(dir_r)
    iso_py = find_isoscape(dir_py)

    if csv_r is None or csv_py is None:
        print(f"[!] CSV não encontrado: R={csv_r} Py={csv_py}")
        sys.exit(1)

    print(f"\n[→] CSV R:    {csv_r}")
    print(f"[→] CSV Py:   {csv_py}")
    print(f"[→] ISO R:    {iso_r}")
    print(f"[→] ISO Py:   {iso_py}")

    df_r  = pd.read_csv(csv_r)
    df_py = pd.read_csv(csv_py)
    df_r_n  = normalize_colnames(df_r)
    df_py_n = normalize_colnames(df_py)

    print(f"\n[✓] R:  {len(df_r)} linhas | cols: {list(df_r_n.columns)}")
    print(f"[✓] Py: {len(df_py)} linhas | cols: {list(df_py_n.columns)}")

    # ─────────────────────────────────────────────────────────────────────────
    # 1. EXTRAÇÃO: comparar valores extraídos dos rasters (R vs Python)
    # ─────────────────────────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("1. ETAPA DE EXTRAÇÃO — R vs Python (mesmas amostras, mesmos pontos)")
    print("─" * 70)

    # Join pelas coordenadas (latitude, longitude, response)
    key = ["latitude", "longitude", "response"]
    merged = df_r_n.merge(df_py_n, on=key, suffixes=("_R", "_PY"), how="inner")
    print(f"[→] Amostras em comum (mesmo lat/lon/response): {len(merged)}")
    if len(merged) == 0:
        print("[!] Sem interseção — verifique se os CSVs vieram do mesmo dataset")

    # Para cada coluna preditora compartilhada, calcular diff
    preds_r  = [c for c in df_r_n.columns  if c not in key]
    preds_py = [c for c in df_py_n.columns if c not in key]
    common_preds = sorted(set(preds_r) & set(preds_py))
    print(f"[→] Preditoras em comum: {common_preds}")

    if common_preds and len(merged) > 0:
        print(f"\n{'preditora':<25}{'mean R':>12}{'mean Py':>12}{'diff mean':>12}{'diff max':>12}")
        for p in common_preds:
            col_r  = f"{p}_R"  if f"{p}_R"  in merged.columns else p
            col_py = f"{p}_PY" if f"{p}_PY" in merged.columns else p
            if col_r not in merged.columns or col_py not in merged.columns:
                continue
            a = merged[col_r].values
            b = merged[col_py].values
            mask = ~np.isnan(a) & ~np.isnan(b)
            if mask.sum() == 0:
                continue
            diff = np.abs(a[mask] - b[mask])
            print(f"{p:<25}{np.nanmean(a):>12.4f}{np.nanmean(b):>12.4f}"
                  f"{np.nanmean(diff):>12.6f}{np.nanmax(diff):>12.6f}")

    # ─────────────────────────────────────────────────────────────────────────
    # 2. ISOSCAPE PREDITO vs MEDIDO (qual engine prediz melhor o real)
    # ─────────────────────────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("2. PREDIÇÃO DO ISOSCAPE vs d13C_wood MEDIDO")
    print("   (extrai isoscape_*.tif nos pontos amostrais e compara com 'response')")
    print("─" * 70)

    if iso_r is None or iso_py is None:
        print("[!] Algum isoscape_*.tif não foi encontrado — pulando")
    else:
        # Pegar o dataset com ground truth — qualquer um serve, são o mesmo
        df_gt = df_r_n[key].copy()
        df_gt["pred_R"]  = sample_raster_at(iso_r,  df_gt)
        df_gt["pred_PY"] = sample_raster_at(iso_py, df_gt)

        y_true = df_gt["response"].values
        m_r  = metrics_summary(y_true, df_gt["pred_R"].values)
        m_py = metrics_summary(y_true, df_gt["pred_PY"].values)

        print(f"\n{'métrica':<10}{'R':>15}{'Python':>15}{'vencedor':>15}")
        for metric in ("n", "MSE", "RMSE", "MAE", "R2", "bias"):
            vr  = m_r.get(metric)
            vpy = m_py.get(metric)
            if vr is None or vpy is None:
                continue
            # Vencedor: menor é melhor (MSE/RMSE/MAE/|bias|), maior é melhor (R²)
            if metric == "R2":
                winner = "R" if vr > vpy else ("Py" if vpy > vr else "=")
            elif metric == "bias":
                winner = "R" if abs(vr) < abs(vpy) else ("Py" if abs(vpy) < abs(vr) else "=")
            elif metric == "n":
                winner = ""
            else:
                winner = "R" if vr < vpy else ("Py" if vpy < vr else "=")
            print(f"{metric:<10}{vr:>15.4f}{vpy:>15.4f}{winner:>15}")

        # Distribuição residual
        print("\nResíduos (pred - real):")
        for label, pred_col in (("R", "pred_R"), ("Python", "pred_PY")):
            resid = df_gt[pred_col].values - y_true
            mask = ~np.isnan(resid)
            print(f"  {label:<8}n={mask.sum():>4}  "
                  f"min={np.min(resid[mask]):>7.3f}  "
                  f"max={np.max(resid[mask]):>7.3f}  "
                  f"|resid|>2: {int(np.sum(np.abs(resid[mask]) > 2))}")

        # Tabela top-10 maiores discordâncias entre as engines
        df_gt["abs_diff"] = np.abs(df_gt["pred_R"] - df_gt["pred_PY"])
        worst = df_gt.dropna(subset=["pred_R", "pred_PY"]).nlargest(10, "abs_diff")
        if len(worst) > 0:
            print("\nTop 10 amostras com MAIOR discordância entre as engines:")
            print(worst[["latitude", "longitude", "response",
                         "pred_R", "pred_PY", "abs_diff"]]
                  .to_string(index=False,
                             float_format=lambda x: f"{x:.3f}"))

    # ─────────────────────────────────────────────────────────────────────────
    # 3. ESTATÍSTICAS GLOBAIS DOS ISOSCAPES (pixel-a-pixel)
    # ─────────────────────────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("3. ESTATÍSTICAS GLOBAIS DO ISOSCAPE (todos os pixels)")
    print("─" * 70)

    if iso_r and iso_py:
        def raster_stats(p: Path) -> dict:
            with rasterio.open(p) as src:
                arr = src.read(1).astype(np.float64)
                nd  = src.nodata if src.nodata is not None else -9999.0
                arr[arr == nd] = np.nan
                return {
                    "shape": arr.shape,
                    "n_valid": int(np.sum(~np.isnan(arr))),
                    "mean": float(np.nanmean(arr)),
                    "std":  float(np.nanstd(arr)),
                    "min":  float(np.nanmin(arr)),
                    "max":  float(np.nanmax(arr)),
                }

        s_r  = raster_stats(iso_r)
        s_py = raster_stats(iso_py)

        print(f"\n{'estat':<10}{'R':>15}{'Python':>15}")
        for k in ("shape", "n_valid", "mean", "std", "min", "max"):
            vr  = s_r[k];  vpy = s_py[k]
            if isinstance(vr, tuple):
                print(f"{k:<10}{str(vr):>15}{str(vpy):>15}")
            else:
                print(f"{k:<10}{vr:>15.4f}{vpy:>15.4f}")

        # Diff pixel-a-pixel se shapes baterem
        if s_r["shape"] == s_py["shape"]:
            with rasterio.open(iso_r) as r, rasterio.open(iso_py) as p:
                a = r.read(1).astype(np.float64)
                b = p.read(1).astype(np.float64)
                a[a == (r.nodata or -9999)] = np.nan
                b[b == (p.nodata or -9999)] = np.nan
                d = np.abs(a - b)
                mask = ~np.isnan(d)
                print(f"\nDiferença pixel-a-pixel |R - Py|:")
                print(f"  pixels comparáveis: {mask.sum()}")
                print(f"  diff mean: {np.nanmean(d):.4f}")
                print(f"  diff std:  {np.nanstd(d):.4f}")
                print(f"  diff p95:  {np.nanpercentile(d[mask], 95):.4f}")
                print(f"  diff max:  {np.nanmax(d):.4f}")

    # ─────────────────────────────────────────────────────────────────────────
    # 4. METRICS.JSON: variáveis selecionadas e MSE/R² internos
    # ─────────────────────────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("4. METRICS.JSON — variáveis selecionadas pelo VSURF e métricas internas")
    print("─" * 70)

    mr  = load_metrics(dir_r)
    mpy = load_metrics(dir_py)

    for label, m in (("R", mr), ("Python", mpy)):
        print(f"\n[{label}]")
        print(f"  pred_vars:      {m.get('pred_vars')}")
        print(f"  threshold_vars: {m.get('threshold_vars')}")
        print(f"  MSE (interno):  {m.get('MSE')}")
        print(f"  R²  (interno):  {m.get('R2')}")
        print(f"  n_samples:      {m.get('n_samples')}")

    # Veredicto final
    print("\n" + "=" * 70)
    print("VEREDICTO")
    print("=" * 70)
    if iso_r and iso_py:
        if m_r.get("n", 0) > 0 and m_py.get("n", 0) > 0:
            score_r  = (m_r["RMSE"], -m_r["R2"], abs(m_r["bias"]))
            score_py = (m_py["RMSE"], -m_py["R2"], abs(m_py["bias"]))
            if score_r < score_py:
                print(f"→ R prediz o d13C_wood medido MELHOR")
                print(f"  R:      RMSE={m_r['RMSE']:.3f}  R²={m_r['R2']:.3f}  bias={m_r['bias']:+.3f}")
                print(f"  Python: RMSE={m_py['RMSE']:.3f}  R²={m_py['R2']:.3f}  bias={m_py['bias']:+.3f}")
            elif score_py < score_r:
                print(f"→ Python prediz o d13C_wood medido MELHOR")
                print(f"  Python: RMSE={m_py['RMSE']:.3f}  R²={m_py['R2']:.3f}  bias={m_py['bias']:+.3f}")
                print(f"  R:      RMSE={m_r['RMSE']:.3f}  R²={m_r['R2']:.3f}  bias={m_r['bias']:+.3f}")
            else:
                print("→ Empate técnico")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Uso: python compare_engines.py <iso_r_id> <iso_py_id>")
        print("Ex:  python compare_engines.py 16 17")
        sys.exit(1)
    main(int(sys.argv[1]), int(sys.argv[2]))