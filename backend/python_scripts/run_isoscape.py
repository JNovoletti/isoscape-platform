#!/usr/bin/env python
"""
backend/python_scripts/run_isoscape.py

Python equivalent of run_isoscape.R
Trains Random Forest model on sample data + raster predictors to generate isoscape.

Usage:
    python run_isoscape.py \
      --job-id abc-123 \
      --dataset-path /data/datasets/samples.csv \
      --raster-dir /data/rasters/project1/ \
      --output-dir /data/isoscapes/abc-123/ \
      --response-col d13C \
      --lat-col latitude \
      --lon-col longitude \
      --uncertainty quantile_rf \
      --resolution 5 \
      --seed 1350
"""

import argparse
import sys
from pathlib import Path
from typing import Optional, Tuple
import logging
import warnings

import numpy as np
import pandas as pd
import rasterio
import rioxarray
import xarray as xr
from scipy import stats
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import mean_squared_error, r2_score
import geopandas as gpd
from shapely.geometry import Point

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from python_scripts.utils import (
    setup_logging, log_msg, MetricsCollector, load_shapefile, load_dataset,
    ensure_output_dir, load_raster_stack
)

warnings.filterwarnings('ignore')


# =============================================================================
# Feature Selection (VSURF Equivalent)
# =============================================================================

class RecursiveFeatureEliminator:
    """
    Simplified VSURF equivalent using recursive feature elimination.
    Selects features based on feature importance from Random Forest.
    """
    
    def __init__(self, n_features_to_select: Optional[int] = None, n_iterations: int = 10):
        self.n_features_to_select = n_features_to_select
        self.n_iterations = n_iterations
        self.selected_features = None
        self.feature_importance = None
    
    def select(self, X: np.ndarray, y: np.ndarray, feature_names: list) -> list:
        """
        Select features using recursive elimination with Random Forest.
        
        Args:
            X: Feature matrix (n_samples, n_features)
            y: Target vector (n_samples,)
            feature_names: Names of features
            
        Returns:
            List of selected feature names
        """
        features = list(feature_names)
        importances = []
        
        for iteration in range(self.n_iterations):
            # Train RF on current features
            rf = RandomForestRegressor(
                n_estimators=100,
                max_depth=15,
                min_samples_leaf=5,
                random_state=42,
                n_jobs=-1
            )
            
            # Select features by index
            feature_indices = [i for i, name in enumerate(feature_names) if name in features]
            X_subset = X[:, feature_indices]
            
            rf.fit(X_subset, y)
            
            # Get importance for current features
            for feat_name, importance in zip(features, rf.feature_importances_):
                importances.append((feat_name, importance))
            
            # Remove worst performing features (bottom 20%)
            if len(features) > max(3, len(features) // 3):
                importances.sort(key=lambda x: x[1], reverse=True)
                keep_count = max(3, int(len(features) * 0.8))
                features = [name for name, _ in importances[:keep_count]]
                importances = []
        
        # Final RF to get importance scores
        rf = RandomForestRegressor(
            n_estimators=500,
            max_depth=20,
            min_samples_leaf=5,
            random_state=42,
            n_jobs=-1
        )
        
        feature_indices = [i for i, name in enumerate(feature_names) if name in features]
        X_subset = X[:, feature_indices]
        rf.fit(X_subset, y)
        
        # Store importance
        self.feature_importance = dict(zip(features, rf.feature_importances_))
        self.selected_features = sorted(features, 
                                       key=lambda x: self.feature_importance[x], 
                                       reverse=True)
        
        return self.selected_features


# =============================================================================
# Raster Prediction and Uncertainty
# =============================================================================

def predict_raster_stack(raster_stack: xr.Dataset, rf_model: RandomForestRegressor,
                        feature_names: list) -> xr.DataArray:
    """
    Make spatial predictions using trained RF model on raster stack.
    
    Args:
        raster_stack: xarray Dataset with rasters
        rf_model: Trained RandomForestRegressor
        feature_names: Names of features in order
        
    Returns:
        xarray DataArray with predictions (same spatial coords as input)
    """
    # Stack rasters into matrix (n_pixels, n_features)
    # Keep track of spatial coordinates
    
    # Get template raster for coordinates
    first_var = list(raster_stack.data_vars.keys())[0]
    template = raster_stack[first_var]
    
    # Create feature matrix
    n_features = len(feature_names)
    shape = template.shape
    X_raster = np.zeros((shape[0] * shape[1], n_features))
    
    for i, feat_name in enumerate(feature_names):
        if feat_name in raster_stack:
            data = raster_stack[feat_name].values
            X_raster[:, i] = data.flatten()
    
    # Predict
    predictions = rf_model.predict(X_raster)
    predictions = predictions.reshape(shape)
    
    # Create DataArray with spatial coordinates
    pred_da = xr.DataArray(
        predictions,
        coords={
            'y': template.y,
            'x': template.x
        },
        dims=['y', 'x'],
        name='isoscape'
    )
    pred_da = pred_da.rio.write_crs(template.rio.crs)
    
    return pred_da


def compute_uncertainty_quantile_rf(raster_stack: xr.Dataset, 
                                   X_train: np.ndarray, y_train: np.ndarray,
                                   feature_names: list, shape: Tuple[int, int]) -> xr.DataArray:
    """
    Compute uncertainty using quantile regression forests.
    
    Uses sklearn's RandomForestRegressor with quantile loss.
    Computes 0.16 and 0.84 quantiles to estimate ~1 std dev.
    
    Args:
        raster_stack: xarray Dataset with rasters
        X_train: Training features
        y_train: Training target
        feature_names: Feature names
        shape: Shape of raster (height, width)
        
    Returns:
        xarray DataArray with uncertainty (std dev) estimates
    """
    # Train RF for lower quantile
    rf_lower = RandomForestRegressor(
        n_estimators=200,
        max_depth=20,
        min_samples_leaf=5,
        loss='quantile',
        alpha=0.16,
        random_state=42,
        n_jobs=-1
    )
    rf_lower.fit(X_train, y_train)
    
    # Train RF for upper quantile
    rf_upper = RandomForestRegressor(
        n_estimators=200,
        max_depth=20,
        min_samples_leaf=5,
        loss='quantile',
        alpha=0.84,
        random_state=42,
        n_jobs=-1
    )
    rf_upper.fit(X_train, y_train)
    
    # Get template for coordinates
    first_var = list(raster_stack.data_vars.keys())[0]
    template = raster_stack[first_var]
    
    # Create feature matrix for rasters
    n_features = len(feature_names)
    X_raster = np.zeros((shape[0] * shape[1], n_features))
    
    for i, feat_name in enumerate(feature_names):
        if feat_name in raster_stack:
            data = raster_stack[feat_name].values
            X_raster[:, i] = data.flatten()
    
    # Predict quantiles
    pred_lower = rf_lower.predict(X_raster).reshape(shape)
    pred_upper = rf_upper.predict(X_raster).reshape(shape)
    
    # Uncertainty = (upper - lower) / 2 ≈ 1 std dev
    uncertainty = (pred_upper - pred_lower) / 2
    
    # Create DataArray
    unc_da = xr.DataArray(
        uncertainty,
        coords={
            'y': template.y,
            'x': template.x
        },
        dims=['y', 'x'],
        name='uncertainty'
    )
    unc_da = unc_da.rio.write_crs(template.rio.crs)
    
    return unc_da


def compute_uncertainty_bootstrap(raster_stack: xr.Dataset,
                                 X_train: np.ndarray, y_train: np.ndarray,
                                 feature_names: list, shape: Tuple[int, int],
                                 n_bootstrap: int = 50) -> xr.DataArray:
    """
    Compute uncertainty using bootstrap resampling.
    
    Trains multiple RF models on bootstrap samples, computes predictions,
    and estimates std dev.
    
    Args:
        raster_stack: xarray Dataset with rasters
        X_train: Training features
        y_train: Training target
        feature_names: Feature names
        shape: Shape of raster (height, width)
        n_bootstrap: Number of bootstrap samples
        
    Returns:
        xarray DataArray with uncertainty (std dev) estimates
    """
    np.random.seed(42)
    
    # Get template for coordinates
    first_var = list(raster_stack.data_vars.keys())[0]
    template = raster_stack[first_var]
    
    # Create feature matrix for rasters
    n_features = len(feature_names)
    X_raster = np.zeros((shape[0] * shape[1], n_features))
    
    for i, feat_name in enumerate(feature_names):
        if feat_name in raster_stack:
            data = raster_stack[feat_name].values
            X_raster[:, i] = data.flatten()
    
    # Bootstrap predictions
    predictions = np.zeros((n_bootstrap, shape[0] * shape[1]))
    
    for b in range(n_bootstrap):
        # Bootstrap sample
        indices = np.random.choice(len(X_train), size=len(X_train), replace=True)
        X_boot = X_train[indices]
        y_boot = y_train[indices]
        
        # Train RF
        rf = RandomForestRegressor(
            n_estimators=200,
            max_depth=20,
            min_samples_leaf=5,
            random_state=42,
            n_jobs=-1
        )
        rf.fit(X_boot, y_boot)
        
        # Predict
        predictions[b, :] = rf.predict(X_raster)
    
    # Compute std dev across bootstrap samples
    uncertainty = np.std(predictions, axis=0).reshape(shape)
    
    # Create DataArray
    unc_da = xr.DataArray(
        uncertainty,
        coords={
            'y': template.y,
            'x': template.x
        },
        dims=['y', 'x'],
        name='uncertainty'
    )
    unc_da = unc_da.rio.write_crs(template.rio.crs)
    
    return unc_da


# =============================================================================
# Main Processing
# =============================================================================

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Train isoscape model and generate predictions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_isoscape.py --job-id j1 --dataset-path samples.csv \
    --raster-dir rasters/ --output-dir output/ --response-col d13C
        """
    )
    
    parser.add_argument("--job-id", required=True, help="Job identifier")
    parser.add_argument("--dataset-path", required=True, help="Path to CSV or XLSX with samples")
    parser.add_argument("--raster-dir", required=True, help="Directory with raster TIFs")
    parser.add_argument("--output-dir", required=True, help="Output directory for results")
    parser.add_argument("--response-col", required=True, help="Response variable column name")
    parser.add_argument("--lat-col", default="latitude", help="Latitude column name")
    parser.add_argument("--lon-col", default="longitude", help="Longitude column name")
    parser.add_argument("--uncertainty", default="quantile_rf",
                       choices=["quantile_rf", "bootstrap"],
                       help="Uncertainty estimation method")
    parser.add_argument("--resolution", type=float, default=5, help="Raster resolution (arc-min)")
    parser.add_argument("--seed", type=int, default=1350, help="Random seed")
    
    args = parser.parse_args()
    
    # Initialize
    output_dir = ensure_output_dir(Path(args.output_dir))
    logger = setup_logging(output_dir, args.job_id, "run_isoscape")
    metrics = MetricsCollector(args.job_id)
    
    np.random.seed(args.seed)
    
    log_msg(logger, f"[→] Job initiated: {args.job_id}", "INFO")
    log_msg(logger, f"[→] Dataset: {args.dataset_path}", "INFO")
    log_msg(logger, f"[→] Rasters: {args.raster_dir}", "INFO")
    log_msg(logger, f"[→] Resolution: {args.resolution} arc-min", "INFO")
    log_msg(logger, f"[→] Uncertainty: {args.uncertainty}", "INFO")
    
    try:
        # 1. Load dataset
        log_msg(logger, "[→] Reading dataset...", "INFO")
        df, gdf, response = load_dataset(
            args.dataset_path,
            lat_col=args.lat_col,
            lon_col=args.lon_col,
            response_col=args.response_col
        )
        log_msg(logger, f"[✓] Dataset loaded: {len(df)} rows", "INFO")
        
        # 2. Load raster stack
        log_msg(logger, "[→] Loading rasters...", "INFO")
        raster_dir = Path(args.raster_dir)
        raster_files = sorted(raster_dir.glob("*.tif"))
        
        if not raster_files:
            raise FileNotFoundError(f"No TIFs found in {raster_dir}")
        
        # Load rasters into xarray Dataset
        raster_dict = {}
        for raster_file in raster_files:
            name = raster_file.stem
            da = rioxarray.open_rasterio(raster_file).squeeze('band', drop=True)
            raster_dict[name] = da
        
        raster_stack = xr.Dataset(raster_dict)
        log_msg(logger, f"[✓] Rasters loaded: {len(raster_files)} layers", "INFO")
        
        # 3. Extract values at sample points
        log_msg(logger, "[→] Extracting values at sample points...", "INFO")
        
        extracted_values = []
        for idx, row in gdf.iterrows():
            point = row.geometry
            values = {}
            
            for var_name in raster_dict.keys():
                da = raster_dict[var_name]
                # Extract value at point (nearest neighbor or bilinear)
                try:
                    val = da.sel(x=point.x, y=point.y, method='nearest').values
                    values[var_name] = float(val)
                except:
                    values[var_name] = np.nan
            
            extracted_values.append(values)
        
        extracted_df = pd.DataFrame(extracted_values)
        df_model = pd.concat([gdf[['latitude', 'longitude', 'response']], extracted_df], axis=1)
        df_model = df_model.dropna()
        
        if len(df_model) == 0:
            raise ValueError("No valid data after extraction. Check if points are within raster bounds.")
        
        log_msg(logger, f"[✓] Extraction complete: {len(df_model)} valid points, {len(extracted_df.columns)} predictors", "INFO")
        
        # 4. Feature selection (VSURF-like)
        log_msg(logger, "[→] Running feature selection (recursive elimination)...", "INFO")
        
        X = df_model[extracted_df.columns].values
        y = df_model['response'].values
        
        selector = RecursiveFeatureEliminator(n_iterations=10)
        selected_vars = selector.select(X, y, extracted_df.columns.tolist())
        
        log_msg(logger, f"[✓] Feature selection complete. Selected variables: {', '.join(selected_vars)}", "INFO")
        
        # 5. Train Random Forest
        log_msg(logger, "[→] Training Random Forest (ntree=500)...", "INFO")
        
        # Select features
        X_selected = df_model[selected_vars].values
        
        # Split train/test
        X_train, X_test, y_train, y_test = train_test_split(
            X_selected, y,
            test_size=0.2,
            random_state=args.seed
        )
        
        # Train model
        rf_model = RandomForestRegressor(
            n_estimators=500,
            max_depth=20,
            min_samples_leaf=5,
            random_state=args.seed,
            n_jobs=-1
        )
        rf_model.fit(X_train, y_train)
        
        # Evaluate
        y_pred = rf_model.predict(X_test)
        mse = mean_squared_error(y_test, y_pred)
        r2 = r2_score(y_test, y_pred)
        
        log_msg(logger, f"[✓] RF trained. MSE = {mse:.4f} | R² = {r2:.4f}", "INFO")
        
        # 6. Predict isoscape
        log_msg(logger, "[→] Generating isoscape (spatial prediction)...", "INFO")
        
        # Prepare raster stack with only selected variables
        raster_stack_selected = xr.Dataset({var: raster_dict[var] for var in selected_vars})
        
        isoscape_pred = predict_raster_stack(raster_stack_selected, rf_model, selected_vars)
        
        log_msg(logger, "[✓] Isoscape generated", "INFO")
        
        # 7. Compute uncertainty
        log_msg(logger, f"[→] Computing uncertainty via {args.uncertainty}...", "INFO")
        
        # Create raster feature matrix for uncertainty
        shape = (len(raster_dict[selected_vars[0]].y), len(raster_dict[selected_vars[0]].x))
        
        if args.uncertainty == "quantile_rf":
            uncertainty = compute_uncertainty_quantile_rf(
                raster_stack_selected, X_selected, y,
                selected_vars, shape
            )
        else:  # bootstrap
            uncertainty = compute_uncertainty_bootstrap(
                raster_stack_selected, X_selected, y,
                selected_vars, shape,
                n_bootstrap=50
            )
        
        log_msg(logger, "[✓] Uncertainty map generated", "INFO")
        
        # 8. Save outputs
        log_msg(logger, "[→] Saving outputs...", "INFO")
        
        iso_path = output_dir / "isoscape.tif"
        unc_path = output_dir / "uncertainty.tif"
        
        isoscape_pred.rio.to_raster(iso_path)
        uncertainty.rio.to_raster(unc_path)
        
        log_msg(logger, f"[✓] isoscape.tif saved: {iso_path}", "INFO")
        log_msg(logger, f"[✓] uncertainty.tif saved: {unc_path}", "INFO")
        
        # 9. Save metrics
        metrics.add("MSE", float(mse))
        metrics.add("R2", float(r2))
        metrics.add("threshold_vars", [])  # Placeholder for VSURF compatibility
        metrics.add("interp_vars", [])
        metrics.add("pred_vars", selected_vars)
        metrics.add("isoscape_path", str(iso_path))
        metrics.add("uncertainty_path", str(unc_path))
        
        metrics_path = output_dir / "metrics.json"
        metrics.save(metrics_path)
        
        log_msg(logger, f"[✓] metrics.json saved: {metrics_path}", "INFO")
        log_msg(logger, "[★] Job completed successfully", "INFO")
        
        sys.exit(0)
    
    except Exception as e:
        log_msg(logger, f"[!] Fatal error: {str(e)}", "ERROR")
        import traceback
        log_msg(logger, traceback.format_exc(), "ERROR")
        metrics.add("error", str(e))
        metrics.save(output_dir / "metrics.json")
        sys.exit(1)


if __name__ == "__main__":
    main()
