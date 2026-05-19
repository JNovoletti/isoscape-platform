#!/usr/bin/env python
"""
backend/python_scripts/run_assign.py

Equivalente Python do run_assign.R (e do 04_assign.R do curso).
Atribuição de origem geográfica para amostras desconhecidas usando
isoscape + Teorema de Bayes.

Como o pacote `assignR` é R-only, esta implementação reproduz manualmente
a lógica de pdRaster() / qtlRaster() / oddsRatio() usando a suposição de
normalidade (pixel ~ N(μ_isoscape, σ_isoscape)) — exatamente como o
assignR faz internamente quando recebe um isoscape com 2 bandas
(predição, sd).

Saídas (sufixo "_py"):
  pd_map_<sample_id>_py.tif         -- mapa de densidade posterior
  qtl_area_<sample_id>_py.tif       -- threshold por área (default 0.5)
  qtl_prob_<sample_id>_py.tif       -- threshold por prob acumulada (0.95)
  odds_ratios_py.csv                -- razão de chances entre regiões
  posterior_probs_py.csv            -- probabilidade posterior por região
  metrics.json

Uso:
    python run_assign.py \\
      --job-id          abc-123 \\
      --isoscape-path   /data/isoscapes/X/isoscape_combined_py.tif \\
      --unknown-path    /data/datasets/unknowns.csv \\
      --regions-shp     /data/shapefiles/fu.shp \\
      --regions-field   ADM1_PT \\
      --regions-filter  "Amazonas,Mato Grosso" \\
      --output-dir      /data/assignments/abc-123/ \\
      --response-col    d13C_wood \\
      --area-threshold  0.5 \\
      --prob-threshold  0.95
"""

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Tuple
import logging

import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.features import rasterize
from shapely.geometry import mapping
from scipy.stats import norm

sys.path.insert(0, str(Path(__file__).parent.parent))

from python_scripts.utils import (
    setup_logging, log_msg, MetricsCollector,
    load_shapefile, ensure_output_dir
)

NODATA_OUT = -9999.0


# =============================================================================
# pdRaster: probabilidade posterior por pixel
# =============================================================================

def pd_raster(
    iso_mean: np.ndarray,
    iso_sd:   np.ndarray,
    sample_value: float,
) -> np.ndarray:
    """
    Para cada pixel (i,j) válido, calcula a densidade da N(iso_mean, iso_sd)
    avaliada em sample_value, e normaliza para que a soma global seja 1.

    Isso reproduz EXATAMENTE o que assignR::pdRaster faz com a suposição
    de normalidade — ver Bowen et al. 2014 (eq. 1) e o código fonte do
    assignR.
    """
    valid = ~np.isnan(iso_mean) & ~np.isnan(iso_sd) & (iso_sd > 0)
    pd_map = np.full_like(iso_mean, np.nan, dtype=np.float64)

    if not valid.any():
        return pd_map

    # Densidade da normal em sample_value
    dens = norm.pdf(sample_value, loc=iso_mean[valid], scale=iso_sd[valid])
    total = float(dens.sum())
    if total <= 0:
        return pd_map

    pd_map[valid] = dens / total
    return pd_map


# =============================================================================
# qtlRaster: máscara por área ou por probabilidade acumulada
# =============================================================================

def qtl_raster_area(pd_map: np.ndarray, threshold: float) -> np.ndarray:
    """
    Seleciona os `threshold * N_valid` pixels com maior probabilidade posterior.
    Retorna máscara binária (1 = selecionado, 0 = não, nan = fora da área).
    """
    out = np.full_like(pd_map, np.nan, dtype=np.float32)
    valid = ~np.isnan(pd_map)
    if not valid.any():
        return out

    vals = pd_map[valid]
    n_keep = int(np.ceil(threshold * vals.size))
    if n_keep <= 0:
        out[valid] = 0
        return out
    if n_keep >= vals.size:
        out[valid] = 1
        return out

    cutoff = np.partition(vals, -n_keep)[-n_keep]
    mask_flat = vals >= cutoff
    out[valid] = mask_flat.astype(np.float32)
    return out


def qtl_raster_prob(pd_map: np.ndarray, threshold: float) -> np.ndarray:
    """
    Seleciona o conjunto mínimo de pixels (de maior probabilidade) cuja soma
    de probabilidade posterior é ≥ threshold.
    """
    out = np.full_like(pd_map, np.nan, dtype=np.float32)
    valid = ~np.isnan(pd_map)
    if not valid.any():
        return out

    vals = pd_map[valid]
    order = np.argsort(vals)[::-1]    # decrescente
    cum   = np.cumsum(vals[order])
    # Primeiro índice onde a soma acumulada ≥ threshold
    cutoff_idx = int(np.searchsorted(cum, threshold))
    cutoff_idx = min(cutoff_idx, len(vals) - 1)
    keep_idx   = set(order[: cutoff_idx + 1].tolist())

    keep_flat = np.array([i in keep_idx for i in range(len(vals))])
    out[valid] = keep_flat.astype(np.float32)
    return out


# =============================================================================
# oddsRatio: razão entre prob média dentro de cada par de regiões
# =============================================================================

def odds_ratio_between_regions(
    pd_map: np.ndarray,
    region_masks: Dict[str, np.ndarray],
) -> pd.DataFrame:
    """
    Para cada par de regiões (A, B), calcula:
        OR(A,B) = mean(pd_map em A) / mean(pd_map em B)
    Retorna DataFrame com colunas (region_a, region_b, odds_ratio).
    """
    means = {}
    for name, mask in region_masks.items():
        m = mask & ~np.isnan(pd_map)
        if m.any():
            means[name] = float(np.nanmean(pd_map[m]))
        else:
            means[name] = np.nan

    rows = []
    names = list(region_masks.keys())
    for a in names:
        for b in names:
            if a == b:
                continue
            mu_a = means.get(a, np.nan)
            mu_b = means.get(b, np.nan)
            ratio = (mu_a / mu_b) if (not np.isnan(mu_a) and not np.isnan(mu_b) and mu_b > 0) else np.nan
            rows.append({"region_a": a, "region_b": b, "odds_ratio": ratio})

    return pd.DataFrame(rows)


# =============================================================================
# Helpers
# =============================================================================

def rasterize_regions(
    regions_gdf: gpd.GeoDataFrame,
    name_col: str,
    raster_meta: dict,
) -> Dict[str, np.ndarray]:
    """
    Cria uma máscara booleana 2D por região (mesma grade do raster).
    """
    height = raster_meta["height"]
    width  = raster_meta["width"]
    transform = raster_meta["transform"]

    masks = {}
    for _, row in regions_gdf.iterrows():
        name = str(row[name_col])
        shape = [(mapping(row.geometry), 1)]
        mask = rasterize(
            shape,
            out_shape=(height, width),
            transform=transform,
            fill=0,
            dtype="uint8",
            all_touched=True,
        ).astype(bool)
        masks[name] = mask
    return masks


def save_raster(array: np.ndarray, meta: dict, out_path: Path,
                nodata: float = NODATA_OUT):
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


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Atribuição de origem via Bayes (equiv. run_assign.R / 04_assign.R)"
    )
    parser.add_argument("--job-id",          required=True)
    parser.add_argument("--isoscape-path",   required=True,
                        help="GeoTIFF com 2 bandas: [predição, sd]")
    parser.add_argument("--unknown-path",    required=True,
                        help="CSV ou XLSX com amostras desconhecidas")
    parser.add_argument("--regions-shp",     required=True)
    parser.add_argument("--regions-field",   default="ADM1_PT")
    parser.add_argument("--regions-filter",  default="",
                        help="Lista de regiões separadas por vírgula (vazio = todas)")
    parser.add_argument("--output-dir",      required=True)
    parser.add_argument("--response-col",    default="d13C_wood")
    parser.add_argument("--area-threshold",  type=float, default=0.5)
    parser.add_argument("--prob-threshold",  type=float, default=0.95)
    parser.add_argument("--seed",            type=int,   default=1350)

    args = parser.parse_args()

    output_dir = ensure_output_dir(Path(args.output_dir))
    logger     = setup_logging(output_dir, args.job_id, "run_assign")
    metrics    = MetricsCollector(args.job_id)

    np.random.seed(args.seed)

    log_msg(logger, f"[→] Job run_assign iniciado: {args.job_id}", "INFO")
    log_msg(logger, f"[→] Isoscape: {args.isoscape_path}", "INFO")
    log_msg(logger, f"[→] Unknowns: {args.unknown_path}", "INFO")
    log_msg(logger, f"[→] Shapefile de regiões: {args.regions_shp}", "INFO")
    log_msg(logger, "[→] Engine: Python", "INFO")

    try:
        # ── 1. Carregar isoscape (precisa de 2 bandas: predição + sd) ─────────
        log_msg(logger, "[→] Carregando isoscape...", "INFO")
        with rasterio.open(args.isoscape_path) as src:
            meta = src.meta.copy()
            n_bands = src.count
            nd = src.nodata if src.nodata is not None else NODATA_OUT
            if n_bands >= 2:
                iso_mean = src.read(1).astype(np.float64)
                iso_sd   = src.read(2).astype(np.float64)
            elif n_bands == 1:
                log_msg(logger,
                        "[!] Isoscape tem apenas 1 banda — assumindo sd=0 "
                        "(probabilidade vai concentrar nos pixels mais próximos)",
                        "WARNING")
                iso_mean = src.read(1).astype(np.float64)
                iso_sd   = np.full_like(iso_mean, 1e-3)
            else:
                raise ValueError("Isoscape sem bandas")

            iso_mean[iso_mean == nd] = np.nan
            iso_sd[iso_sd == nd]     = np.nan
            iso_sd[iso_sd <= 0]      = np.nan

        log_msg(logger,
                f"[✓] Isoscape: {n_bands} banda(s) | shape: {iso_mean.shape} | "
                f"CRS: {meta['crs']}",
                "INFO")

        # ── 2. Carregar regiões ───────────────────────────────────────────────
        log_msg(logger, "[→] Carregando shapefile de regiões...", "INFO")
        regions_gdf = load_shapefile(args.regions_shp)
        # Reprojetar para o CRS do isoscape
        if regions_gdf.crs != meta["crs"]:
            regions_gdf = regions_gdf.to_crs(meta["crs"])

        if args.regions_field not in regions_gdf.columns:
            raise ValueError(
                f"Campo '{args.regions_field}' não encontrado. "
                f"Disponíveis: {', '.join(regions_gdf.columns)}"
            )

        if args.regions_filter:
            names = [n.strip() for n in args.regions_filter.split(",")]
            regions_gdf = regions_gdf[regions_gdf[args.regions_field].isin(names)]
            log_msg(logger, f"[→] Regiões filtradas: {', '.join(names)}", "INFO")

        if len(regions_gdf) == 0:
            raise ValueError("Nenhuma região após o filtro")

        log_msg(logger, f"[✓] {len(regions_gdf)} regiões carregadas", "INFO")

        # ── 3. Rasterizar máscaras das regiões ────────────────────────────────
        log_msg(logger, "[→] Rasterizando regiões...", "INFO")
        region_masks = rasterize_regions(regions_gdf, args.regions_field, meta)
        for name, m in region_masks.items():
            log_msg(logger, f"  [→] {name}: {int(m.sum())} pixels", "INFO")

        # Médias do isoscape por região
        log_msg(logger, "[→] Calculando médias do isoscape por região...", "INFO")
        region_means = {}
        for name, m in region_masks.items():
            valid = m & ~np.isnan(iso_mean)
            region_means[name] = float(np.nanmean(iso_mean[valid])) if valid.any() else np.nan
            log_msg(logger, f"  [→] {name}: {region_means[name]:.4f}", "INFO")

        # ── 4. Carregar amostras desconhecidas ────────────────────────────────
        log_msg(logger, "[→] Carregando amostras desconhecidas...", "INFO")
        ext = Path(args.unknown_path).suffix.lower()
        if ext == ".csv":
            unknowns = pd.read_csv(args.unknown_path)
        elif ext in (".xlsx", ".xls"):
            unknowns = pd.read_excel(args.unknown_path)
        else:
            raise ValueError(f"Formato não suportado: {ext}")

        if args.response_col not in unknowns.columns:
            raise ValueError(f"Coluna '{args.response_col}' não encontrada nas amostras")
        if "ID" not in unknowns.columns:
            unknowns["ID"] = range(1, len(unknowns) + 1)
            log_msg(logger, "[→] Criando IDs sequenciais", "INFO")

        unknowns = unknowns[unknowns[args.response_col].notna()].reset_index(drop=True)
        log_msg(logger, f"[✓] {len(unknowns)} amostras desconhecidas com valor isotópico", "INFO")

        # ── 5. Loop por amostra: pdRaster + qtl + odds + posterior ────────────
        pd_paths:   List[str] = []
        qtla_paths: List[str] = []
        qtlp_paths: List[str] = []
        odds_rows:      List[dict] = []
        posterior_rows: List[dict] = []
        most_likely:    Dict[str, str] = {}

        for _, row in unknowns.iterrows():
            sample_id = str(row["ID"])
            iso_value = float(row[args.response_col])

            log_msg(logger,
                    f"[→] Amostra {sample_id} ({args.response_col} = {iso_value:.3f})",
                    "INFO")

            # ─ pdRaster ─
            pd_map = pd_raster(iso_mean, iso_sd, iso_value)
            pd_path = output_dir / f"pd_map_{sample_id}_py.tif"
            save_raster(pd_map, meta, pd_path)
            pd_paths.append(str(pd_path))
            log_msg(logger, f"  [✓] pd_map salvo: {pd_path.name}", "INFO")

            # ─ qtlRaster (area) ─
            qtla = qtl_raster_area(pd_map, args.area_threshold)
            qtla_path = output_dir / f"qtl_area_{sample_id}_py.tif"
            save_raster(qtla, meta, qtla_path)
            qtla_paths.append(str(qtla_path))

            # ─ qtlRaster (prob) ─
            qtlp = qtl_raster_prob(pd_map, args.prob_threshold)
            qtlp_path = output_dir / f"qtl_prob_{sample_id}_py.tif"
            save_raster(qtlp, meta, qtlp_path)
            qtlp_paths.append(str(qtlp_path))

            # ─ oddsRatio entre regiões ─
            odds_df = odds_ratio_between_regions(pd_map, region_masks)
            odds_df["sample_id"] = sample_id
            odds_rows.extend(odds_df.to_dict("records"))

            # ─ Posterior por região: integra pd_map dentro de cada região e normaliza ─
            posterior_by_region = {}
            for rname, rmask in region_masks.items():
                m = rmask & ~np.isnan(pd_map)
                posterior_by_region[rname] = float(pd_map[m].sum()) if m.any() else 0.0

            total = sum(posterior_by_region.values())
            if total > 0:
                posterior_by_region = {k: v / total for k, v in posterior_by_region.items()}

            for rname, p in posterior_by_region.items():
                posterior_rows.append({
                    "sample_id": sample_id,
                    "iso_value": iso_value,
                    "region":    rname,
                    "posterior": p,
                })

            best = max(posterior_by_region, key=posterior_by_region.get) if posterior_by_region else ""
            most_likely[sample_id] = best
            log_msg(logger, f"  [✓] Região mais provável: {best}", "INFO")

        # ── 6. Salvar tabelas consolidadas ────────────────────────────────────
        posterior_df = pd.DataFrame(posterior_rows)
        posterior_csv = output_dir / "posterior_probs_py.csv"
        posterior_df.to_csv(posterior_csv, index=False)
        log_msg(logger, f"[✓] {posterior_csv.name} salvo", "INFO")

        odds_df = pd.DataFrame(odds_rows)
        odds_csv = output_dir / "odds_ratios_py.csv"
        odds_df.to_csv(odds_csv, index=False)
        log_msg(logger, f"[✓] {odds_csv.name} salvo", "INFO")

        # ── 7. metrics.json ───────────────────────────────────────────────────
        metrics.add("engine",          "python")
        metrics.add("n_unknowns",      int(len(unknowns)))
        metrics.add("regions",         list(region_masks.keys()))
        metrics.add("region_means",    region_means)
        metrics.add("pd_maps",         pd_paths)
        metrics.add("qtl_area_maps",   qtla_paths)
        metrics.add("qtl_prob_maps",   qtlp_paths)
        metrics.add("posterior_csv",   str(posterior_csv))
        metrics.add("odds_csv",        str(odds_csv))
        metrics.add("area_threshold",  args.area_threshold)
        metrics.add("prob_threshold",  args.prob_threshold)
        metrics.add("most_likely",     most_likely)

        metrics.save(output_dir / "metrics.json")
        log_msg(logger, f"[✓] metrics.json salvo", "INFO")
        log_msg(logger, "[★] run_assign concluído com sucesso", "INFO")
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