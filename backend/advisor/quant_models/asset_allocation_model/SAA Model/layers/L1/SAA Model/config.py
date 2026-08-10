"""
Configuration and Parameters for SAA Optimization

This module contains all the configurable parameters and constants used
throughout the SAA optimization process.
"""

import os

# ===== FILE PATHS =====
# Get the directory where this config file is located (SAA Model folder)
_config_dir = os.path.dirname(os.path.abspath(__file__))
# Go up one level to the main SAA directory where the Excel file is located
_saa_dir = os.path.dirname(_config_dir)
# Construct the full path to the data file
DATA_FILE_PATH = os.path.join(_saa_dir, "CMA.xlsx")  # Path to data file
OUTPUT_FILE_PATH = os.path.join(_saa_dir, "SAA_Results.xlsx")  # Path for output results

# ===== OPTIMIZATION PARAMETERS =====
LIQUIDITY_TARGET = 0.02  # Target allocation for liquidity (2%)
ACTIVE_RISK_BUDGET = 0.2  # Active risk budget as fraction of risk target squared
LAMBDA_ACTIVE = 2  # Risk aversion parameter for dynamic optimization
GAMMA_ANCHOR = 100.0  # Cluster anchoring strength in Equilibrium objective
MAX_OPTIMIZATION_ITERATIONS = 1000  # Maximum iterations for optimizer
LIQUIDITY_MODE = 'exclude_then_add'  # 'fixed_post' (default) | 'exclude_then_add'

# ===== RISK MANAGEMENT PARAMETERS =====
OUTLIER_THRESHOLD = 100  # Z-score threshold for outlier detection
RISK_TOLERANCE = 0.008  # Risk tolerance for equilibrium optimization (0.1%)
DYNAMIC_RISK_TOLERANCE = 0.0005  # Risk tolerance for dynamic optimization

# ===== NUMERICAL STABILITY PARAMETERS =====
MIN_EIGENVALUE_THRESHOLD = 1e-8  # Minimum eigenvalue for positive definiteness
MATRIX_REGULARIZATION = 1e-8  # Regularization value for covariance matrices
CONVERGENCE_TOLERANCE = 1e-8  # Default convergence tolerance
TIGHT_CONVERGENCE_TOLERANCE = 1e-10  # Tighter convergence tolerance

# ===== DATA VALIDATION PARAMETERS =====
MIN_REQUIRED_OBSERVATIONS_MULTIPLIER = 12  # Monthly observations per year
MIN_ABSOLUTE_OBSERVATIONS = 60  # Minimum absolute number of observations

# ===== OPTIMIZATION STRATEGY PARAMETERS =====
NUM_OPTIMIZATION_ATTEMPTS = 4  # Number of optimization attempts with different starting points
CLUSTER_BUDGET_FACTOR_MIN = 0.5  # Minimum cluster budget factor
CLUSTER_BUDGET_FACTOR_MAX = 1.5  # Maximum cluster budget factor
SMALL_CLUSTER_WEIGHT_THRESHOLD = 0.001  # Threshold for small cluster weights

# ===== OUTPUT FORMATTING =====
PERCENTAGE_DECIMAL_PLACES = 4  # Decimal places for percentage formatting
DATETIME_FORMAT = '%Y-%m-%d'  # Date format for output
TIME_FORMAT = '%H:%M:%S'  # Time format for logging

# ===== TRADING DAYS =====
TRADING_DAYS_PER_YEAR = 252  # Number of trading days per year for annualization
