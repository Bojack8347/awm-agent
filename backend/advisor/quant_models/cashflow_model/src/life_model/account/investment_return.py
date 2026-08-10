# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

from typing import Optional, TYPE_CHECKING

from ..model import LifeModelAgent
from .asset_allocation import (
    AssetAllocationInput,
    AssetReturnRatesInput,
    calculate_weighted_return_rate,
    coerce_asset_allocation,
    coerce_asset_return_rates,
    validate_asset_allocation,
)

if TYPE_CHECKING:
    from ..people.family import Family
    from ..people.person import Person


class InvestmentReturn(LifeModelAgent):
    """Cashflow investment return component.

    This component models self-directed investment returns as their own
    cashflow term. It can be attached to a ``Family`` for joint cashflow
    settlement or to a single ``Person`` for single-person settlement.
    """

    def __init__(
        self,
        family: Optional['Family'] = None,
        balance: float = 0,
        growth_rate: float = 0,
        asset_allocation: AssetAllocationInput = None,
        asset_return_rates: AssetReturnRatesInput = None,
        payout_to_bank: bool = True,
        cash_payout_rate: Optional[float] = None,
        taxable: bool = True,
        owner: Optional['Person'] = None,
    ):
        """InvestmentReturn.

        Args:
            family: Family receiving the investment return in joint cashflow
                settlement.
            balance: Investment principal used to calculate returns.
            growth_rate: Fixed annual return rate in percentage form. Used when
                no allocation and return-rate pair is supplied.
            asset_allocation: Optional allocation input. Accepts a dict mapping
                asset class names to weights, an AssetAllocation object, or
                Agent-style entries such as [{"asset": "US Equity", "weight": 0.6}].
            asset_return_rates: Optional return-rate input. Accepts a dict
                mapping asset class names to percentage return rates, an
                AssetReturnRates object, or Agent-style entries. If supplied
                with ``asset_allocation``, the weighted return overrides
                ``growth_rate``.
            payout_to_bank: If true, positive returns are paid to the family
                bank pool. If false, returns are reinvested into ``balance``.
            cash_payout_rate: Percentage of positive returns paid into family
                cashflow. Defaults to 100 when ``payout_to_bank`` is true and
                0 when false. Negative returns are not paid out.
            taxable: If true, positive paid returns are added to ordinary
                taxable income. Investment loss tax effects are not modeled.
            owner: Single person receiving the investment return in
                single-person cashflow settlement. Pass either ``family`` or
                ``owner``, not both.
        """
        if family is None and owner is None:
            raise ValueError('InvestmentReturn requires either family or owner')
        if family is not None and owner is not None:
            raise ValueError('InvestmentReturn accepts either family or owner, not both')

        model = family.model if family is not None else owner.model
        super().__init__(model)
        asset_allocation = coerce_asset_allocation(asset_allocation)
        asset_return_rates = coerce_asset_return_rates(asset_return_rates)
        self.family = family
        self.owner = owner
        self.balance = balance
        self.growth_rate = growth_rate
        self.asset_allocation = asset_allocation
        self.asset_return_rates = asset_return_rates
        self.payout_to_bank = payout_to_bank
        default_payout_rate = 100.0 if payout_to_bank else 0.0
        self.cash_payout_rate = default_payout_rate if cash_payout_rate is None else cash_payout_rate
        if self.cash_payout_rate < 0 or self.cash_payout_rate > 100:
            raise ValueError('cash_payout_rate must be between 0 and 100')
        self.taxable = taxable
        self._pending_cash_payout = 0.0
        self._calculated_year = None
        # After-tax principal. Reinvested returns are unrealized appreciation
        # and only become taxable when withdrawn.
        self.cost_basis = balance
        self._pending_realized_gains = 0.0
        # Monte Carlo support: when the simulator applies a correlated
        # stochastic return, it replaces the deterministic rate for that year.
        self._account_id = f"invret_{id(self)}"
        self._stochastic_rate = None
        self._stochastic_growth_applied = False

        if self.family is not None:
            self.family.investment_returns.append(self)
        else:
            self.owner.investment_returns.append(self)

    def prepare_start_year_stats(self):
        self.stat_investment_balance = self.balance

    @property
    def effective_growth_rate(self) -> float:
        """Return annual growth rate in percentage form."""
        if self.asset_allocation is None:
            return self.growth_rate
        if self.asset_return_rates is None:
            raise ValueError('asset_return_rates are required when asset_allocation is supplied')
        return calculate_weighted_return_rate(self.asset_allocation, self.asset_return_rates)

    @property
    def account_id(self) -> str:
        """Unique identifier used by the Monte Carlo account registry."""
        return self._account_id

    def apply_stochastic_return(self, return_rate: float) -> float:
        """Record a stochastic return rate for this year (Monte Carlo mode).

        The rate replaces the deterministic growth rate when the year's
        return is calculated, so the cash-payout split and tax treatment stay
        identical between modes.
        """
        self._stochastic_rate = return_rate
        self._stochastic_growth_applied = True
        return self.balance * return_rate

    def calculate_return(self) -> float:
        """Calculate current year investment return."""
        return self.balance * (self.effective_growth_rate / 100)

    def _calculate_current_year_return(self):
        if self._calculated_year == self.model.year:
            return

        if self._stochastic_growth_applied:
            yearly_return = self.balance * self._stochastic_rate
            self._stochastic_growth_applied = False
        else:
            yearly_return = self.calculate_return()
        cash_payout = 0.0
        if self.payout_to_bank:
            cash_payout = max(0.0, yearly_return) * (self.cash_payout_rate / 100)

        self.balance += yearly_return - cash_payout

        self.stat_investment_return = yearly_return
        self.stat_investment_balance = self.balance
        self.stat_cash_investment_return = 0
        self._pending_cash_payout = cash_payout
        self._calculated_year = self.model.year

    def pre_step(self):
        self._calculate_current_year_return()

    def step(self):
        self._calculate_current_year_return()

    def withdraw(self, amount: float) -> float:
        """Liquidate part of the investment balance to cover spending.

        The withdrawal realizes the proportional share of unrealized gains
        (balance above cost basis). Realized gains are recognized as taxable
        income in the following year's settlement, matching when the tax on a
        late-year sale would actually be paid.

        Args:
            amount (float): Amount requested.

        Returns:
            float: Amount actually withdrawn (capped at the balance).
        """
        if amount <= 0 or self.balance <= 0:
            return 0.0

        withdrawal = min(amount, self.balance)
        gain_fraction = max(0.0, self.balance - self.cost_basis) / self.balance
        realized_gain = withdrawal * gain_fraction
        if self.taxable and realized_gain > 0:
            self._pending_realized_gains += realized_gain

        self.cost_basis = max(0.0, self.cost_basis - withdrawal * (1 - gain_fraction))
        self.balance -= withdrawal
        self.stat_investment_balance = self.balance
        return withdrawal

    def deposit(self, amount: float) -> float:
        """Add an after-tax contribution to invested principal."""
        if amount <= 0:
            return 0.0
        contribution = float(amount)
        self.balance += contribution
        self.cost_basis += contribution
        self.stat_investment_balance = self.balance
        return contribution

    def _add_taxable_income(self, amount: float):
        if self.family is not None:
            if self.family.members:
                self.family.members[0].taxable_income += amount
        elif self.owner is not None:
            self.owner.taxable_income += amount

    def _flush_realized_gains(self):
        # Called at settlement time (before taxes are computed) so gains
        # realized during last year's bill paying are taxed this year.
        if self._pending_realized_gains > 0:
            self._add_taxable_income(self._pending_realized_gains)
        self._pending_realized_gains = 0.0

    def apply_to_family_cashflow(self) -> float:
        """Pay positive investment return into family cashflow."""
        self._flush_realized_gains()
        if not self.payout_to_bank:
            return 0

        cash_return = self._pending_cash_payout
        if cash_return == 0:
            return 0

        self.family.deposit_into_bank_account(cash_return)
        if self.taxable and self.family.members:
            self.family.members[0].taxable_income += cash_return
        return cash_return

    def apply_to_person_cashflow(self) -> float:
        """Pay positive investment return into single-person cashflow."""
        self._flush_realized_gains()
        if not self.payout_to_bank:
            return 0

        cash_return = self._pending_cash_payout
        if cash_return == 0:
            return 0

        self.owner.deposit_into_cashflow_bank_account(cash_return)
        if self.taxable:
            self.owner.taxable_income += cash_return
        return cash_return
