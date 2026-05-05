#!/usr/bin/env python
"""
backend/python_scripts/gen_rasters.py

Equivalente Python do gen_rasters.R.
Baixa dados WorldClim (arquivos .zip), extrai, recorta pela área de estudo e salva .tif.

WorldClim v2.1 distribui arquivos .zip com rasters mensais ou bioclim:
  https://geodata.ucdavis.edu/climate/worldclim/2_1/base/wc2.1_{res}m_{var}.zip

Uso:
    python gen_rasters.py \\
      --job-id abc-123 \\
      --shapefile /data/shapefiles/amazonia_legal.shp \\
      --output-dir /data/rasters/project1/ \\
      --worldclim-dir /data/worldclim_cache/ \\
      --variables tavg,tmax,tmin,prec \\
      --bio-layers bio1,bio2,bio3 \\
      --resolution 5 \\
      --skip-existing true
"""

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple
import logging
import urllib.request
import urllib.error

import numpy as np
import geopandas as gpd
import rasterio
from rasterio.mask import mask as rio_mask
from rasterio.warp import calculate_default_transform, reproject, Resampling
from rasterio.crs import CRS
from shapely.geometry import mapping

sys.path.insert(0, str(Path(__file__).parent.parent))

from python_scripts.utils import (
    setup_logging, log_msg, MetricsCollector,
    load_shapefile, ensure_output_dir, parse_csv_list, get_file_prefix
)

# ── WorldClim v2.1 URLs ────────────────────────────────────────────────────────
# Resolução → sufixo usado na URL (2.5m, 5m, 10m)
RESOLUTION_MAP = {
    2.5: "2.5m",
    5:   "5m",
    10:  "10m",
}

WORLDCLIM_BASE = "https://geodata.ucdavis.edu/climate/worldclim/2_1/base"

# Variáveis com múltiplas camadas mensais (12 arquivos dentro do .zip)
MONTHLY_VARS = {"tavg", "tmax", "tmin", "prec", "srad", "vapr", "wind"}
# Bioclim: um único arquivo com 19 bandas
BIO_VAR = "bio"

TARGET_CRS = "EPSG:4674"  # SIRGAS 2000


# =============================================================================
# Download com progresso
# =============================================================================

def _report_hook(count, block_size, total_size):
    """Hook de progresso para urllib.request.urlretrieve."""
    if total_size > 0:
        pct = int(count * block_size * 100 / total_size)
        pct = min(pct, 100)
        if pct % 10 == 0:
            pass  # Evita spam; o caller usa log_msg


def download_file(url: str, dest: Path, logger: logging.Logger) -> bool:
    """
    Baixa url para dest. Retorna True em sucesso.
    Usa urllib (stdlib) — sem dependências extras.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        log_msg(logger, f"  [cache] Arquivo já existe: {dest.name}", "INFO")
        return True

    log_msg(logger, f"  [↓] {url}", "INFO")
    try:
        urllib.request.urlretrieve(url, dest)
        size_mb = dest.stat().st_size / 1024 / 1024
        log_msg(logger, f"  [✓] Download concluído: {dest.name} ({size_mb:.1f} MB)", "INFO")
        return True
    except urllib.error.URLError as e:
        log_msg(logger, f"  [!] Falha no download: {e}", "ERROR")
        if dest.exists():
            dest.unlink()
        return False
    except Exception as e:
        log_msg(logger, f"  [!] Erro inesperado no download: {e}", "ERROR")
        if dest.exists():
            dest.unlink()
        return False


# =============================================================================
# WorldClim: encontrar arquivos dentro do .zip
# =============================================================================

def list_tifs_in_zip(zip_path: Path) -> List[str]:
    """Lista todos os .tif dentro do .zip."""
    with zipfile.ZipFile(zip_path, "r") as z:
        return [n for n in z.namelist() if n.lower().endswith(".tif")]


def extract_tif_from_zip(zip_path: Path, tif_name: str, dest_dir: Path) -> Optional[Path]:
    """Extrai um .tif específico do .zip para dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / Path(tif_name).name
    if dest_file.exists():
        return dest_file
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            with z.open(tif_name) as src, open(dest_file, "wb") as dst:
                dst.write(src.read())
        return dest_file
    except Exception as e:
        return None


# =============================================================================
# Recorte e reprojeção
# =============================================================================

def clip_and_reproject_tif(
    src_path: Path,
    shapes_gdf: gpd.GeoDataFrame,
    out_path: Path,
    target_crs: str = TARGET_CRS,
) -> bool:
    """
    Recorta src_path pela geometria de shapes_gdf e reprojeta para target_crs.
    Salva em out_path. Retorna True em sucesso.
    """
    try:
        with rasterio.open(src_path) as src:
            src_crs = src.crs

            # Reprojetar geometrias para o CRS do raster
            shapes_reproj = shapes_gdf.to_crs(src_crs)
            geoms = [mapping(geom) for geom in shapes_reproj.geometry]

            # Recorte
            out_image, out_transform = rio_mask(src, geoms, crop=True, nodata=src.nodata or -9999)
            out_meta = src.meta.copy()
            out_meta.update({
                "driver": "GTiff",
                "height": out_image.shape[1],
                "width":  out_image.shape[2],
                "transform": out_transform,
                "crs": src_crs,
                "compress": "lzw",
            })

        # Reprojetar para TARGET_CRS se necessário
        dst_crs = CRS.from_string(target_crs)
        if src_crs != dst_crs:
            transform_dst, width_dst, height_dst = calculate_default_transform(
                src_crs, dst_crs,
                out_meta["width"], out_meta["height"],
                *rasterio.transform.array_bounds(
                    out_meta["height"], out_meta["width"], out_meta["transform"]
                )
            )
            out_meta.update({
                "crs": dst_crs,
                "transform": transform_dst,
                "width": width_dst,
                "height": height_dst,
            })
            reproj_image = np.full(
                (out_image.shape[0], height_dst, width_dst),
                out_meta.get("nodata", -9999),
                dtype=out_image.dtype
            )
            for i in range(out_image.shape[0]):
                reproject(
                    source=out_image[i],
                    destination=reproj_image[i],
                    src_transform=out_transform,
                    src_crs=src_crs,
                    dst_transform=transform_dst,
                    dst_crs=dst_crs,
                    resampling=Resampling.bilinear,
                )
            out_image = reproj_image

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(out_path, "w", **out_meta) as dst:
            dst.write(out_image)

        return True

    except Exception as e:
        raise RuntimeError(f"clip_and_reproject falhou para {src_path.name}: {e}") from e


# =============================================================================
# Processamento: variáveis mensais (tavg, tmax, etc.)
# =============================================================================

def process_monthly_var(
    var: str,
    resolution: float,
    shapes_gdf: gpd.GeoDataFrame,
    prefix: str,
    res_tag: str,
    output_dir: Path,
    worldclim_dir: Path,
    skip_existing: bool,
    metrics: MetricsCollector,
    logger: logging.Logger,
) -> Tuple[List[str], List[str], List[str]]:
    """
    Baixa variável mensal, calcula média e salva .tif recortado.
    Retorna (generated, skipped, failed).
    """
    generated, skipped, failed = [], [], []

    out_file = output_dir / f"{prefix}_{res_tag}_{var}_mean.tif"

    if skip_existing and out_file.exists():
        log_msg(logger, f"[skip] {out_file.name}", "INFO")
        skipped.append(str(out_file))
        return generated, skipped, failed

    res_str = RESOLUTION_MAP.get(resolution)
    if not res_str:
        log_msg(logger, f"[!] Resolução inválida: {resolution}", "ERROR")
        failed.append(var)
        return generated, skipped, failed

    zip_name  = f"wc2.1_{res_str}_{var}.zip"
    zip_url   = f"{WORLDCLIM_BASE}/{zip_name}"
    zip_cache = worldclim_dir / zip_name

    log_msg(logger, f"[→] Baixando: {var} ({res_str})", "INFO")
    if not download_file(zip_url, zip_cache, logger):
        failed.append(var)
        return generated, skipped, failed

    # Listar .tifs no zip (ex: wc2.1_10m_tavg_01.tif ... _12.tif)
    tif_names = list_tifs_in_zip(zip_cache)
    if not tif_names:
        log_msg(logger, f"[!] Nenhum .tif encontrado no zip de {var}", "ERROR")
        failed.append(var)
        return generated, skipped, failed

    log_msg(logger, f"[→] {len(tif_names)} rasters mensais encontrados em {zip_name}", "INFO")

    # Extrair e recortar cada mês
    extract_dir = worldclim_dir / f"extracted_{res_str}_{var}"
    month_arrays = []
    month_meta   = None

    for tif_name in sorted(tif_names):
        extracted = extract_tif_from_zip(zip_cache, tif_name, extract_dir)
        if extracted is None:
            continue
        # Ler após recortar em memória
        temp_out = extract_dir / f"clip_{Path(tif_name).name}"
        try:
            if not temp_out.exists():
                clip_and_reproject_tif(extracted, shapes_gdf, temp_out)
            with rasterio.open(temp_out) as src:
                if month_meta is None:
                    month_meta = src.meta.copy()
                arr = src.read(1).astype(np.float32)
                nodata = src.nodata
                if nodata is not None:
                    arr[arr == nodata] = np.nan
                month_arrays.append(arr)
        except Exception as e:
            log_msg(logger, f"  [!] Erro processando {Path(tif_name).name}: {e}", "ERROR")

    if not month_arrays or month_meta is None:
        log_msg(logger, f"[!] Nenhum dado válido para {var}", "ERROR")
        failed.append(var)
        return generated, skipped, failed

    # Média mensal (ignora NaN)
    stack = np.stack(month_arrays, axis=0)
    mean_arr = np.nanmean(stack, axis=0)

    month_meta.update({
        "count": 1,
        "dtype": "float32",
        "compress": "lzw",
        "nodata": -9999.0,
    })
    mean_arr[np.isnan(mean_arr)] = -9999.0

    try:
        with rasterio.open(out_file, "w", **month_meta) as dst:
            dst.write(mean_arr, 1)
        log_msg(logger, f"[✓] Salvo: {out_file.name} | shape: {mean_arr.shape} "
                        f"| min={np.nanmin(stack):.2f} max={np.nanmax(stack):.2f}", "INFO")
        generated.append(str(out_file))
    except Exception as e:
        log_msg(logger, f"[!] Erro ao salvar {out_file.name}: {e}", "ERROR")
        failed.append(var)

    return generated, skipped, failed


# =============================================================================
# Processamento: bioclim
# =============================================================================

def process_bio_layers(
    bio_layers: List[str],
    resolution: float,
    shapes_gdf: gpd.GeoDataFrame,
    prefix: str,
    res_tag: str,
    output_dir: Path,
    worldclim_dir: Path,
    skip_existing: bool,
    metrics: MetricsCollector,
    logger: logging.Logger,
) -> Tuple[List[str], List[str], List[str]]:
    """
    Baixa bioclim WorldClim (19 bandas num .zip) e salva .tif por layer.
    Retorna (generated, skipped, failed).
    """
    generated, skipped, failed = [], [], []

    res_str = RESOLUTION_MAP.get(resolution)
    if not res_str:
        log_msg(logger, f"[!] Resolução inválida: {resolution}", "ERROR")
        return generated, skipped, [BIO_VAR]

    zip_name  = f"wc2.1_{res_str}_bio.zip"
    zip_url   = f"{WORLDCLIM_BASE}/{zip_name}"
    zip_cache = worldclim_dir / zip_name

    # Checar skip: se todos os layers já existem
    all_exist = all(
        (output_dir / f"{prefix}_{res_tag}_{b}.tif").exists()
        for b in bio_layers
    )
    if skip_existing and all_exist:
        log_msg(logger, "[skip] Todos os layers bio já existem", "INFO")
        for b in bio_layers:
            skipped.append(str(output_dir / f"{prefix}_{res_tag}_{b}.tif"))
        return generated, skipped, failed

    log_msg(logger, f"[→] Baixando: bio ({res_str})", "INFO")
    if not download_file(zip_url, zip_cache, logger):
        return generated, skipped, [BIO_VAR]

    tif_names = list_tifs_in_zip(zip_cache)
    # Formato esperado: wc2.1_10m_bio_1.tif, wc2.1_10m_bio_2.tif, ...
    # Ou (versões antigas): wc2.1_10m_bio1.tif, ...
    log_msg(logger, f"[→] {len(tif_names)} arquivos no zip bio", "INFO")

    extract_dir = worldclim_dir / f"extracted_{res_str}_bio"

    for tif_name in sorted(tif_names):
        # Extrair número do bio do nome do arquivo
        match = re.search(r"bio_?(\d+)", Path(tif_name).stem, re.IGNORECASE)
        if not match:
            continue
        bio_num  = int(match.group(1))
        bio_name = f"bio{bio_num}"

        if bio_name not in bio_layers:
            continue

        out_file = output_dir / f"{prefix}_{res_tag}_{bio_name}.tif"

        if skip_existing and out_file.exists():
            log_msg(logger, f"  [skip] {out_file.name}", "INFO")
            skipped.append(str(out_file))
            continue

        extracted = extract_tif_from_zip(zip_cache, tif_name, extract_dir)
        if extracted is None:
            log_msg(logger, f"  [!] Falha ao extrair {tif_name}", "ERROR")
            failed.append(bio_name)
            continue

        try:
            clip_and_reproject_tif(extracted, shapes_gdf, out_file)
            with rasterio.open(out_file) as src:
                arr = src.read(1, masked=True)
                log_msg(logger, f"  [✓] Salvo: {out_file.name} "
                                f"| shape: {arr.shape} "
                                f"| min={float(arr.min()):.2f} max={float(arr.max()):.2f}", "INFO")
            generated.append(str(out_file))
        except Exception as e:
            log_msg(logger, f"  [!] Erro processando {bio_name}: {e}", "ERROR")
            failed.append(bio_name)

    return generated, skipped, failed


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Baixa WorldClim e recorta para área de estudo (equiv. gen_rasters.R)"
    )
    parser.add_argument("--job-id",        required=True)
    parser.add_argument("--shapefile",      required=True)
    parser.add_argument("--output-dir",     required=True)
    parser.add_argument("--worldclim-dir",  required=True)
    parser.add_argument("--variables",      required=True)
    parser.add_argument("--bio-layers",     default="")
    parser.add_argument("--resolution",     type=float, default=5)
    parser.add_argument("--skip-existing",  type=lambda x: x.lower() == "true", default=True)

    args = parser.parse_args()

    output_dir    = ensure_output_dir(Path(args.output_dir))
    worldclim_dir = ensure_output_dir(Path(args.worldclim_dir))
    logger        = setup_logging(output_dir, args.job_id, "gen_rasters")
    metrics       = MetricsCollector(args.job_id)

    log_msg(logger, f"[→] Job gen_rasters iniciado: {args.job_id}", "INFO")
    log_msg(logger, f"[→] Shapefile: {args.shapefile}", "INFO")
    log_msg(logger, f"[→] Variáveis: {args.variables}", "INFO")
    log_msg(logger, f"[→] Resolução: {args.resolution} arc-min", "INFO")
    log_msg(logger, f"[→] Skip existing: {args.skip_existing}", "INFO")

    # Verificar shapefile
    if not Path(args.shapefile).exists():
        log_msg(logger, f"[!] Shapefile não encontrado: {args.shapefile}", "ERROR")
        sys.exit(1)

    try:
        log_msg(logger, "[→] Carregando shapefile...", "INFO")
        shapes_gdf = load_shapefile(args.shapefile)
        log_msg(logger, f"[✓] Shapefile carregado: {Path(args.shapefile).name} | "
                        f"CRS: {shapes_gdf.crs} | features: {len(shapes_gdf)}", "INFO")
    except Exception as e:
        log_msg(logger, f"[!] Erro ao carregar shapefile: {e}", "ERROR")
        sys.exit(1)

    variables  = parse_csv_list(args.variables)
    bio_layers = (
        parse_csv_list(args.bio_layers)
        if args.bio_layers
        else [f"bio{i}" for i in range(1, 20)]
    )
    prefix  = get_file_prefix(args.shapefile)
    res_tag = f"{int(args.resolution)}arc"

    all_generated: List[str] = []
    all_skipped:   List[str] = []
    all_failed:    List[str] = []

    for var in variables:
        if var == BIO_VAR:
            gen, skip, fail = process_bio_layers(
                bio_layers, args.resolution, shapes_gdf,
                prefix, res_tag, output_dir, worldclim_dir,
                args.skip_existing, metrics, logger,
            )
        elif var in MONTHLY_VARS:
            gen, skip, fail = process_monthly_var(
                var, args.resolution, shapes_gdf,
                prefix, res_tag, output_dir, worldclim_dir,
                args.skip_existing, metrics, logger,
            )
        else:
            log_msg(logger, f"[!] Variável desconhecida (ignorada): {var}", "WARNING")
            gen, skip, fail = [], [], [var]

        all_generated.extend(gen)
        all_skipped.extend(skip)
        all_failed.extend(fail)

    log_msg(logger, f"[→] Resumo: gerados={len(all_generated)} "
                    f"| pulados={len(all_skipped)} | falhos={len(all_failed)}", "INFO")

    metrics.add("generated_files", all_generated)
    metrics.add("skipped_files",   all_skipped)
    metrics.add("failed_vars",     all_failed)
    metrics.add("output_dir",      str(output_dir))
    metrics.save(output_dir / "metrics.json")
    log_msg(logger, f"[✓] metrics.json salvo", "INFO")

    if all_failed:
        log_msg(logger, f"[!] Variáveis com falha: {', '.join(all_failed)}", "ERROR")
        sys.exit(1)

    log_msg(logger, "[★] gen_rasters concluído com sucesso", "INFO")
    sys.exit(0)


if __name__ == "__main__":
    main()