"""
backend/python_scripts/utils.py

Shared utilities for gen_rasters.py and run_isoscape.py.
- Logging: structured logging with timestamps
- Metrics: JSON serialization and metrics management
- File I/O: raster/shapefile loading
- CLI: argument parsing and validation
"""

import json
import logging
import sys
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime

import geopandas as gpd
import rasterio
import rioxarray
import xarray as xr
import pandas as pd


# =============================================================================
# Logging Setup
# =============================================================================

def setup_logging(output_dir: Path, job_id: str, script_name: str = "script") -> logging.Logger:
    """
    Configure logging to write to both stdout and a log file.
    
    Args:
        output_dir: Directory to save log file
        job_id: Job identifier for log file naming
        script_name: Script name for logger identification
        
    Returns:
        Configured logger instance
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "log.txt"
    
    logger = logging.getLogger(script_name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()  # Remove default handlers
    
    # Formatter matching R script style: [HH:MM:SS] [prefix] message
    formatter = logging.Formatter('[%(asctime)s] %(levelname)s — %(message)s', 
                                 datefmt='%H:%M:%S')
    
    # File handler
    fh = logging.FileHandler(log_file, mode='w')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    
    # Console handler (stdout)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    
    return logger


def log_msg(logger: logging.Logger, message: str, level: str = "INFO"):
    """
    Log a message using the configured logger.
    Mirrors R script style with emoji-like markers:
    - [→] starting a process
    - [✓] completed successfully
    - [!] warning/error
    - [★] final completion
    """
    if level == "INFO":
        logger.info(message)
    elif level == "WARNING":
        logger.warning(message)
    elif level == "ERROR":
        logger.error(message)
    elif level == "DEBUG":
        logger.debug(message)


# =============================================================================
# Metrics and JSON Serialization
# =============================================================================

class MetricsCollector:
    """Collects and serializes metrics during script execution."""
    
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.data: Dict[str, Any] = {
            "job_id": job_id,
            "timestamp": datetime.now().isoformat(),
        }
    
    def add(self, key: str, value: Any):
        """Add a metric value."""
        self.data[key] = value
    
    def to_dict(self) -> Dict[str, Any]:
        """Return metrics as dictionary."""
        return self.data
    
    def save(self, output_path: Path):
        """Save metrics to JSON file."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w') as f:
            json.dump(self.data, f, indent=2, default=str)


# =============================================================================
# Geospatial I/O
# =============================================================================

def load_shapefile(shapefile_path: str | Path) -> gpd.GeoDataFrame:
    """
    Load a shapefile and return as GeoDataFrame.
    
    Args:
        shapefile_path: Path to .shp file
        
    Returns:
        GeoDataFrame with geometry
        
    Raises:
        FileNotFoundError: If shapefile doesn't exist
        Exception: If file is invalid
    """
    shapefile_path = Path(shapefile_path)
    if not shapefile_path.exists():
        raise FileNotFoundError(f"Shapefile not found: {shapefile_path}")
    
    try:
        gdf = gpd.read_file(shapefile_path)
        # Ensure CRS is set (default to WGS84 if missing)
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4674")  # SIRGAS 2000
        return gdf
    except Exception as e:
        raise Exception(f"Error loading shapefile: {e}")


def load_raster_stack(raster_dir: str | Path, pattern: str = "*.tif") -> tuple[xr.DataArray, Dict[str, Path]]:
    """
    Load all rasters from a directory into a stack.
    
    Args:
        raster_dir: Directory containing .tif files
        pattern: Glob pattern to filter files (default: *.tif)
        
    Returns:
        Tuple of (stacked DataArray, dict mapping names to paths)
        
    Raises:
        FileNotFoundError: If no rasters found
    """
    raster_dir = Path(raster_dir)
    raster_files = sorted(raster_dir.glob(pattern))
    
    if not raster_files:
        raise FileNotFoundError(f"No rasters matching pattern '{pattern}' in {raster_dir}")
    
    rasters = {}
    for file_path in raster_files:
        name = file_path.stem
        rasters[name] = file_path
    
    # Load all rasters using rioxarray
    data_arrays = {}
    for name, path in rasters.items():
        try:
            da = rioxarray.open_rasterio(path).squeeze('band', drop=True)
            data_arrays[name] = da
        except Exception as e:
            raise Exception(f"Error loading raster {path}: {e}")
    
    # Stack into single DataArray
    stacked = xr.concat(data_arrays.values(), dim=xr.Variable('variable', list(data_arrays.keys())))
    
    return stacked, rasters


def load_dataset(dataset_path: str | Path, lat_col: str = "latitude", 
                 lon_col: str = "longitude", response_col: str = "response") -> tuple:
    """
    Load dataset from CSV or XLSX, return dataframe and spatial data.
    
    Args:
        dataset_path: Path to CSV or XLSX file
        lat_col: Name of latitude column
        lon_col: Name of longitude column
        response_col: Name of response variable column
        
    Returns:
        Tuple of (DataFrame, GeoDataFrame, response Series)
        
    Raises:
        ValueError: If required columns missing
        Exception: If file format unsupported
    """
    dataset_path = Path(dataset_path)
    
    if not dataset_path.exists():
        raise FileNotFoundError(f"Dataset not found: {dataset_path}")
    
    # Load based on file extension
    ext = dataset_path.suffix.lower()
    if ext == ".csv":
        df = pd.read_csv(dataset_path)
    elif ext in [".xlsx", ".xls"]:
        df = pd.read_excel(dataset_path)
    else:
        raise Exception(f"Unsupported file format: {ext}")
    
    # Check required columns
    required_cols = [lat_col, lon_col, response_col]
    missing = [col for col in required_cols if col not in df.columns]
    if missing:
        raise ValueError(f"Missing columns: {', '.join(missing)}")
    
    # Create spatial dataframe (CRS: SIRGAS 2000)
    gdf = gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df[lon_col], df[lat_col]),
        crs="EPSG:4674"
    )
    
    # Rename to standard names
    gdf = gdf.rename(columns={
        lat_col: "latitude",
        lon_col: "longitude", 
        response_col: "response"
    })
    
    # Validate numeric columns
    for col in ["latitude", "longitude", "response"]:
        if not all(gdf[col].apply(lambda x: isinstance(x, (int, float)) or pd.isna(x))):
            raise ValueError(f"Column '{col}' must be numeric")
    
    response = gdf["response"]
    
    return df, gdf, response


# =============================================================================
# CLI and Validation
# =============================================================================

def validate_required_args(args: Dict[str, Any], required: List[str]) -> List[str]:
    """
    Validate that required CLI arguments are present.
    
    Args:
        args: Dictionary of parsed arguments
        required: List of required argument names
        
    Returns:
        List of missing argument names (empty if all present)
    """
    missing = [arg for arg in required if arg not in args or args[arg] is None]
    return missing


def parse_csv_list(value: str, default: Optional[List[str]] = None) -> List[str]:
    """
    Parse comma-separated string into list.
    
    Args:
        value: Comma-separated string (e.g., "tavg,tmax,prec")
        default: Default list if value is empty
        
    Returns:
        List of trimmed values
    """
    if not value or not str(value).strip():
        return default or []
    return [v.strip() for v in str(value).split(",")]


# =============================================================================
# File and Directory Utilities
# =============================================================================

def ensure_output_dir(output_dir: Path) -> Path:
    """
    Ensure output directory exists.
    
    Args:
        output_dir: Path to create
        
    Returns:
        Path object (created if necessary)
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def get_file_prefix(shapefile_path: str | Path) -> str:
    """
    Extract file prefix from shapefile name.
    Removes extension and special characters.
    
    Example:
        "amazonia_legal.shp" → "amazonia_legal"
        
    Args:
        shapefile_path: Path to shapefile
        
    Returns:
        Cleaned prefix string
    """
    stem = Path(shapefile_path).stem
    # Replace special chars with underscore, keep only alphanumeric and underscore
    import re
    prefix = re.sub(r'[^A-Za-z0-9_]', '_', stem)
    return prefix
