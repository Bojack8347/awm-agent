"""
Main Orchestration Module for SAA Optimization

This module provides the main execution logic that coordinates all components
of the Strategic Asset Allocation optimization process.
"""

import argparse
import sys
from datetime import datetime

import importlib
import sys

def get_config(param_name):
    """Get fresh config value (deprecated: use utils.get_config_value)."""
    if 'config' in sys.modules:
        importlib.reload(sys.modules['config'])
    import config
    return getattr(config, param_name)
from data_processor import load_saa_data
from equilibrium_saa import EquilibriumSAA
from dynamic_saa import DynamicSAA
from results_exporter import export_results_to_excel, print_summary
from utils import log_time_elapsed, validate_matrix_dimensions, get_config_value
from layers.L1.layer1_saa import apply_hard_exclusions


def run_saa_optimization(data_file_path=None, output_file_path=None,
                         risk_profile=None, target_volatility=None,
                         excluded_asset_classes=None):
    """
    Run the full SAA optimization process.

    Parameters:
    data_file_path (str): Path to SAA data file (CMA.xlsx)
    output_file_path (str): Path for output results
    risk_profile (str): If provided, optimize only this profile (e.g. "RP1")
    target_volatility (float): If provided, use this volatility instead of Excel lookup
    excluded_asset_classes (list[str]): Asset classes removed from every Layer 1
        input before optimization.

    Returns:
    tuple: (results_dict, saa_data_dict)
    """
    # Get fresh config values if not provided
    if data_file_path is None:
        data_file_path = get_config_value('DATA_FILE_PATH')
    if output_file_path is None:
        output_file_path = get_config_value('OUTPUT_FILE_PATH')

    start_time = datetime.now()
    print(f"Starting SAA optimization at {start_time.strftime(get_config_value('TIME_FORMAT'))}")
    print(f"Loading data from: {data_file_path}")

    # Load data
    saa_data, _ = load_saa_data(data_file_path)
    saa_data = apply_hard_exclusions(saa_data, excluded_asset_classes or [])
    print(f"Data loaded successfully with {len(saa_data['asset_classes'])} asset classes")

    # Verify loaded data has expected dimensions
    _validate_input_data(saa_data)

    # Display key parameters
    _display_parameters()

    # Scope to single profile if requested from API
    if risk_profile is not None:
        if target_volatility is not None:
            saa_data['risk_profiles'] = {risk_profile: target_volatility}
        elif risk_profile in saa_data['risk_profiles']:
            saa_data['risk_profiles'] = {risk_profile: saa_data['risk_profiles'][risk_profile]}
        else:
            available = ", ".join(saa_data['risk_profiles'].keys())
            raise ValueError(f"Risk profile '{risk_profile}' not found. Available: {available}")

    # Process risk profile(s)
    results = _optimize_all_profiles(saa_data)
    
    # Export results and print summary
    if results:
        export_results_to_excel(results, saa_data, output_file_path)
        print(f"\nResults exported to: {output_file_path}")
        
        print_summary(results, saa_data)
        
        log_time_elapsed(start_time, "Total optimization time")
    
    return results, saa_data
def _validate_input_data(saa_data):
    """Validate that input data has consistent dimensions."""
    asset_count = len(saa_data['asset_classes'])
    
    matrices_to_validate = {
        'equilibrium_covariance_matrix': saa_data['equilibrium_covariance_matrix'],
        'active_covariance_matrix': saa_data['active_covariance_matrix']
    }
    
    vectors_to_validate = {
        'market_weights': saa_data['market_weights'],
        'expected_returns': saa_data['expected_returns']
    }
    
    # Validate matrix dimensions
    validate_matrix_dimensions(matrices_to_validate, asset_count)
    
    # Validate vector dimensions
    for name, vector in vectors_to_validate.items():
        if len(vector) != asset_count:
            raise ValueError(f"{name} has length {len(vector)}, expected {asset_count}")


def _display_parameters():
    """Display key optimization parameters."""
    print("\nKey Parameters:")
    print(f"  Liquidity Target: {get_config_value('LIQUIDITY_TARGET'):.1%}")
    print(f"  Active Risk Budget: {get_config_value('ACTIVE_RISK_BUDGET'):.1%}")
    print(f"  Lambda (Risk Aversion): {get_config_value('LAMBDA_ACTIVE')}")


def _optimize_all_profiles(saa_data):
    """Optimize all risk profiles."""
    results = {}
    
    for profile, vol_target in saa_data['risk_profiles'].items():
        profile_start = datetime.now()
        print(f"\nProcessing {profile} (Target Volatility: {vol_target:.1%})")
        
        try:
            # Initialize result structure
            results[profile] = {
                'target_vol': vol_target,
                'equilibrium': {},
                'dynamic': {}
            }
            
            # Step 1: Equilibrium SAA
            eq_weights, eq_stats, eq_diag = _optimize_equilibrium_saa(saa_data, vol_target)
            results[profile]['equilibrium'] = {
                'weights': eq_weights,
                'stats': eq_stats,
                'diagnostics': eq_diag
            }
            
            # Step 2: Dynamic SAA
            dynamic_weights, dynamic_metrics, dyn_diag, dyn_feas = _optimize_dynamic_saa(
                saa_data, eq_weights, vol_target
            )
            results[profile]['dynamic'] = {
                'weights': dynamic_weights,
                'metrics': dynamic_metrics,
                'diagnostics': dyn_diag,
                'constraint7_feasibility': dyn_feas
            }
            
            log_time_elapsed(profile_start, f"  ✓ Successfully optimized {profile}")
            
        except Exception as e:
            print(f"  ✗ Failed to optimize {profile}: {str(e)}")
            results[profile]['error'] = str(e)
    
    return results


def _optimize_equilibrium_saa(saa_data, vol_target):
    """Optimize equilibrium SAA for a given volatility target."""
    eq_saa = EquilibriumSAA(
        market_weights=saa_data['market_weights'],
        asset_clusters=saa_data['asset_clusters'],
        risk_target=vol_target,
        cov_matrix=saa_data['equilibrium_covariance_matrix'],
        asset_names=saa_data['asset_classes'],
        liquidity_target=get_config_value('LIQUIDITY_TARGET')
    )
    
    # Run Equilibrium optimization
    eq_weights = eq_saa.optimize()
    eq_stats = eq_saa.get_portfolio_stats(eq_weights)
    eq_diag = eq_saa.get_transformation_diagnostics()
    
    return eq_weights, eq_stats, eq_diag


def _optimize_dynamic_saa(saa_data, eq_weights, vol_target):
    """Optimize dynamic SAA for a given equilibrium and volatility target."""
    dsaa = DynamicSAA(
        equilibrium_weights=eq_weights,
        expected_returns=saa_data['expected_returns'],
        active_cov_matrix=saa_data['active_covariance_matrix'],
        asset_clusters=saa_data['asset_clusters'],
        asset_names=saa_data['asset_classes'],
        risk_target=vol_target,
        base_cov_matrix=saa_data['equilibrium_covariance_matrix'],
        market_weights=saa_data['market_weights'],
        lambda_active=get_config_value('LAMBDA_ACTIVE'),
    )
    
    # Run Dynamic optimization
    dynamic_weights = dsaa.optimize()
    dyn_diag = dsaa.get_implementation_diagnostics()
    dyn_feas = dsaa.check_constraint_7_feasibility()
    
    return dynamic_weights, dsaa.final_metrics, dyn_diag, dyn_feas


def main():
    """Main entry point for command-line execution."""
    parser = argparse.ArgumentParser(description='Strategic Asset Allocation Optimization')
    parser.add_argument('--data', type=str, default=get_config_value('DATA_FILE_PATH'),
                        help=f'Path to SAA data file (default: {get_config_value("DATA_FILE_PATH")})')
    parser.add_argument('--output', type=str, default=get_config_value('OUTPUT_FILE_PATH'),
                        help=f'Path for output results (default: {get_config_value("OUTPUT_FILE_PATH")})')
    
    args = parser.parse_args()
    
    try:
        run_saa_optimization(
            data_file_path=args.data,
            output_file_path=args.output,
        )
    except Exception as e:
        print(f"Optimization failed: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
