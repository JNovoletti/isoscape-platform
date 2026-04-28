#!/usr/bin/env python
"""
backend/python_scripts/gen_rasters.py

Python equivalent of gen_rasters.R
Downloads WorldClim climate data, clips to study area shapefile, and generates TIFs.

Usage:
    python gen_rasters.py \
      --job-id abc-123 \
      --shapefile /data/shapefiles/amazonia_legal.shp \
      --output-dir /data/rasters/project1/ \
      --worldclim-dir /data/worldclim_cache/ \
      --variables tavg,tmax,tmin,prec \
      --bio-layers bio1,bio2,bio3 \
      --resolution 5 \
      --skip-existing true
"""

import argparse
import sys
from pathlib import Path
from typing import List, Optional
import logging

import numpy as np
import rioxarray
import xarray as xr
from scipy.ndimage import zoom

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from python_scripts.utils import (
    setup_logging, log_msg, MetricsCollector, load_shapefile,
    ensure_output_dir, get_file_prefix, parse_csv_list
)


# =============================================================================
# WorldClim Data Access
# =============================================================================

def download_worldclim_bio(resolution: float, cache_dir: Path) -> xr.Dataset:
    """
    Download WorldClim bioclimatic variables.
    
    Args:
        resolution: Resolution in arc-minutes (2.5, 5, or 10)
        cache_dir: Directory to cache downloaded files
        
    Returns:
        xarray Dataset with bio layers (bio1-bio19)
    """
    # Use COG (Cloud-Optimized GeoTIFF) URLs for remote access
    # This avoids large local downloads
    base_url = f"https://www.worldclim.org/version2_1"
    
    # Try using earthpy for downloading
    try:
        import earthpy.io as eio
        cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Download via earthpy (which uses GDAL/urllib under the hood)
        # For simplicity, we'll use rioxarray to open remote COGs
        bio_url = f"{base_url}/bio/wc2.1_{int(resolution)}m/wc2.1_{int(resolution)}m_bio.tif"
        
        # Use rioxarray to open remote COG
        ds = rioxarray.open_rasterio(bio_url)
        return ds
    except Exception as e:
        raise Exception(f"Failed to download WorldClim bio data: {e}")


def download_worldclim_variable(variable: str, resolution: float, cache_dir: Path) -> xr.Dataset:
    """
    Download a single WorldClim variable.
    
    Variables supported: tavg, tmax, tmin, prec, srad, vapr, wind, elev
    
    Args:
        variable: Climate variable name
        resolution: Resolution in arc-minutes (2.5, 5, or 10)
        cache_dir: Directory to cache downloaded files
        
    Returns:
        xarray Dataset
    """
    # WorldClim COG URLs
    base_url = f"https://www.worldclim.org/version2_1/data"
    
    try:
        # Construct URL for monthly data (average)
        url = f"{base_url}/monthly/wc2.1_{int(resolution)}m_{variable}.tif"
        
        # Use rioxarray to open remote COG
        ds = rioxarray.open_rasterio(url)
        return ds
    except Exception as e:
        # Fallback: try different URL pattern
        try:
            url = f"https://worldclim.blob.core.windows.net/v2/2.1/wc2.1_{int(resolution)}m_{variable}.tif"
            ds = rioxarray.open_rasterio(url)
            return ds
        except Exception as e2:
            raise Exception(f"Failed to download WorldClim variable '{variable}': {e2}")


def clip_and_project_raster(raster: xr.Dataset, shapefile_gdf, target_crs: str = "EPSG:4674"):
    """
    Clip raster to shapefile bounds and project to target CRS.
    
    Args:
        raster: xarray Dataset or DataArray with spatial data
        shapefile_gdf: GeoDataFrame with study area geometry
        target_crs: Target CRS (default: SIRGAS 2000)
        
    Returns:
        Clipped and projected xarray Dataset
    """
    # Reproject if needed
    if raster.rio.crs is None or str(raster.rio.crs) != target_crs:
        raster = raster.rio.reproject(target_crs)
    
    # Get bounds from shapefile
    bounds = shapefile_gdf.total_bounds  # (minx, miny, maxx, maxy)
    
    # Clip to bounds
    clipped = raster.rio.clip_box(
        minx=bounds[0], miny=bounds[1],
        maxx=bounds[2], maxy=bounds[3]
    )
    
    # Clip to actual geometry (more precise)
    clipped = clipped.rio.clip(shapefile_gdf.geometry, from_disk=True, crs=target_crs)
    
    return clipped


def save_raster_to_tif(data: xr.DataArray, output_path: Path, overwrite: bool = True):
    """
    Save xarray DataArray to GeoTIFF.
    
    Args:
        data: xarray DataArray with spatial coordinates
        output_path: Path to save .tif file
        overwrite: Whether to overwrite existing file
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    if output_path.exists() and not overwrite:
        return
    
    data.rio.to_raster(output_path)


# =============================================================================
# Main Processing
# =============================================================================

def process_bio_layers(shapefile_gdf, variables: List[str], bio_layers: List[str],
                       resolution: float, output_dir: Path, metrics: MetricsCollector,
                       logger: logging.Logger, skip_existing: bool = True) -> tuple[List[str], List[str]]:
    """
    Process bioclimatic layers (bio1-bio19).
    
    Args:
        shapefile_gdf: Study area geometry
        variables: List of requested variables (should contain "bio")
        bio_layers: List of bio layers to keep (e.g., ["bio1", "bio2"])
        resolution: Resolution in arc-minutes
        output_dir: Output directory
        metrics: MetricsCollector instance
        logger: Logger instance
        skip_existing: Skip existing files
        
    Returns:
        Tuple of (generated_files, skipped_files)
    """
    if "bio" not in variables:
        return [], []
    
    generated = []
    skipped = []
    
    prefix = get_file_prefix(shapefile_gdf.name if hasattr(shapefile_gdf, 'name') else "study_area")
    res_tag = f"{int(resolution)}arc"
    
    log_msg(logger, "[→] Downloading: bio (WorldClim bioclim)", "INFO")
    
    try:
        bio_data = download_worldclim_bio(resolution, Path("/tmp/worldclim_cache"))
        bio_clipped = clip_and_project_raster(bio_data, shapefile_gdf)
        
        # Process each bio layer
        for layer_idx in range(bio_clipped.shape[0]):
            bio_num = layer_idx + 1  # bio1, bio2, ..., bio19
            bio_name = f"bio{bio_num}"
            
            if bio_layers and bio_name not in bio_layers:
                continue
            
            out_file = output_dir / f"{prefix}_{res_tag}_{bio_name}.tif"
            
            if skip_existing and out_file.exists():
                log_msg(logger, f"  [skip] {out_file.name}", "INFO")
                skipped.append(str(out_file))
                continue
            
            try:
                layer_data = bio_clipped.isel(band=layer_idx).drop_vars('band', errors='ignore')
                save_raster_to_tif(layer_data, out_file)
                log_msg(logger, f"  [✓] Saved: {out_file.name}", "INFO")
                generated.append(str(out_file))
            except Exception as e:
                log_msg(logger, f"  [!] Error saving {bio_name}: {str(e)}", "ERROR")
    
    except Exception as e:
        log_msg(logger, f"[!] Error processing bio layers: {str(e)}", "ERROR")
    
    return generated, skipped


def process_climate_variables(shapefile_gdf, variables: List[str],
                             resolution: float, output_dir: Path, metrics: MetricsCollector,
                             logger: logging.Logger, skip_existing: bool = True) -> tuple[List[str], List[str]]:
    """
    Process individual climate variables (tavg, tmax, tmin, prec, etc.).
    
    Args:
        shapefile_gdf: Study area geometry
        variables: List of climate variables to process
        resolution: Resolution in arc-minutes
        output_dir: Output directory
        metrics: MetricsCollector instance
        logger: Logger instance
        skip_existing: Skip existing files
        
    Returns:
        Tuple of (generated_files, skipped_files)
    """
    generated = []
    skipped = []
    failed = []
    
    prefix = get_file_prefix(shapefile_gdf.name if hasattr(shapefile_gdf, 'name') else "study_area")
    res_tag = f"{int(resolution)}arc"
    
    for var in variables:
        if var == "bio":
            continue  # Handled separately
        
        out_file = output_dir / f"{prefix}_{res_tag}_{var}_mean.tif"
        
        if skip_existing and out_file.exists():
            log_msg(logger, f"[skip] {out_file.name}", "INFO")
            skipped.append(str(out_file))
            continue
        
        log_msg(logger, f"[→] Downloading: {var}", "INFO")
        
        try:
            climate_data = download_worldclim_variable(var, resolution, Path("/tmp/worldclim_cache"))
            r_clipped = clip_and_project_raster(climate_data, shapefile_gdf)
            
            # If multiple layers (e.g., 12 months), average them
            if r_clipped.ndim > 2 and 'band' in r_clipped.dims:
                r_out = r_clipped.mean(dim='band')
            else:
                r_out = r_clipped
            
            save_raster_to_tif(r_out, out_file)
            log_msg(logger, f"[✓] Saved: {out_file.name}", "INFO")
            generated.append(str(out_file))
        
        except Exception as e:
            log_msg(logger, f"[!] Error downloading {var}: {str(e)}", "ERROR")
            failed.append(var)
    
    return generated, skipped, failed


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Download WorldClim data and clip to study area shapefile",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python gen_rasters.py --job-id j1 --shapefile area.shp --output-dir output/ \
    --worldclim-dir cache/ --variables tavg,tmax,bio
        """
    )
    
    parser.add_argument("--job-id", required=True, help="Job identifier")
    parser.add_argument("--shapefile", required=True, help="Path to study area shapefile")
    parser.add_argument("--output-dir", required=True, help="Output directory for TIFs")
    parser.add_argument("--worldclim-dir", required=True, help="WorldClim cache directory")
    parser.add_argument("--variables", required=True, help="Comma-separated variable list (e.g., tavg,tmax,bio)")
    parser.add_argument("--bio-layers", default="", help="Comma-separated bio layers (e.g., bio1,bio2,bio12)")
    parser.add_argument("--resolution", type=float, default=5, help="Resolution in arc-minutes (2.5, 5, 10)")
    parser.add_argument("--skip-existing", type=lambda x: x.lower() == 'true', default=True,
                       help="Skip already-processed files (true/false)")
    
    args = parser.parse_args()
    
    # Initialize output directory and logging
    output_dir = ensure_output_dir(Path(args.output_dir))
    logger = setup_logging(output_dir, args.job_id, "gen_rasters")
    metrics = MetricsCollector(args.job_id)
    
    log_msg(logger, f"[→] Job gen_rasters initiated: {args.job_id}", "INFO")
    log_msg(logger, f"[→] Shapefile: {args.shapefile}", "INFO")
    log_msg(logger, f"[→] Variables: {args.variables}", "INFO")
    log_msg(logger, f"[→] Resolution: {args.resolution} arc-min", "INFO")
    log_msg(logger, f"[→] Skip existing: {args.skip_existing}", "INFO")
    
    try:
        # Load shapefile
        log_msg(logger, "[→] Loading shapefile...", "INFO")
        shapefile_gdf = load_shapefile(args.shapefile)
        log_msg(logger, f"[✓] Shapefile loaded: {Path(args.shapefile).name}", "INFO")
        
        # Parse variables and bio layers
        variables = parse_csv_list(args.variables)
        bio_layers = parse_csv_list(args.bio_layers) if args.bio_layers else [f"bio{i}" for i in range(1, 20)]
        
        all_generated = []
        all_skipped = []
        all_failed = []
        
        # Process bio layers
        if "bio" in variables:
            gen, skipped = process_bio_layers(
                shapefile_gdf, variables, bio_layers,
                args.resolution, output_dir, metrics, logger,
                args.skip_existing
            )
            all_generated.extend(gen)
            all_skipped.extend(skipped)
        
        # Process climate variables
        climate_vars = [v for v in variables if v != "bio"]
        if climate_vars:
            gen, skipped, failed = process_climate_variables(
                shapefile_gdf, climate_vars,
                args.resolution, output_dir, metrics, logger,
                args.skip_existing
            )
            all_generated.extend(gen)
            all_skipped.extend(skipped)
            all_failed.extend(failed)
        
        # Save metrics
        metrics.add("generated_files", all_generated)
        metrics.add("skipped_files", all_skipped)
        metrics.add("failed_vars", all_failed)
        metrics.add("output_dir", str(output_dir))
        metrics_path = output_dir / "metrics.json"
        metrics.save(metrics_path)
        
        log_msg(logger, f"[✓] metrics.json saved: {metrics_path}", "INFO")
        
        if all_failed:
            log_msg(logger, f"[!] Variables with failures: {', '.join(all_failed)}", "WARNING")
            sys.exit(1)
        
        log_msg(logger, f"[★] gen_rasters completed successfully", "INFO")
        sys.exit(0)
    
    except Exception as e:
        log_msg(logger, f"[!] Fatal error: {str(e)}", "ERROR")
        metrics.add("error", str(e))
        metrics.save(output_dir / "metrics.json")
        sys.exit(1)


if __name__ == "__main__":
    main()
