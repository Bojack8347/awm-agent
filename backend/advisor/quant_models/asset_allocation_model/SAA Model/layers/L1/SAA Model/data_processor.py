"""
Data Processing Module for SAA Optimization

This module handles loading data from Excel files and preparing data for optimization.
"""

import pandas as pd
import numpy as np
import traceback

from utils import (
    ensure_positive_definite,
    handle_nan_inf_values,
    validate_matrix_dimensions,
)


def load_saa_data(file_path):
    """
    Load and structure SAA input data from Excel.
    
    Parameters:
    file_path (str): Path to input data file (CMA.xlsx)
    
    Returns:
    tuple: (saa_data_dict, None) - second element kept for backward compatibility
    """
    try:
        with pd.ExcelFile(file_path) as xl:
            # Core data - Load market weights from 'Market Weight' tab
            market_weight_df = pd.read_excel(xl, 'Market Weight', header=None)
            assets = market_weight_df.iloc[0, 0:].tolist()
            market_weights = market_weight_df.iloc[1, 0:].values.astype(float)
            print(f"Successfully loaded market weights for {len(assets)} assets")
            
            # Returns and risks from Expected R&R tab
            returns = pd.read_excel(xl, 'Expected R&R', header=None)
            exp_returns = returns.iloc[1,1:].values.astype(float)
            fu_vols = returns.iloc[2,1:].values.astype(float)
            
            # Risk profiles and clusters
            risk_profiles = pd.read_excel(xl, 'Naive Benchmark', header=None)
            clusters = pd.read_excel(xl, 'Asset Cluster Mapping', header=None)
            
            # Load Equilibrium Volatility for equilibrium SAA
            try:
                eq_vol_df = pd.read_excel(xl, 'Eq Volatility', header=None)
                eq_vols = eq_vol_df.iloc[1, 1:len(assets)+1].values.astype(float)
                print(f"Successfully loaded equilibrium volatilities for {len(eq_vols)} assets")
            except Exception as e:
                print(f"Error loading equilibrium volatility: {e}")
                raise ValueError("Failed to load equilibrium volatility from 'Eq Volatility' sheet.") from e
            
            # Load Equilibrium Correlation Matrix for equilibrium SAA
            try:
                eq_corr_df = pd.read_excel(xl, 'Eq Corr Matrix', header=None).iloc[1:len(assets)+1, 1:len(assets)+1]
                eq_corr = eq_corr_df.astype(float).values
                print(f"Successfully loaded equilibrium correlation matrix with shape {eq_corr.shape}")
            except Exception as e:
                print(f"Error loading equilibrium correlation matrix: {e}")
                raise ValueError("Failed to load equilibrium correlation matrix from 'Eq Corr Matrix' sheet.") from e
            
            # Active/Expected correlation matrix for Dynamic SAA
            try:
                active_corr_df = pd.read_excel(xl, 'Expected Corr Matrix', header=None).iloc[1:len(assets)+1, 1:]
                # Check for non-numeric values
                non_numeric = active_corr_df.map(lambda x: not pd.api.types.is_numeric_dtype(type(x))).any().any()
                if non_numeric:
                    print("Warning: Non-numeric values detected in correlation matrix, converting to float")
                
                active_corr = active_corr_df.astype(float).values
                print(f"Successfully loaded active correlation matrix with shape {active_corr.shape}, dtype {active_corr.dtype}")
            except Exception as e:
                print(f"Error loading active correlation matrix: {e}")
                raise ValueError("Failed to load active correlation matrix from 'Expected Corr Matrix' sheet.") from e
        
        # Validate dimensions
        _validate_data_dimensions(assets, market_weights, exp_returns, eq_corr)
        
        # Construct equilibrium covariance matrix from equilibrium volatility and equilibrium correlation
        # This is used for Equilibrium SAA optimization
        equilibrium_covariance_matrix = np.diag(eq_vols.astype(float)) @ eq_corr.astype(float) @ np.diag(eq_vols.astype(float))
        
        # Construct active covariance matrix from forward-looking volatilities and correlation
        # This is used for Dynamic SAA optimization
        active_covariance_matrix = np.diag(fu_vols.astype(float)) @ active_corr.astype(float) @ np.diag(fu_vols.astype(float))
        
        # Clean and validate equilibrium covariance matrix
        equilibrium_covariance_matrix, _ = handle_nan_inf_values(equilibrium_covariance_matrix, "equilibrium covariance matrix")
        equilibrium_covariance_matrix, _ = ensure_positive_definite(equilibrium_covariance_matrix, "equilibrium covariance matrix")
        
        # Clean and validate active covariance matrix
        active_covariance_matrix, _ = handle_nan_inf_values(active_covariance_matrix, "active covariance matrix")
        active_covariance_matrix, _ = ensure_positive_definite(active_covariance_matrix, "active covariance matrix")
        
        # Print covariance matrix information
        print(f"Equilibrium covariance matrix shape: {equilibrium_covariance_matrix.shape}, dtype: {equilibrium_covariance_matrix.dtype}")
        print(f"Active covariance matrix shape: {active_covariance_matrix.shape}, dtype: {active_covariance_matrix.dtype}")
            
        # Create structured data dictionary
        saa_data = {
            'asset_classes': assets,
            'market_weights': market_weights,
            'expected_returns': exp_returns,
            'equilibrium_covariance_matrix': equilibrium_covariance_matrix,  # For Equilibrium SAA
            'active_covariance_matrix': active_covariance_matrix,  # For Dynamic SAA
            'risk_profiles': dict(zip(risk_profiles.iloc[0,:], risk_profiles.iloc[1,:].astype(float))),
            'asset_clusters': dict(zip(clusters.iloc[1:,0], clusters.iloc[1:,1]))
        }
        
        return saa_data, None
        
    except Exception as e:
        print(f"Error loading data: {str(e)}")
        traceback.print_exc()
        raise


def _validate_data_dimensions(assets, market_weights, exp_returns, corr):
    """
    Validate that all data arrays have consistent dimensions.
    
    Parameters:
    assets (list): List of asset names
    market_weights (np.array): Market portfolio weights
    exp_returns (np.array): Expected returns
    corr (np.array): Correlation matrix
    
    Raises:
    ValueError: If dimensions don't match
    """
    asset_count = len(assets)
    
    if len(market_weights) != asset_count:
        raise ValueError(f"Mismatch between asset count ({asset_count}) and market weights ({len(market_weights)})")
        
    if len(exp_returns) != asset_count:
        raise ValueError(f"Expected returns count ({len(exp_returns)}) doesn't match asset count ({asset_count})")
            
    if corr.shape[0] != asset_count or corr.shape[1] != asset_count:
        raise ValueError(f"Correlation matrix size ({corr.shape}) doesn't match asset count ({asset_count})")
