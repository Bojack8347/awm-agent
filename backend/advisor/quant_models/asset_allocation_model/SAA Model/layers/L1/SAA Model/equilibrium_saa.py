"""
Equilibrium SAA Optimization Model

This module implements the Equilibrium Strategic Asset Allocation optimization,
which minimizes tracking error to the market portfolio while achieving a target volatility.
"""

import numpy as np
from scipy.optimize import minimize

from utils import (
    ensure_positive_definite,
    handle_nan_inf_values,
    calculate_portfolio_risk,
    calculate_tracking_error,
    normalize_weights,
    create_cluster_mapping,
    get_config_value
)


class EquilibriumSAA:
    """
    Equilibrium Strategic Asset Allocation optimization model
    
    Objective: Minimize tracking error to the market portfolio while achieving a target volatility.
    
    Constraints:
    - Total Portfolio Risk: Ensure portfolio volatility matches the target risk
    - Full Investment: Sum of portfolio weights equals 1
    - Non-Negativity: All portfolio weights are non-negative
    - Cluster Proportions: Maintain market-relative proportions within asset clusters
    - Liquidity Allocation: Maintain a fixed allocation for liquidity
    """
    
    def __init__(self, market_weights, asset_clusters, risk_target, cov_matrix, asset_names, liquidity_target=None):
        """
        Initialize Equilibrium SAA model
        
        Parameters:
        market_weights (np.array): Market portfolio weights
        asset_clusters (dict): Dictionary mapping assets to their clusters
        risk_target (float): Target volatility from risk profile
        cov_matrix (np.array): Covariance matrix of asset returns
        asset_names (list): List of asset class names
        liquidity_target (float): Target allocation for liquidity
        """
        self.market_weights = market_weights
        self.asset_clusters = asset_clusters
        self.risk_target = risk_target
        self.assets = asset_names
        self.n_assets = len(self.assets)
        self.liquidity_target = liquidity_target if liquidity_target is not None else get_config_value('LIQUIDITY_TARGET')
        self.liquidity_mode = get_config_value('LIQUIDITY_MODE') if hasattr(__import__('config'), 'LIQUIDITY_MODE') else 'fixed_post'
        
        # Clean and validate covariance matrix
        self.cov_matrix, _ = handle_nan_inf_values(cov_matrix, "covariance matrix")
        self.cov_matrix, _ = ensure_positive_definite(self.cov_matrix, "covariance matrix")
        
        # Find liquidity asset index
        self.liquidity_index = next(
            (
                i
                for i, asset in enumerate(self.assets)
                if self.asset_clusters[asset] == 'Liquidity'
            ),
            None,
        )
        if self.liquidity_index is None:
            self.liquidity_target = 0.0
            self.liquidity_mode = 'fixed_post'
        
        # Create cluster mapping matrices
        self._create_cluster_matrices()
    
    def _create_cluster_matrices(self):
        """Create transformation matrices for cluster-level optimization"""
        unique_clusters, self.cluster_map, self.reverse_cluster_map = create_cluster_mapping(
            self.assets, self.asset_clusters
        )
        self.n_clusters = len(unique_clusters)
        # Build cached cluster indices to avoid recomputation and preserve order
        self.cluster_names = [self.cluster_map[i] for i in range(self.n_clusters)]
        self.cluster_indices = {
            cluster: [i for i, asset in enumerate(self.assets) if self.asset_clusters[asset] == cluster]
            for cluster in self.cluster_names
        }
        
        # Create transformation matrix Omega
        self.omega = np.zeros((self.n_assets, self.n_clusters))
        for i, asset in enumerate(self.assets):
            cluster = self.asset_clusters[asset]
            j = unique_clusters.index(cluster)
            cluster_sum = sum(self.market_weights[k]
                for k, a in enumerate(self.assets)
                if self.asset_clusters[a] == cluster)
            
            # Handle zero weights differently
            if cluster_sum > 0:
                self.omega[i, j] = self.market_weights[i] / cluster_sum
            else:
                # For zero-weight clusters, use equal weights within cluster
                cluster_count = sum(1 for a in self.assets if self.asset_clusters[a] == cluster)
                self.omega[i, j] = 1.0 / cluster_count
        
        # Calculate cluster-level covariance matrix Pi
        self.pi = self.omega.T @ self.cov_matrix @ self.omega
        
        # Calculate cluster-level market weights
        self.cluster_market_weights = np.array([
            max(0.0001, sum(self.market_weights[i] for i in self.cluster_indices[cluster]))
            for cluster in unique_clusters
        ])
        # Renormalize cluster weights
        self.cluster_market_weights = self.cluster_market_weights / np.sum(self.cluster_market_weights)
    
    def optimize(self):
        """
        Optimize equilibrium weights using the transformation approach from SAA methodology paper
        
        This implements the cluster-level optimization as described in Section 2.3:
        - Optimizes over cluster weights (ŵe) instead of asset weights
        - Uses cluster covariance matrix (Π) for objective and constraints  
        - Transforms back to asset weights using Omega matrix
        """
        risk_target = self.risk_target
        # If excluding liquidity, adjust target risk for the non-liquidity sub-portfolio
        adjusted_risk_target = risk_target
        if self.liquidity_mode == 'exclude_then_add':
            nonliq_share = 1.0 - self.liquidity_target
            if nonliq_share <= 0:
                raise ValueError("Liquidity target must be less than 100% when using 'exclude_then_add'")
            adjusted_risk_target = risk_target / nonliq_share
        risk_tolerance = get_config_value('RISK_TOLERANCE')
        
        # Calculate target risk band in cluster space (may be applied on reduced cluster set)
        # For now, use a simple scaling approach - this could be enhanced based on naive benchmark definition
        target_var_upper = (adjusted_risk_target + risk_tolerance)**2
        target_var_lower = (adjusted_risk_target - risk_tolerance)**2

        # Set gamma scaling factor (anchoring strength) from config
        gamma = get_config_value('GAMMA_ANCHOR') if hasattr(__import__('config'), 'GAMMA_ANCHOR') else 100.0

        # Determine whether to optimize in full cluster space or exclude liquidity cluster
        if self.liquidity_mode == 'exclude_then_add':
            # Build reduced (non-liquidity) cluster space
            try:
                liq_cluster_idx = self.cluster_names.index('Liquidity')
            except ValueError:
                liq_cluster_idx = None
            nonliq_indices = [i for i in range(self.n_clusters) if i != liq_cluster_idx]
            pi_eff = self.pi[np.ix_(nonliq_indices, nonliq_indices)]
            omega_eff = self.omega[:, nonliq_indices]
            cluster_mkt_eff = self.cluster_market_weights[nonliq_indices]
            # Re-normalize market weights in effective cluster space
            cluster_mkt_eff = cluster_mkt_eff / np.sum(cluster_mkt_eff)
            n_eff = len(nonliq_indices)

            def objective(cluster_weights):
                """Objective in reduced cluster space."""
                cluster_diff = cluster_weights - gamma * cluster_mkt_eff
                return np.dot(cluster_diff.T, np.dot(pi_eff, cluster_diff))

            def risk_upper_constraint(cluster_weights):
                quad_form = np.dot(cluster_weights.T, np.dot(pi_eff, cluster_weights))
                return target_var_upper - quad_form

            def risk_lower_constraint(cluster_weights):
                quad_form = np.dot(cluster_weights.T, np.dot(pi_eff, cluster_weights))
                return quad_form - target_var_lower

            def cluster_investment_constraint(cluster_weights):
                return np.sum(cluster_weights) - 1.0

            constraints = [
                {'type': 'eq', 'fun': cluster_investment_constraint},
                {'type': 'ineq', 'fun': risk_upper_constraint},
                {'type': 'ineq', 'fun': risk_lower_constraint}
            ]

            best_result = None
            best_objective = float('inf')

            # Use simple in-function initializations to respect reduced dimension
            init_candidates = [
                cluster_mkt_eff,
                np.ones(n_eff) / n_eff,
            ]
            # Add a random start for robustness
            rand_init = np.random.random(n_eff)
            rand_init = rand_init / np.sum(rand_init)
            init_candidates.append(rand_init)

            for attempt, initial_cluster_weights in enumerate(init_candidates, start=1):
                result = minimize(
                    objective,
                    initial_cluster_weights,
                    method='SLSQP',
                    bounds=[(0, 1) for _ in range(n_eff)],
                    constraints=constraints,
                    options={'maxiter': get_config_value('MAX_OPTIMIZATION_ITERATIONS'), 'ftol': get_config_value('CONVERGENCE_TOLERANCE')}
                )

                if result.success and (best_result is None or result.fun < best_objective):
                    best_result = result
                    best_objective = result.fun
                    print(f"  Attempt {attempt}: Better solution found with objective {result.fun:.6e}")
                elif result.success:
                    print(f"  Attempt {attempt}: Converged but not better than previous (obj: {result.fun:.6e} vs {best_objective:.6e})")
                else:
                    print(f"  Attempt {attempt} failed: {result.message}")
        else:
            # Full cluster space (original behavior)
            def objective(cluster_weights):
                """
                Objective function: minimize tracking error to market portfolio in cluster space
                Following the paper: min (ŵe - γŵb)′Π(ŵe - γŵb)
                """
                cluster_diff = cluster_weights - gamma * self.cluster_market_weights
                return np.dot(cluster_diff.T, np.dot(self.pi, cluster_diff))

            def risk_upper_constraint(cluster_weights):
                quad_form = np.dot(cluster_weights.T, np.dot(self.pi, cluster_weights))
                return target_var_upper - quad_form

            def risk_lower_constraint(cluster_weights):
                quad_form = np.dot(cluster_weights.T, np.dot(self.pi, cluster_weights))
                return quad_form - target_var_lower

            def cluster_investment_constraint(cluster_weights):
                return np.sum(cluster_weights) - 1.0

            constraints = [
                {'type': 'eq', 'fun': cluster_investment_constraint},
                {'type': 'ineq', 'fun': risk_upper_constraint},
                {'type': 'ineq', 'fun': risk_lower_constraint}
            ]

            best_result = None
            best_objective = float('inf')

            # Try multiple starting points with different tolerances
            for attempt in range(get_config_value('NUM_OPTIMIZATION_ATTEMPTS')):
                initial_cluster_weights, ftol = self._get_initial_cluster_weights_and_tolerance(attempt)

                result = minimize(
                    objective,
                    initial_cluster_weights,
                    method='SLSQP',
                    bounds=[(0, 1) for _ in range(self.n_clusters)],
                    constraints=constraints,
                    options={'maxiter': get_config_value('MAX_OPTIMIZATION_ITERATIONS'), 'ftol': ftol}
                )

                if result.success and (best_result is None or result.fun < best_objective):
                    best_result = result
                    best_objective = result.fun
                    print(f"  Attempt {attempt+1}: Better solution found with objective {result.fun:.6e}")
                elif result.success:
                    print(f"  Attempt {attempt+1}: Converged but not better than previous (obj: {result.fun:.6e} vs {best_objective:.6e})")
                else:
                    print(f"  Attempt {attempt+1} failed: {result.message}")
        
        # Use the best result if any optimization succeeded
        if best_result is not None:
            optimal_cluster_weights = best_result.x

            # Transform back to asset weights using appropriate Omega
            if self.liquidity_mode == 'exclude_then_add':
                # Map from reduced cluster space back to assets
                asset_weights_nonliq = omega_eff @ optimal_cluster_weights
                # Scale non-liquidity weights by (1 - L) and set liquidity to L
                scaling = 1.0 - self.liquidity_target
                asset_weights = asset_weights_nonliq * scaling
                final_weights = np.zeros_like(asset_weights)
                final_weights[:] = asset_weights
                final_weights[self.liquidity_index] = self.liquidity_target
                asset_weights = final_weights
            else:
                # Default: include liquidity in transformation and enforce exact target post-hoc
                asset_weights = self._cluster_to_asset_weights(optimal_cluster_weights)
                asset_weights = self._enforce_liquidity_target(asset_weights)
            
            # Ensure weights sum to 1 (fix any small numerical issues)
            asset_weights = normalize_weights(asset_weights)
            
            # Verify that weights meet risk target in asset space
            actual_risk = calculate_portfolio_risk(asset_weights, self.cov_matrix)
            if self.liquidity_mode == 'exclude_then_add':
                actual_cluster_risk = np.sqrt(np.dot(optimal_cluster_weights.T, np.dot(pi_eff, optimal_cluster_weights)))
            else:
                actual_cluster_risk = np.sqrt(np.dot(optimal_cluster_weights.T, np.dot(self.pi, optimal_cluster_weights)))
            
            print(f"  Target risk: {risk_target:.4f}")
            print(f"  Achieved asset risk: {actual_risk:.4f}")
            print(f"  Achieved cluster risk: {actual_cluster_risk:.4f}")
            
            # Store both cluster and asset weights for analysis
            if self.liquidity_mode == 'exclude_then_add':
                # Expand reduced cluster weights to full length for diagnostics (set liquidity to 0)
                full_cluster_weights = np.zeros(self.n_clusters)
                for pos, idx in enumerate(nonliq_indices):
                    full_cluster_weights[idx] = optimal_cluster_weights[pos]
                self.optimal_cluster_weights = full_cluster_weights
            else:
                self.optimal_cluster_weights = optimal_cluster_weights
            self.equilibrium_weights = asset_weights
            
            return asset_weights
        
        # If optimization fails, raise an exception
        raise ValueError("All equilibrium optimization attempts failed.")
    
    def _get_initial_cluster_weights_and_tolerance(self, attempt):
        """
        Get initial cluster weights and tolerance for optimization attempt
        
        Parameters:
        attempt (int): Attempt number (0-based)
        
        Returns:
        tuple: (initial_cluster_weights, ftol)
        """
        if attempt == 0:
            # Start with market weights in cluster space
            initial_weights = self.cluster_market_weights.copy()
            ftol = get_config_value('CONVERGENCE_TOLERANCE')
        elif attempt == 1:
            # Try equal weights across clusters
            initial_weights = np.ones(self.n_clusters) / self.n_clusters
            ftol = get_config_value('CONVERGENCE_TOLERANCE')
        elif attempt == 2:
            # Random weights
            initial_weights = np.random.random(self.n_clusters)
            initial_weights = initial_weights / np.sum(initial_weights)
            ftol = get_config_value('CONVERGENCE_TOLERANCE')
        else:
            # Try with tighter tolerance and different starting point
            initial_weights = (self.cluster_market_weights + 
                               np.ones(self.n_clusters) / self.n_clusters) / 2
            initial_weights = initial_weights / np.sum(initial_weights)
            ftol = get_config_value('TIGHT_CONVERGENCE_TOLERANCE')
        
        return initial_weights, ftol
    
    def _cluster_to_asset_weights(self, cluster_weights):
        """
        Transform cluster weights back to asset weights using Omega matrix
        Following the paper: w*e = Ωŵ*e
        
        Parameters:
        cluster_weights (np.array): Optimal cluster weights
        
        Returns:
        np.array: Asset weights
        """
        return self.omega @ cluster_weights
    
    def _enforce_liquidity_target(self, asset_weights):
        """
        Ensure liquidity allocation matches target while preserving cluster structure
        
        Parameters:
        asset_weights (np.array): Asset weights from cluster transformation
        
        Returns:
        np.array: Adjusted asset weights with correct liquidity target
        """
        if self.liquidity_index is None:
            return asset_weights

        # If liquidity target is already approximately correct, return as-is
        current_liquidity = asset_weights[self.liquidity_index]
        
        if abs(current_liquidity - self.liquidity_target) < 1e-6:
            return asset_weights
        
        # Adjust liquidity allocation
        adjusted_weights = asset_weights.copy()
        adjusted_weights[self.liquidity_index] = self.liquidity_target
        
        # Scale other weights to sum to (1 - liquidity_target)
        other_indices = [i for i in range(self.n_assets) if i != self.liquidity_index]
        other_sum = np.sum(adjusted_weights[other_indices])
        
        if other_sum > 0:
            target_other_sum = 1.0 - self.liquidity_target
            scaling_factor = target_other_sum / other_sum
            adjusted_weights[other_indices] *= scaling_factor
        
        return adjusted_weights
    
    def get_portfolio_stats(self, weights):
        """
        Calculate and return portfolio statistics including cluster-level analysis
        
        Parameters:
        weights (np.array): Portfolio weights
        
        Returns:
        dict: Portfolio statistics
        """
        stats = {
            'volatility': calculate_portfolio_risk(weights, self.cov_matrix),
            'tracking_error': calculate_tracking_error(weights, self.market_weights, self.cov_matrix),
        }
        
        # Calculate cluster weights
        cluster_weights = {}
        for cluster in set(self.asset_clusters.values()):
            indices = [i for i, (asset, c) in enumerate(zip(self.assets, self.asset_clusters.values())) 
                      if c == cluster]
            cluster_weights[cluster] = np.sum(weights[indices])
        stats['cluster_weights'] = cluster_weights
        
        # Add cluster-level statistics if optimization has been run
        if hasattr(self, 'optimal_cluster_weights'):
            stats['cluster_level_stats'] = {
                'optimal_cluster_weights': dict(zip(self.cluster_map.values(), self.optimal_cluster_weights)),
                'cluster_market_weights': dict(zip(self.cluster_map.values(), self.cluster_market_weights)),
                'cluster_risk': np.sqrt(np.dot(self.optimal_cluster_weights.T, 
                                             np.dot(self.pi, self.optimal_cluster_weights))),
                'cluster_tracking_error': self._calculate_cluster_tracking_error()
            }
        
        return stats
    
    def _calculate_cluster_tracking_error(self):
        """Calculate tracking error in cluster space"""
        if hasattr(self, 'optimal_cluster_weights'):
            cluster_diff = self.optimal_cluster_weights - self.cluster_market_weights
            cluster_te_variance = np.dot(cluster_diff.T, np.dot(self.pi, cluster_diff))
            return np.sqrt(cluster_te_variance)
        return None
    
    def get_transformation_diagnostics(self):
        """
        Get diagnostic information about the transformation matrices
        
        Returns:
        dict: Diagnostic information
        """
        diagnostics = {
            'n_assets': self.n_assets,
            'n_clusters': self.n_clusters,
            'cluster_map': self.cluster_map,
            'omega_shape': self.omega.shape,
            'pi_shape': self.pi.shape,
            'omega_condition_number': np.linalg.cond(self.omega),
            'pi_condition_number': np.linalg.cond(self.pi),
            'pi_eigenvalues': np.linalg.eigvals(self.pi),
            'cluster_market_weights': dict(zip(self.cluster_map.values(), self.cluster_market_weights))
        }
        
        # Check if Omega matrix is properly normalized
        cluster_sums = []
        for j in range(self.n_clusters):
            cluster_sum = np.sum(self.omega[:, j])
            cluster_sums.append(cluster_sum)
        
        diagnostics['omega_column_sums'] = cluster_sums
        diagnostics['omega_properly_normalized'] = all(abs(s - 1.0) < 1e-10 for s in cluster_sums)
        
        return diagnostics
