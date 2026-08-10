# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE
import html
from enum import Enum
from typing import Optional
from ..model import LifeModelAgent, Event
from ..people.person import Person
from ..config.config_manager import config


class TrustType(Enum):
    """Types of trusts"""
    REVOCABLE = "Revocable"
    IRREVOCABLE = "Irrevocable"


class Trust(LifeModelAgent):
    def __init__(self, grantor: Person, name: str, trust_type: TrustType,
                 balance: float = 0, growth_rate: Optional[float] = None,
                 beneficiary: Optional[Person] = None,
                 annual_distribution: float = 0,
                 distribution_percent: Optional[float] = None):
        """ Models a trust account for estate planning

        Tax treatment follows the grantor-trust distinction at this model's
        granularity:

        - REVOCABLE: a grantor trust. Investment income is taxed to the
          grantor every year, the grantor can withdraw freely, and
          distributions are tax-free transfers (the income was already taxed).
        - IRREVOCABLE: a separate taxpayer. Undistributed investment income is
          taxed inside the trust at the configured flat trust rate (trust
          brackets compress to the top rate almost immediately), paid from the
          trust balance. Distributions to the beneficiary are then treated as
          tax-free (a simplification of DNI pass-through: the model taxes the
          income once, inside the trust). The grantor cannot withdraw.

        Estate and gift taxes are not modeled.

        Args:
            grantor: The person who created and funded the trust
            name: Name of the trust
            trust_type: REVOCABLE or IRREVOCABLE
            balance: Current trust assets
            growth_rate: Annual growth percentage. Defaults to the configured
                accounts.brokerage.default_growth_rate, so economic scenarios
                can override it.
            beneficiary: Person receiving distributions (defaults to grantor)
            annual_distribution: Fixed annual distribution amount
            distribution_percent: Alternative to a fixed amount; distributes
                this percent of the balance each year
        """
        super().__init__(grantor.model)
        self.grantor = grantor
        self.name = name
        self.trust_type = trust_type
        self.balance = balance
        if growth_rate is None:
            growth_rate = config.financial.get('accounts.brokerage.default_growth_rate', 7.0)
        self.growth_rate = growth_rate
        self.beneficiary = beneficiary or grantor
        self.annual_distribution = annual_distribution
        self.distribution_percent = distribution_percent

        self.stat_trust_balance = balance
        self.stat_trust_distributions = 0.0
        self.stat_trust_taxes_paid = 0.0

        # Register with the model registry (keyed by grantor)
        self.model.registries.trusts.register(grantor, self)

    @property
    def trust_tax_rate(self) -> float:
        """Flat tax rate (percent) applied to irrevocable trust income"""
        return config.financial.get('tax.trust.flat_tax_rate', 37.0)

    def withdraw(self, amount: float) -> float:
        """Withdraw from the trust. Only the grantor of a revocable trust
        retains access to the principal; irrevocable trusts refuse."""
        if self.trust_type != TrustType.REVOCABLE:
            return 0.0
        withdrawn = min(amount, self.balance)
        if withdrawn <= 0:
            return 0.0
        self.balance -= withdrawn
        self.grantor.deposit_into_cashflow_bank_account(withdrawn)
        self.stat_trust_balance = self.balance
        return withdrawn

    def prepare_start_year_stats(self):
        self.stat_trust_balance = self.balance

    def pre_step(self):
        """Apply growth, settle trust tax, and make distributions.

        Runs in pre_step so distributed cash is available to the same year's
        settlement and recognized income lands before taxes are computed.
        """
        growth = self.balance * (self.growth_rate / 100)
        self.balance += growth

        trust_tax = 0.0
        if self.trust_type == TrustType.REVOCABLE:
            # Grantor trust: income is the grantor's, taxed on their return
            if growth > 0:
                self.grantor.taxable_income += growth
        else:
            # Irrevocable: the trust pays its own (flat top-rate) tax
            if growth > 0:
                trust_tax = growth * (self.trust_tax_rate / 100)
                self.balance -= trust_tax

        distribution = self.annual_distribution
        if self.distribution_percent is not None:
            distribution = self.balance * (self.distribution_percent / 100)
        distribution = min(distribution, self.balance)
        if distribution > 0:
            self.balance -= distribution
            self.beneficiary.deposit_into_cashflow_bank_account(distribution)

        self.stat_trust_balance = self.balance
        self.stat_trust_distributions = distribution
        self.stat_trust_taxes_paid = trust_tax

    def dissolve(self) -> float:
        """Terminate the trust and pay the balance out to the beneficiary."""
        payout = self.balance
        if payout > 0:
            self.beneficiary.deposit_into_cashflow_bank_account(payout)
            self.model.event_log.add(Event(
                f"{self.name} dissolved, ${payout:,.0f} paid to {self.beneficiary.name}"))
        self.balance = 0.0
        self.stat_trust_balance = 0.0
        return payout

    def _repr_html_(self):
        desc = '<ul>'
        desc += f'<li>Name: {html.escape(self.name)}</li>'
        desc += f'<li>Type: {self.trust_type.value}</li>'
        desc += f'<li>Balance: ${self.balance:,.2f}</li>'
        desc += f'<li>Beneficiary: {html.escape(self.beneficiary.name)}</li>'
        desc += '</ul>'
        return desc
