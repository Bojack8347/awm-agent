# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

"""
Investment account registry for Monte Carlo simulation.

This module provides a registry to collect and manage investment accounts
that have asset allocations defined, enabling coordinated stochastic
return application during Monte Carlo simulations.
"""

from typing import Dict, List, Tuple, Optional, TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    pass


@runtime_checkable
class StochasticInvestment(Protocol):
    """Protocol for investment accounts that support stochastic returns."""
    
    @property
    def account_id(self) -> str:
        """Unique identifier for the account."""
        ...
    
    @property
    def asset_allocation(self) -> Optional[Dict[str, float]]:
        """Asset allocation dictionary or None if not set."""
        ...
    
    def apply_stochastic_return(self, return_rate: float) -> float:
        """Apply a stochastic return rate to the account."""
        ...


class InvestmentAccountRegistry:
    """Collects all investment accounts with asset allocations for Monte Carlo.
    
    This registry maintains a collection of investment accounts that have
    asset allocations defined. During Monte Carlo simulation, the simulator
    uses this registry to:
    1. Collect all accounts that need stochastic returns
    2. Apply correlated returns to each account
    
    Example:
        >>> registry = InvestmentAccountRegistry()
        >>> registry.register(brokerage_account)
        >>> registry.register(ira_account)
        >>> accounts = registry.get_accounts_with_allocations()
        >>> # Returns [("brokerage_123", {...}), ("ira_456", {...})]
    """
    
    def __init__(self):
        """Initialize an empty registry."""
        self._accounts: Dict[str, StochasticInvestment] = {}
    
    def register(self, account) -> bool:
        """Register an investment account if it has asset allocation.
        
        Args:
            account: Investment account to register. Must have account_id
                    and asset_allocation attributes.
        
        Returns:
            True if account was registered, False if it lacks required attributes
        """
        if not hasattr(account, 'asset_allocation') or not hasattr(account, 'account_id'):
            return False
        
        if account.asset_allocation is None:
            return False
        
        self._accounts[account.account_id] = account
        return True
    
    def unregister(self, account_id: str) -> bool:
        """Remove an account from the registry.
        
        Args:
            account_id: ID of account to remove
        
        Returns:
            True if account was found and removed, False otherwise
        """
        if account_id in self._accounts:
            del self._accounts[account_id]
            return True
        return False
    
    def get_accounts_with_allocations(self) -> List[Tuple[str, Dict[str, float]]]:
        """Get list of (account_id, asset_allocation) for all registered accounts.
        
        Returns:
            List of tuples containing account ID and its asset allocation
        """
        return [(acc.account_id, acc.asset_allocation) 
                for acc in self._accounts.values()
                if acc.asset_allocation is not None]
    
    def get_account(self, account_id: str) -> Optional[StochasticInvestment]:
        """Get a registered account by ID.
        
        Args:
            account_id: ID of account to retrieve
        
        Returns:
            The account if found, None otherwise
        """
        return self._accounts.get(account_id)
    
    def get_all_accounts(self) -> List[StochasticInvestment]:
        """Get all registered accounts.
        
        Returns:
            List of all registered investment accounts
        """
        return list(self._accounts.values())
    
    def apply_returns(self, returns: Dict[str, float]) -> Dict[str, float]:
        """Apply returns to all registered accounts.

        Args:
            returns: Dict mapping account_id to return rate (decimal)

        Returns:
            Dict mapping account_id to the growth amount applied
        """
        growth_applied = {}
        for account_id, return_rate in returns.items():
            account = self._accounts.get(account_id)
            if account is not None and hasattr(account, 'apply_stochastic_return'):
                # Normal draws can produce rates below -100% for high
                # volatility assets; a long-only account cannot lose more
                # than its value, so the applied rate is floored at -100%.
                growth = account.apply_stochastic_return(max(return_rate, -1.0))
                growth_applied[account_id] = growth
        return growth_applied
    
    def clear(self):
        """Remove all accounts from the registry."""
        self._accounts.clear()
    
    def __len__(self) -> int:
        """Return number of registered accounts."""
        return len(self._accounts)
    
    def __contains__(self, account_id: str) -> bool:
        """Check if an account is registered."""
        return account_id in self._accounts
