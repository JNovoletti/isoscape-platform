#!/usr/bin/env python
"""
backend/python_scripts/gen_rasters.py

Equivalente Python do gen_rasters.R (e do 01_worldclim.R do curso).
Baixa dados WorldClim, recorta pela área de estudo e salva .tif.

Compartilhamento de cache com R:
==================================
O pacote `geodata` do R salva os arquivos em:
    {worldclim_dir}/climate/wc2.1_{res}m/wc2.1_{res}m_{var}_{NN}.tif
e mantém o .zip original em:
    {worldclim_dir}/wc2.1_{res}m_{var}.zip

Este script usa EXATAMENTE essa convenção. Portanto:
  - Se R rodar primeiro, Python reaproveita os .tif sem baixar nada.
  - Se Python rodar primeiro, R reaproveita os .tif sem baixar nada.

Os arquivos de saída usam o sufixo "_py" para diferenciar da versão R ("_r"):
    {prefix}_{res}arc_{var}_mean_py.tif   ← Python
    {prefix}_{res}arc_{var}_mean_r.tif    ← R

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
# Resolução → sufixo usado pela URL e pelo cache do geodata
RESOLUTION_MAP = {
    2.5: "2.5m",
    5:   "5m",
    10:  "10m",
}

WORLDCLIM_BASE = "https://geodata.ucdavis.edu/climate/worldclim/2_1/base"
# elev é distribuído separadamente
ELEV_BASE      = "https://geodata.ucdavis.edu/geodata/elevation"

# Variáveis com múltiplas camadas mensais (12 arquivos dentro do .zip)
MONTHLY_VARS = {"tavg", "tmax", "tmin", "prec", "srad", "vapr", "wind"}
# Bioclim: um único arquivo com 19 bandas
BIO_VAR = "bio"
# Elevação: single layer
ELEV_VAR = "elev"

TARGET_CRS = "EPSG:4674"  # SIRGAS 2000

# Nodata padrão para arquivos WorldClim que não declaram nodata
WORLDCLIM_NODATA = -9999.0


# =============================================================================
# Cache: mesma estrutura que o geodata do R
# =============================================================================

def _geodata_cache_dir(worldclim_dir: Path, res_str: str) -> Path:
    """
    Retorna o diretório onde o geodata/R salva os .tif do WorldClim:
      {worldclim_dir}/climate/wc2.1_{res}m/
    Cria o diretório se não existir.
    """
    d = worldclim_dir / "climate" / f"wc2.1_{res_str}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _zip_cache_path(worldclim_dir: Path, res_str: str, var: str) -> Path:
    """
    Caminho do .zip na raiz do worldclim_dir (mesmo local que o geodata usa
    antes de extrair):
      {worldclim_dir}/wc2.1_{res}m_{var}.zip
    """
    return worldclim_dir / f"wc2.1_{res_str}_{var}.zip"


# =============================================================================
# Download
# =============================================================================

def download_file(url: str, dest: Path, logger: logging.Logger) -> bool:
    """Baixa url para dest. Retorna True em sucesso."""
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
# Extração do .zip para o cache do geodata
# =============================================================================

def ensure_tifs_extracted(
    zip_path: Path,
    cache_dir: Path,
    logger: logging.Logger,
) -> List[Path]:
    """
    Extrai todos os .tif do zip_path para cache_dir, se ainda não existirem.
    Retorna a lista de caminhos dos .tif extraídos (ordenada).
    """
    with zipfile.ZipFile(zip_path, "r") as z:
        tif_names = sorted(n for n in z.namelist() if n.lower().endswith(".tif"))
        if not tif_names:
            return []
        extracted = []
        for name in tif_names:
            dest = cache_dir / Path(name).name
            if not dest.exists():
                with z.open(name) as src, open(dest, "wb") as dst:
                    dst.write(src.read())
            extracted.append(dest)
    return extracted


# =============================================================================
# Recorte e reprojeção  (espelho fiel do process_raster() do R)
# =============================================================================

def clip_and_reproject_tif(
    src_path: Path,
    shapes_gdf: gpd.GeoDataFrame,
    out_path: Path,
    target_crs: str = TARGET_CRS,
) -> None:
    """
    1. Reprojeta a geometria para o CRS do raster (igual ao R: project(area, crs(r)))
    2. Recorta (crop) e mascara (mask) — equivalente a terra::crop + terra::mask
    3. Reprojeta o raster para target_crs — equivalente a terra::project(r, target_crs)
    4. Salva em out_path.

    Lança RuntimeError em caso de falha.
    """
    try:
        with rasterio.open(src_path) as src:
            src_crs  = src.crs
            src_nodata = src.nodata if src.nodata is not None else WORLDCLIM_NODATA

            shapes_reproj = shapes_gdf.to_crs(src_crs)
            geoms = [mapping(geom) for geom in shapes_reproj.geometry]

            # all_touched=True espelha o comportamento padrão do terra::mask
            out_image, out_transform = rio_mask(
                src, geoms,
                crop=True,
                filled=True,
                nodata=src_nodata,
                all_touched=True,
            )
            out_meta = src.meta.copy()
            out_meta.update({
                "driver":    "GTiff",
                "height":    out_image.shape[1],
                "width":     out_image.shape[2],
                "transform": out_transform,
                "crs":       src_crs,
                "nodata":    src_nodata,
                "compress":  "lzw",
            })

        # Reprojetar para TARGET_CRS se necessário (igual ao terra::project do R)
        dst_crs = CRS.from_string(target_crs)
        if src_crs != dst_crs:
            transform_dst, width_dst, height_dst = calculate_default_transform(
                src_crs, dst_crs,
                out_meta["width"], out_meta["height"],
                *rasterio.transform.array_bounds(
                    out_meta["height"], out_meta["width"], out_meta["transform"]
                ),
            )
            out_meta.update({
                "crs":       dst_crs,
                "transform": transform_dst,
                "width":     width_dst,
                "height":    height_dst,
            })
            reproj_image = np.full(
                (out_image.shape[0], height_dst, width_dst),
                src_nodata,
                dtype=out_image.dtype,
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

    except Exception as e:
        raise RuntimeError(f"clip_and_reproject falhou para {src_path.name}: {e}") from e


# =============================================================================
# Leitura segura de banda (trata nodata → NaN)
# =============================================================================

def read_band_as_float(src_path: Path) -> Tuple[np.ndarray, dict]:
    """
    Lê a banda 1 do raster como float32 com nodata → NaN.
    Retorna (array, meta).
    """
    with rasterio.open(src_path) as src:
        meta = src.meta.copy()
        arr = src.read(1, masked=True).astype(np.float32)
        arr = arr.filled(np.nan)
    return arr, meta


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
    Usa o mesmo cache que o R (climate/wc2.1_{res}m/).
    Retorna (generated, skipped, failed).
    """
    generated, skipped, failed = [], [], []

    out_file = output_dir / f"{prefix}_{res_tag}_{var}_mean_py.tif"

    if skip_existing and out_file.exists():
        log_msg(logger, f"[skip] {out_file.name}", "INFO")
        skipped.append(str(out_file))
        return generated, skipped, failed

    res_str = RESOLUTION_MAP.get(resolution)
    if not res_str:
        log_msg(logger, f"[!] Resolução inválida: {resolution}", "ERROR")
        failed.append(var)
        return generated, skipped, failed

    # Cache dir idêntico ao do geodata/R
    cache_dir = _geodata_cache_dir(worldclim_dir, res_str)
    zip_path  = _zip_cache_path(worldclim_dir, res_str, var)
    zip_url   = f"{WORLDCLIM_BASE}/wc2.1_{res_str}_{var}.zip"

    # Verificar se os .tif já estão no cache do geodata (R pode ter baixado antes)
    existing_tifs = sorted(cache_dir.glob(f"wc2.1_{res_str}_{var}_*.tif"))

    if not existing_tifs:
        # Precisamos baixar
        log_msg(logger, f"[→] Baixando: {var} ({res_str})", "INFO")
        log_msg(logger, f"  [↓] {zip_url}", "INFO")
        if not download_file(zip_url, zip_path, logger):
            failed.append(var)
            return generated, skipped, failed

        # Extrair para o mesmo diretório que o geodata usaria
        log_msg(logger, f"[→] Extraindo para cache ({cache_dir.name}/)", "INFO")
        try:
            existing_tifs = ensure_tifs_extracted(zip_path, cache_dir, logger)
        except Exception as e:
            log_msg(logger, f"[!] Erro ao extrair {zip_path.name}: {e}", "ERROR")
            failed.append(var)
            return generated, skipped, failed
    else:
        log_msg(logger, f"[→] Cache hit: {len(existing_tifs)} .tif em {cache_dir.name}/ (provavelmente baixados pelo R)", "INFO")

    if not existing_tifs:
        log_msg(logger, f"[!] Nenhum .tif encontrado para {var}", "ERROR")
        failed.append(var)
        return generated, skipped, failed

    log_msg(logger, f"[→] {var} tem {len(existing_tifs)} camadas — calculando média anual", "INFO")

    # Recortar cada mês e acumular em stack para calcular a média
    month_arrays: List[np.ndarray] = []
    final_meta: Optional[dict]     = None

    for tif_path in existing_tifs:
        # Recortar em arquivo temporário dentro do cache (reutilizável entre runs)
        clipped = cache_dir / f"clip_{prefix}_{tif_path.name}"
        try:
            if not clipped.exists():
                clip_and_reproject_tif(tif_path, shapes_gdf, clipped, TARGET_CRS)
            arr, meta = read_band_as_float(clipped)
            if final_meta is None:
                final_meta = meta
            month_arrays.append(arr)
        except Exception as e:
            log_msg(logger, f"  [!] Erro processando {tif_path.name}: {e}", "ERROR")

    if not month_arrays or final_meta is None:
        log_msg(logger, f"[!] Nenhum dado válido para {var}", "ERROR")
        failed.append(var)
        return generated, skipped, failed

    # Média ignorando NaN (equivalente a terra::app(r, mean, na.rm=TRUE) do R)
    stack    = np.stack(month_arrays, axis=0)   # (12, H, W)
    mean_arr = np.nanmean(stack, axis=0)         # (H, W)

    # Substituir NaN por nodata antes de salvar
    out_arr = mean_arr.copy()
    out_arr[np.isnan(out_arr)] = WORLDCLIM_NODATA

    final_meta.update({
        "count":   1,
        "dtype":   "float32",
        "compress": "lzw",
        "nodata":  WORLDCLIM_NODATA,
    })

    try:
        with rasterio.open(out_file, "w", **final_meta) as dst:
            dst.write(out_arr.astype(np.float32), 1)

        valid = mean_arr[~np.isnan(mean_arr)]
        if valid.size > 0:
            log_msg(
                logger,
                f"[✓] Salvo: {out_file.name} "
                f"| shape: {mean_arr.shape} "
                f"| valores: [{float(valid.min()):.2f} ; {float(valid.max()):.2f}]",
                "INFO",
            )
        else:
            log_msg(logger, f"[✓] Salvo: {out_file.name} (sem pixels válidos)", "WARNING")
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
    Baixa bioclim WorldClim e salva .tif por layer.
    Usa o mesmo cache que o R (climate/wc2.1_{res}m/).
    Retorna (generated, skipped, failed).
    """
    generated, skipped, failed = [], [], []

    res_str = RESOLUTION_MAP.get(resolution)
    if not res_str:
        log_msg(logger, f"[!] Resolução inválida: {resolution}", "ERROR")
        return generated, skipped, [BIO_VAR]

    cache_dir = _geodata_cache_dir(worldclim_dir, res_str)
    zip_path  = _zip_cache_path(worldclim_dir, res_str, "bio")
    zip_url   = f"{WORLDCLIM_BASE}/wc2.1_{res_str}_bio.zip"

    # Checar skip: se todos os layers de saída já existem
    all_out_exist = all(
        (output_dir / f"{prefix}_{res_tag}_{b}_py.tif").exists()
        for b in bio_layers
    )
    if skip_existing and all_out_exist:
        log_msg(logger, "[skip] Todos os layers bio já existem", "INFO")
        for b in bio_layers:
            skipped.append(str(output_dir / f"{prefix}_{res_tag}_{b}_py.tif"))
        return generated, skipped, failed

    # Verificar se os .tif já estão no cache do geodata
    existing_tifs = sorted(cache_dir.glob(f"wc2.1_{res_str}_bio_*.tif"))

    if not existing_tifs:
        log_msg(logger, f"[→] Baixando: bio ({res_str})", "INFO")
        if not download_file(zip_url, zip_path, logger):
            return generated, skipped, [BIO_VAR]

        log_msg(logger, f"[→] Extraindo para cache ({cache_dir.name}/)", "INFO")
        try:
            existing_tifs = ensure_tifs_extracted(zip_path, cache_dir, logger)
        except Exception as e:
            log_msg(logger, f"[!] Erro ao extrair {zip_path.name}: {e}", "ERROR")
            return generated, skipped, [BIO_VAR]
    else:
        log_msg(logger, f"[→] Cache hit: {len(existing_tifs)} .tif bio em {cache_dir.name}/", "INFO")

    log_msg(logger, f"[→] {len(existing_tifs)} arquivos no cache bio", "INFO")

    for tif_path in sorted(existing_tifs):
        match = re.search(r"bio_?(\d+)", tif_path.stem, re.IGNORECASE)
        if not match:
            continue
        bio_num  = int(match.group(1))
        bio_name = f"bio{bio_num}"

        if bio_name not in bio_layers:
            continue

        out_file = output_dir / f"{prefix}_{res_tag}_{bio_name}_py.tif"

        if skip_existing and out_file.exists():
            log_msg(logger, f"  [skip] {out_file.name}", "INFO")
            skipped.append(str(out_file))
            continue

        try:
            clip_and_reproject_tif(tif_path, shapes_gdf, out_file, TARGET_CRS)
            arr, _ = read_band_as_float(out_file)
            valid  = arr[~np.isnan(arr)]
            if valid.size > 0:
                log_msg(
                    logger,
                    f"  [✓] Salvo: {out_file.name} "
                    f"| shape: {arr.shape} "
                    f"| valores: [{float(valid.min()):.2f} ; {float(valid.max()):.2f}]",
                    "INFO",
                )
            else:
                log_msg(logger, f"  [✓] Salvo: {out_file.name} (sem pixels válidos)", "WARNING")
            generated.append(str(out_file))
        except Exception as e:
            log_msg(logger, f"  [!] Erro processando {bio_name}: {e}", "ERROR")
            failed.append(bio_name)

    return generated, skipped, failed


# =============================================================================
# Processamento: elevação (geodata::elevation_global do R)
# =============================================================================

def process_elev(
    resolution: float,
    shapes_gdf: gpd.GeoDataFrame,
    prefix: str,
    res_tag: str,
    output_dir: Path,
    worldclim_dir: Path,
    skip_existing: bool,
    logger: logging.Logger,
) -> Tuple[List[str], List[str], List[str]]:
    """
    Elevação SRTM agregada (single layer). O R usa geodata::elevation_global().
    No Python tentamos a mesma URL que o geodata; se falhar, marcamos como falha
    (o curso lista elev em vars mas alguns ambientes não conseguem baixá-la
    sem proxy).
    """
    generated, skipped, failed = [], [], []

    out_file = output_dir / f"{prefix}_{res_tag}_elev_py.tif"
    if skip_existing and out_file.exists():
        log_msg(logger, f"[skip] {out_file.name}", "INFO")
        return generated, [str(out_file)], failed

    res_str = RESOLUTION_MAP.get(resolution)
    if not res_str:
        failed.append(ELEV_VAR)
        return generated, skipped, failed

    cache_dir = _geodata_cache_dir(worldclim_dir, res_str)

    # Geodata salva elev como wc2.1_{res}m_elev.tif diretamente
    elev_cached = cache_dir / f"wc2.1_{res_str}_elev.tif"

    if not elev_cached.exists():
        # Tentar baixar diretamente o .tif (geodata usa este URL)
        url = f"{WORLDCLIM_BASE}/wc2.1_{res_str}_elev.zip"
        zip_path = _zip_cache_path(worldclim_dir, res_str, "elev")
        log_msg(logger, f"[→] Baixando elev ({res_str})", "INFO")
        if not download_file(url, zip_path, logger):
            failed.append(ELEV_VAR)
            return generated, skipped, failed
        try:
            extracted = ensure_tifs_extracted(zip_path, cache_dir, logger)
            if extracted:
                elev_cached = extracted[0]
        except Exception as e:
            log_msg(logger, f"[!] Erro ao extrair elev: {e}", "ERROR")
            failed.append(ELEV_VAR)
            return generated, skipped, failed
    else:
        log_msg(logger, f"[→] Cache hit: {elev_cached.name}", "INFO")

    try:
        clip_and_reproject_tif(elev_cached, shapes_gdf, out_file, TARGET_CRS)
        arr, _ = read_band_as_float(out_file)
        valid = arr[~np.isnan(arr)]
        if valid.size > 0:
            log_msg(logger,
                    f"[✓] Salvo: {out_file.name} | valores: "
                    f"[{float(valid.min()):.2f} ; {float(valid.max()):.2f}]",
                    "INFO")
        generated.append(str(out_file))
    except Exception as e:
        log_msg(logger, f"[!] Erro processando elev: {e}", "ERROR")
        failed.append(ELEV_VAR)

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
    log_msg(logger, f"[→] WorldClim cache dir: {worldclim_dir}", "INFO")
    log_msg(logger, f"[→] Output dir: {output_dir}", "INFO")
    log_msg(logger, "[→] Engine: Python (gera arquivos com sufixo '_py')", "INFO")
    log_msg(logger,
            f"[→] Cache esperado em: {worldclim_dir / 'climate'}/wc2.1_{RESOLUTION_MAP.get(args.resolution, '?')}/",
            "INFO")

    # Verificar permissões (espelho do R)
    log_msg(logger, "[→] Verificando permissões de diretórios...", "INFO")
    for path, label in [(output_dir, "output-dir"), (worldclim_dir, "worldclim-dir")]:
        test = path / f".write_test_{__import__('os').getpid()}"
        try:
            test.write_text("test")
            test.unlink()
            log_msg(logger, f"[✓] {label} OK → {path}", "INFO")
        except Exception as e:
            log_msg(logger, f"[!] Sem permissão de escrita em {label}: {e}", "ERROR")
            sys.exit(1)

    # Verificar shapefile
    if not Path(args.shapefile).exists():
        log_msg(logger, f"[!] Shapefile não encontrado: {args.shapefile}", "ERROR")
        sys.exit(1)

    log_msg(logger, "[→] Carregando shapefile...", "INFO")
    try:
        shapes_gdf = load_shapefile(args.shapefile)
        log_msg(
            logger,
            f"[✓] Shapefile carregado: {Path(args.shapefile).name} | "
            f"CRS: {shapes_gdf.crs} | features: {len(shapes_gdf)}",
            "INFO",
        )
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
        elif var == ELEV_VAR:
            gen, skip, fail = process_elev(
                args.resolution, shapes_gdf,
                prefix, res_tag, output_dir, worldclim_dir,
                args.skip_existing, logger,
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

    log_msg(
        logger,
        f"[→] Resumo: gerados={len(all_generated)} "
        f"| pulados={len(all_skipped)} | falhos={len(all_failed)}",
        "INFO",
    )

    metrics.add("engine",          "python")
    metrics.add("generated_files", all_generated)
    metrics.add("skipped_files",   all_skipped)
    metrics.add("failed_vars",     all_failed)
    metrics.add("output_dir",      str(output_dir))
    metrics.save(output_dir / "metrics.json")
    log_msg(logger, "[✓] metrics.json salvo", "INFO")

    if all_failed:
        log_msg(logger, f"[!] Variáveis com falha: {', '.join(all_failed)}", "ERROR")
        sys.exit(1)

    log_msg(logger, "[★] gen_rasters concluído com sucesso", "INFO")
    sys.exit(0)


if __name__ == "__main__":
    main()