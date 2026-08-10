"""
Results Export Module for SAA Optimization

This module handles exporting optimization results to Excel files with
proper formatting and comprehensive reporting.
"""

import pandas as pd
import traceback
from datetime import datetime

from config import (
    LAMBDA_ACTIVE,
    ACTIVE_RISK_BUDGET,
    DATETIME_FORMAT
)
from utils import format_percentage_columns


def export_results_to_excel(results, saa_data, output_path):
    """
    Export optimization results to Excel.
    
    Parameters:
    results (dict): Optimization results
    saa_data (dict): SAA input data
    output_path (str): Path for output file
    """
    try:
        # Create Excel writer
        with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
            # 1. Summary Sheet - Key Metrics
            _create_summary_sheet(results, writer)
            
            # 2. Detailed Allocations Sheet
            _create_allocations_sheet(results, saa_data, writer)
            
            # 3. Cluster Summary Sheet
            _create_cluster_summary_sheet(results, saa_data, writer)
            
            # 4. Diagnostics Sheets
            _create_diagnostics_sheets(results, saa_data, writer)
        
        print(f"\nSuccessfully exported results to {output_path}")
        
    except Exception as e:
        print(f"\nError exporting results: {str(e)}")
        traceback.print_exc()
        raise


def _create_summary_sheet(results, writer):
    """Create the summary sheet with key metrics"""
    summary_data = []
    for profile in results:
        row = {
            'Risk Profile': profile,
            'Target Risk': results[profile]['target_vol'],
            'Status': 'Success' if 'dynamic' in results[profile] and 'weights' in results[profile]['dynamic'] else 'Failed'
        }
        
        # Add Equilibrium metrics if available
        if 'equilibrium' in results[profile] and 'stats' in results[profile]['equilibrium']:
            row.update({
                'Equilibrium Risk': results[profile]['equilibrium']['stats']['volatility'],
                'Equilibrium TE': results[profile]['equilibrium']['stats']['tracking_error']
            })
        
        # Add Dynamic metrics if available
        if 'dynamic' in results[profile] and 'metrics' in results[profile]['dynamic']:
            row.update({
                'Dynamic Risk': results[profile]['dynamic']['metrics']['risk'],
                'Dynamic TE': results[profile]['dynamic']['metrics']['tracking_error'],
                'Expected Return': results[profile]['dynamic']['metrics']['expected_return'],
                'Risk Aversion (λ)': LAMBDA_ACTIVE,
                'Active Risk Budget': ACTIVE_RISK_BUDGET
            })
        
        summary_data.append(row)
    
    # Convert to DataFrame and write to Excel
    summary_df = pd.DataFrame(summary_data)
    format_percentage_columns(summary_df)
    summary_df.to_excel(writer, sheet_name='Summary', index=False)


def _create_allocations_sheet(results, saa_data, writer):
    """Create the detailed allocations sheet"""
    allocation_data = []
    for profile in results:
        if 'equilibrium' in results[profile] and 'weights' in results[profile]['equilibrium']:
            for i, asset in enumerate(saa_data['asset_classes']):
                row = {
                    'Risk Profile': profile,
                    'Asset': asset,
                    'Cluster': saa_data['asset_clusters'].get(asset, 'Unknown'),
                    'Market Weight': saa_data['market_weights'][i],
                    'Equilibrium Weight': results[profile]['equilibrium']['weights'][i],
                }
                
                # Add Dynamic weights if available
                if 'dynamic' in results[profile] and 'weights' in results[profile]['dynamic']:
                    dyn_weight = results[profile]['dynamic']['weights'][i]
                    row.update({
                        'Dynamic Weight': dyn_weight,
                        'Active vs Market': dyn_weight - saa_data['market_weights'][i],
                        'Active vs Equilibrium': dyn_weight - results[profile]['equilibrium']['weights'][i]
                    })
                
                allocation_data.append(row)
    
    # Convert to DataFrame and write to Excel
    allocations_df = pd.DataFrame(allocation_data)
    format_percentage_columns(allocations_df)
    allocations_df.to_excel(writer, sheet_name='Asset_Allocations', index=False)


def _create_cluster_summary_sheet(results, saa_data, writer):
    """Create the cluster summary sheet"""
    cluster_data = []
    for profile in results:
        if 'equilibrium' in results[profile] and 'stats' in results[profile]['equilibrium']:
            for cluster in set(saa_data['asset_clusters'].values()):
                row = {
                    'Risk Profile': profile,
                    'Cluster': cluster,
                    'Equilibrium Weight': results[profile]['equilibrium']['stats']['cluster_weights'].get(cluster, 0)
                }
                
                # Add Dynamic metrics if available
                if 'dynamic' in results[profile] and 'metrics' in results[profile]['dynamic']:
                    row.update({
                        'Dynamic Weight': results[profile]['dynamic']['metrics']['cluster_weights'].get(cluster, 0),
                        'Active Weight': results[profile]['dynamic']['metrics']['cluster_active_weights'].get(cluster, 0),
                        'Tracking Error': results[profile]['dynamic']['metrics']['cluster_tracking_errors'].get(cluster, 0)
                    })
                
                cluster_data.append(row)
    
    # Convert to DataFrame and write to Excel
    clusters_df = pd.DataFrame(cluster_data)
    format_percentage_columns(clusters_df)
    clusters_df.to_excel(writer, sheet_name='Cluster_Summary', index=False)


def _create_diagnostics_sheets(results, saa_data, writer):
    """Create diagnostics sheets for Equilibrium and Dynamic models.
    Falls back to reconstruct diagnostics if they are not present in results.
    """
    try:
        # Lazy imports to avoid circulars
        from equilibrium_saa import EquilibriumSAA
        from dynamic_saa import DynamicSAA

        # Equilibrium diagnostics
        eq_rows = []
        for profile, res in results.items():
            diag = res.get('equilibrium', {}).get('diagnostics', None)
            if diag:
                # Flatten selected fields for readability
                eq_rows.append({
                    'Risk Profile': profile,
                    'n_assets': diag.get('n_assets'),
                    'n_clusters': diag.get('n_clusters'),
                    'omega_shape': str(diag.get('omega_shape')),
                    'pi_shape': str(diag.get('pi_shape')),
                    'omega_condition_number': diag.get('omega_condition_number'),
                    'pi_condition_number': diag.get('pi_condition_number'),
                })
            else:
                # Reconstruct model to extract diagnostics
                eq = EquilibriumSAA(
                    market_weights=saa_data['market_weights'],
                    asset_clusters=saa_data['asset_clusters'],
                    risk_target=res.get('target_vol'),
                    cov_matrix=saa_data['equilibrium_covariance_matrix'],
                    asset_names=saa_data['asset_classes'],
                )
                d = eq.get_transformation_diagnostics()
                eq_rows.append({
                    'Risk Profile': profile,
                    'n_assets': d.get('n_assets'),
                    'n_clusters': d.get('n_clusters'),
                    'omega_shape': str(d.get('omega_shape')),
                    'pi_shape': str(d.get('pi_shape')),
                    'omega_condition_number': d.get('omega_condition_number'),
                    'pi_condition_number': d.get('pi_condition_number'),
                })
        if eq_rows:
            pd.DataFrame(eq_rows).to_excel(writer, sheet_name='Diagnostics_Equilibrium', index=False)

        # Dynamic diagnostics (model-level)
        dyn_rows = []
        feas_rows = []
        for profile, res in results.items():
            d = res.get('dynamic', {})
            diag = d.get('diagnostics', None)
            feas = d.get('constraint7_feasibility', None)
            if diag:
                params = diag.get('parameters', {})
                mats = diag.get('matrices', {})
                dyn_rows.append({
                    'Risk Profile': profile,
                    'lambda_active': params.get('lambda_active'),
                    'variance_target_sigma_squared': params.get('variance_target_sigma_squared'),
                    'active_risk_budget_volatility': params.get('active_risk_budget_volatility'),
                    'active_risk_budget_variance': params.get('active_risk_budget_variance'),
                    'base_cov_shape': str(mats.get('base_cov_matrix_shape')),
                    'active_cov_shape': str(mats.get('active_cov_matrix_shape')),
                    'base_cov_cond_num': mats.get('base_cov_condition_number'),
                    'active_cov_cond_num': mats.get('active_cov_condition_number'),
                    'equilibrium_weights_sum': mats.get('equilibrium_weights_sum'),
                    'market_weights_sum': mats.get('market_weights_sum'),
                })
            elif 'equilibrium' in res and 'weights' in res['equilibrium']:
                # Reconstruct dynamic model for diagnostics
                eq_w = res['equilibrium']['weights']
                dyn = DynamicSAA(
                    equilibrium_weights=eq_w,
                    expected_returns=saa_data['expected_returns'],
                    active_cov_matrix=saa_data['active_covariance_matrix'],
                    asset_clusters=saa_data['asset_clusters'],
                    asset_names=saa_data['asset_classes'],
                    risk_target=res.get('target_vol'),
                    base_cov_matrix=saa_data['equilibrium_covariance_matrix'],
                    market_weights=saa_data['market_weights'],
                )
                dd = dyn.get_implementation_diagnostics()
                params = dd.get('parameters', {})
                mats = dd.get('matrices', {})
                dyn_rows.append({
                    'Risk Profile': profile,
                    'lambda_active': params.get('lambda_active'),
                    'variance_target_sigma_squared': params.get('variance_target_sigma_squared'),
                    'active_risk_budget_volatility': params.get('active_risk_budget_volatility'),
                    'active_risk_budget_variance': params.get('active_risk_budget_variance'),
                    'base_cov_shape': str(mats.get('base_cov_matrix_shape')),
                    'active_cov_shape': str(mats.get('active_cov_matrix_shape')),
                    'base_cov_cond_num': mats.get('base_cov_condition_number'),
                    'active_cov_cond_num': mats.get('active_cov_condition_number'),
                    'equilibrium_weights_sum': mats.get('equilibrium_weights_sum'),
                    'market_weights_sum': mats.get('market_weights_sum'),
                })
                feas = dyn.check_constraint_7_feasibility()
            if feas:
                feas_rows.append({
                    'Risk Profile': profile,
                    'sigma2_target': feas.get('variance_target_sigma_squared'),
                    'paper_value': feas.get('paper_formulation', {}).get('constraint_value'),
                    'paper_feasible': feas.get('paper_formulation', {}).get('feasible'),
                    'paper_violation': feas.get('paper_formulation', {}).get('violation_amount'),
                    'practical_value': feas.get('practical_formulation', {}).get('constraint_value'),
                    'practical_feasible': feas.get('practical_formulation', {}).get('feasible'),
                    'practical_slack': feas.get('practical_formulation', {}).get('slack'),
                    'active_vs_base_ratio': feas.get('matrix_comparison', {}).get('active_vs_base_ratio'),
                    'active_exceeds_base': feas.get('matrix_comparison', {}).get('active_exceeds_base'),
                    'recommendation': feas.get('recommendation'),
                })
        if dyn_rows:
            pd.DataFrame(dyn_rows).to_excel(writer, sheet_name='Diagnostics_Dynamic', index=False)
        if feas_rows:
            pd.DataFrame(feas_rows).to_excel(writer, sheet_name='Constraint7_Feasibility', index=False)
    except Exception as e:
        print(f"\nError creating diagnostics sheets: {str(e)}")
        traceback.print_exc()


def print_summary(results, saa_data):
    """Print a summary of optimization results"""
    successful_profiles = {k: v for k, v in results.items() 
                         if 'dynamic' in v and 'weights' in v['dynamic']}
    
    if not successful_profiles:
        print("No successful optimizations to summarize")
        return
    
    print("\n" + "="*50)
    print("OPTIMIZATION SUMMARY")
    print("="*50)
    
    print(f"\nSuccessful optimizations: {len(successful_profiles)}/{len(results)}")
    
    # Print metrics table
    metrics = []
    for profile in successful_profiles:
        metrics.append({
            'Profile': profile,
            'Target': results[profile]['target_vol'],
            'Actual': results[profile]['dynamic']['metrics']['risk'],
            'Return': results[profile]['dynamic']['metrics']['expected_return'],
            'TE': results[profile]['dynamic']['metrics']['tracking_error']
        })
    
    metrics_df = pd.DataFrame(metrics).set_index('Profile')
    print("\nKey Metrics:")
    print(metrics_df.round(4).to_string())
    
    # Print top active positions for middle profile
    if len(successful_profiles) > 0:
        middle_profile = list(successful_profiles.keys())[len(successful_profiles)//2]
        
        print(f"\nTop Active Positions for {middle_profile}:")
        active_weights = results[middle_profile]['dynamic']['weights'] - results[middle_profile]['equilibrium']['weights']
        active_df = pd.Series(active_weights, index=saa_data['asset_classes'])
        top_active = active_df.abs().nlargest(5)
        
        for asset in top_active.index:
            print(f"  {asset}: {active_df[asset]:.2%}")
