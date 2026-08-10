"""
Utility Functions for SAA Optimization

This module contains helper functions, validation utilities, and common
operations used throughout the SAA optimization process.
"""

import numpy as np
import pandas as pd
from datetime import datetime
import importlib
import sys
from config import (
    MIN_EIGENVALUE_THRESHOLD, 
    MATRIX_REGULARIZATION,
    PERCENTAGE_DECIMAL_PLACES
)


def ensure_positive_definite(matrix, name="matrix"):
    """
    Ensure a covariance matrix is positive definite
    
    Parameters:
    matrix (np.array): Covariance matrix to check and fix
    name (str): Name of the matrix for logging purposes
    
    Returns:
    np.array: Positive definite matrix
    bool: Whether adjustment was needed
    """
    # Check if it's positive definite
    min_eigenvalue = np.min(np.linalg.eigvals(matrix))
    adjustment_needed = False
    
    if min_eigenvalue <= 0:
        print(f"Warning: {name} is not positive definite (min eigenvalue: {min_eigenvalue:.6f})")
        # Add small diagonal adjustment to ensure positive definiteness
        adjustment = abs(min_eigenvalue) + MIN_EIGENVALUE_THRESHOLD
        matrix = matrix + np.eye(len(matrix)) * adjustment
        print(f"Applied adjustment of {adjustment:.6f} to ensure positive definiteness")
        adjustment_needed = True
    
    return matrix, adjustment_needed


def get_config_value(param_name):
    """
    Return a configuration parameter from config.py, ensuring fresh values
    after edits by reloading the module if already imported.
    """
    if 'config' in sys.modules:
        importlib.reload(sys.modules['config'])
    import config  # local import on purpose
    return getattr(config, param_name)


def handle_nan_inf_values(matrix, name="matrix"):
    """
    Handle NaN or infinite values in a matrix
    
    Parameters:
    matrix (np.array): Matrix to clean
    name (str): Name of the matrix for logging purposes
    
    Returns:
    np.array: Cleaned matrix
    bool: Whether cleaning was needed
    """
    cleaning_needed = False
    
    # Check for NaN or infinite values
    if np.isnan(matrix).any() or np.isinf(matrix).any():
        print(f"Warning: NaN or infinite values detected in {name}")
        # Replace NaN/inf with small values
        mask_nan_inf = np.logical_or(np.isnan(matrix), np.isinf(matrix))
        matrix = np.where(mask_nan_inf, MATRIX_REGULARIZATION, matrix)
        cleaning_needed = True
        print(f"Replaced NaN/inf values in {name}")
    
    return matrix, cleaning_needed


def validate_matrix_dimensions(matrices_dict, expected_size):
    """
    Validate that all matrices have expected dimensions
    
    Parameters:
    matrices_dict (dict): Dictionary of matrix_name -> matrix
    expected_size (int): Expected matrix dimension
    
    Raises:
    ValueError: If any matrix has incorrect dimensions
    """
    for name, matrix in matrices_dict.items():
        if hasattr(matrix, 'shape'):
            if len(matrix.shape) == 2:  # 2D matrix
                if matrix.shape[0] != expected_size or matrix.shape[1] != expected_size:
                    raise ValueError(f"{name} has shape {matrix.shape}, expected ({expected_size}, {expected_size})")
            elif len(matrix.shape) == 1:  # 1D vector
                if matrix.shape[0] != expected_size:
                    raise ValueError(f"{name} has length {matrix.shape[0]}, expected {expected_size}")


def calculate_portfolio_risk(weights, cov_matrix):
    """
    Calculate portfolio volatility with numerical stability improvements
    
    Parameters:
    weights (np.array): Portfolio weights
    cov_matrix (np.array): Covariance matrix
    
    Returns:
    float: Portfolio volatility
    """
    quad_form = np.dot(weights.T, np.dot(cov_matrix, weights))
    # Safeguard against small negative values due to numerical precision
    if quad_form < 0:
        print(f"Warning: Negative quadratic form detected ({quad_form:.2e}). Using absolute value.")
        quad_form = abs(quad_form)
    return np.sqrt(quad_form)


def calculate_tracking_error(weights, benchmark_weights, cov_matrix):
    """
    Calculate tracking error between two portfolios
    
    Parameters:
    weights (np.array): Portfolio weights
    benchmark_weights (np.array): Benchmark weights
    cov_matrix (np.array): Covariance matrix
    
    Returns:
    float: Tracking error
    """
    diff = weights - benchmark_weights
    quad_form = np.dot(diff.T, np.dot(cov_matrix, diff))
    if quad_form < 0:
        print(f"Warning: Negative quadratic form detected in TE ({quad_form:.2e}). Using absolute value.")
        quad_form = abs(quad_form)
    return np.sqrt(quad_form)


def normalize_weights(weights):
    """
    Normalize weights to sum to 1.0
    
    Parameters:
    weights (np.array): Portfolio weights
    
    Returns:
    np.array: Normalized weights
    """
    return weights / np.sum(weights)


def format_percentage_columns(df):
    """
    Format percentage columns in dataframe
    
    Parameters:
    df (pd.DataFrame): DataFrame to format
    """
    # Columns that should be formatted as percentages
    pct_columns = ['Market Weight', 'Equilibrium Weight', 'Dynamic Weight', 
                  'Active Weight', 'Active vs Market', 'Active vs Equilibrium',
                  'Target Risk', 'Equilibrium Risk', 'Dynamic Risk', 
                  'Equilibrium TE', 'Dynamic TE', 'Expected Return', 
                  'Tracking Error', 'Active Risk Budget']
    
    for col in pct_columns:
        if col in df.columns:
            # Don't format if column contains strings
            if df[col].dtype != 'object':
                df[col] = df[col].round(PERCENTAGE_DECIMAL_PLACES)


def log_time_elapsed(start_time, message):
    """
    Log elapsed time since start_time
    
    Parameters:
    start_time (datetime): Start time
    message (str): Message to log
    
    Returns:
    float: Elapsed time in seconds
    """
    elapsed = (datetime.now() - start_time).total_seconds()
    print(f"{message}: {elapsed:.1f} seconds")
    return elapsed


def create_cluster_mapping(assets, asset_clusters):
    """
    Create cluster mapping matrices for optimization
    
    Parameters:
    assets (list): List of asset names
    asset_clusters (dict): Dictionary mapping assets to clusters
    
    Returns:
    tuple: (unique_clusters, cluster_map, reverse_cluster_map)
    """
    unique_clusters = sorted(set(asset_clusters.values()))
    cluster_map = {i: cluster for i, cluster in enumerate(unique_clusters)}
    reverse_cluster_map = {cluster: i for i, cluster in enumerate(unique_clusters)}
    
    return unique_clusters, cluster_map, reverse_cluster_map


def winsorize_outliers(returns, z_threshold=4):
    """
    Winsorize extreme outliers in returns data
    
    Parameters:
    returns (pd.DataFrame): Returns data
    z_threshold (float): Z-score threshold for outlier detection
    
    Returns:
    pd.DataFrame: Winsorized returns
    int: Number of outliers found
    """
    std_returns = returns.std()
    mean_returns = returns.mean()
    z_scores = (returns - mean_returns) / std_returns
    outlier_mask = (z_scores.abs() > z_threshold).any(axis=1)
    outlier_count = outlier_mask.sum()
    
    if outlier_count > 0:
        print(f"Detected {outlier_count} extreme outliers (z-score > {z_threshold})")
        # Winsorize extreme returns rather than removing them
        for col in returns.columns:
            returns[col] = np.clip(
                returns[col], 
                mean_returns[col] - z_threshold * std_returns[col],
                mean_returns[col] + z_threshold * std_returns[col]
            )
        print("Winsorized extreme values to reduce estimation distortion")
    
    return returns, outlier_count
