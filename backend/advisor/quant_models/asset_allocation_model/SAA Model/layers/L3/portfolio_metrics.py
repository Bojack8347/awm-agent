from __future__ import annotations

from typing import Dict, Tuple

import numpy as np
import pandas as pd

from layers.layer_types import Layer1Result, ManagerSelectionResult


def _build_asset_class_returns(layer1_result: Layer1Result) -> Dict[str, float]:
    asset_classes = list(layer1_result.selected_weights.index)
    base_returns = layer1_result.saa_data.get("expected_returns")
    if base_returns is None:
        return {ac: 0.0 for ac in asset_classes}

    saa_assets = layer1_result.saa_data.get("asset_classes", [])
    mapping = {ac: float(r) for ac, r in zip(saa_assets, base_returns)}
    return {ac: mapping.get(ac, 0.0) for ac in asset_classes}


def _build_asset_class_correlations(layer1_result: Layer1Result) -> pd.DataFrame:
    asset_classes = list(layer1_result.selected_weights.index)
    cov = layer1_result.saa_data.get("active_covariance_matrix")
    if cov is None:
        return pd.DataFrame(np.eye(len(asset_classes)), index=asset_classes, columns=asset_classes)

    cov = np.array(cov, dtype=float)
    vols = np.sqrt(np.diag(cov))
    corr = np.zeros_like(cov)
    for i in range(len(vols)):
        for j in range(len(vols)):
            denom = vols[i] * vols[j]
            corr[i, j] = (cov[i, j] / denom) if denom > 0 else (1.0 if i == j else 0.0)

    saa_assets = layer1_result.saa_data.get("asset_classes", [])
    corr_df = pd.DataFrame(corr, index=saa_assets, columns=saa_assets)
    return corr_df.reindex(index=asset_classes, columns=asset_classes, fill_value=0.0)


def _compute_manager_active_alpha(
    manager_result: ManagerSelectionResult,
    asset_class: str,
) -> float:
    manager_allocs = manager_result.allocations.get(asset_class, {})
    if not manager_allocs:
        return 0.0

    mgr_data = manager_result.manager_data.set_index("ISIN")
    total_alpha = 0.0
    for isin, w in manager_allocs.items():
        if isin not in mgr_data.index:
            continue
        row = mgr_data.loc[isin]
        te = float(row.get("Expected Tracking Error", 0.0))
        ir = float(row.get("Expected Information Ratio", 0.0))
        total_alpha += float(w) * (ir * te)
    return total_alpha


def _compute_manager_active_te(
    manager_result: ManagerSelectionResult,
    asset_class: str,
) -> float:
    manager_allocs = manager_result.allocations.get(asset_class, {})
    if not manager_allocs:
        return 0.0

    cov = manager_result.covariance_by_asset.get(asset_class)
    if cov is None:
        return float(manager_result.active_tes.get(asset_class, 0.0))

    isins = list(manager_allocs.keys())
    if any(isin not in cov.index for isin in isins):
        return float(manager_result.active_tes.get(asset_class, 0.0))

    w = np.array([manager_allocs[isin] for isin in isins], dtype=float)
    cov_mat = cov.loc[isins, isins].values.astype(float)
    var = float(w @ cov_mat @ w)
    return float(np.sqrt(max(var, 0.0)))


def _blended_volatility(passive_vol: float, active_te: float, alpha: float) -> float:
    active_vol = passive_vol + active_te
    if active_vol <= 0:
        return float(passive_vol)

    ratio = active_te / active_vol if active_vol > 0 else 0.0
    rho = np.sqrt(max(0.0, 1.0 - ratio**2))
    rho = max(0.0, min(1.0, rho))

    return float(
        np.sqrt(
            alpha**2 * active_vol**2
            + (1 - alpha) ** 2 * passive_vol**2
            + 2 * alpha * (1 - alpha) * active_vol * passive_vol * rho
        )
    )


def compute_portfolio_expected_return_and_volatility(
    layer1_result: Layer1Result,
    active_allocations: Dict[str, float],
    manager_result: ManagerSelectionResult,
    passive_vols: Dict[str, float],
) -> Tuple[float, float]:
    asset_classes = list(layer1_result.selected_weights.index)
    weights = layer1_result.selected_weights.to_dict()
    base_returns = _build_asset_class_returns(layer1_result)
    corr = _build_asset_class_correlations(layer1_result)

    asset_returns: Dict[str, float] = {}
    asset_vols: Dict[str, float] = {}

    for ac in asset_classes:
        alpha = float(active_allocations.get(ac, 0.0))
        passive_vol = float(passive_vols.get(ac, 0.0))
        active_alpha = _compute_manager_active_alpha(manager_result, ac)
        active_te = _compute_manager_active_te(manager_result, ac)

        asset_returns[ac] = float(base_returns.get(ac, 0.0)) + alpha * active_alpha
        asset_vols[ac] = _blended_volatility(passive_vol, active_te, alpha)

    expected_return = sum(weights.get(ac, 0.0) * asset_returns.get(ac, 0.0) for ac in asset_classes)

    total_var = 0.0
    for ac_i in asset_classes:
        for ac_j in asset_classes:
            wi = weights.get(ac_i, 0.0)
            wj = weights.get(ac_j, 0.0)
            vi = asset_vols.get(ac_i, 0.0)
            vj = asset_vols.get(ac_j, 0.0)
            corr_ij = float(corr.loc[ac_i, ac_j]) if ac_i in corr.index and ac_j in corr.columns else 0.0
            total_var += wi * wj * vi * vj * corr_ij

    expected_vol = float(np.sqrt(max(total_var, 0.0)))
    return float(expected_return), expected_vol
