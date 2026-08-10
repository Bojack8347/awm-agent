# Copyright 2022 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

from enum import Enum
from typing import List, Optional, TYPE_CHECKING
from ..model import LifeModelAgent, LifeModel, Event, ModelSetupException
from .family import Family
from ..limits import federal_retirement_age, early_withdrawal_penalty_rate
from ..tax.federal import FilingStatus
from ..tax.regime import TaxInput
from ..tax.tax import TaxesDue
from ..account.job401k import Job401kAccount
from ..config.config_manager import config
from ..services.tax_calculation_service import TaxCalculationService
from ..services.payment_service import PaymentService

if TYPE_CHECKING:
    from ..insurance.social_security import SocialSecurity


class GenderAtBirth(Enum):
    MALE = "male"
    FEMALE = "female"
    OTHER = "other"


class Person(LifeModelAgent):
    def __init__(
        self,
        family: Family,
        name: str,
        age: int,
        retirement_age: float,
        spending: 'Spending',
        state: Optional[str] = None,
    ):
        """Person

        Args:
            family (Family): Family of which the person is a part.
            name (str): Person's name.
            age (int): Person's age.
            retirement_age (float): Person's retirement age.
            spending (Spending): Person's spending habits.
            state (str, optional): State tax jurisdiction for this person. If
                None, the configured model default is used.
        """
        super().__init__(family.model)
        self.family = family
        self.name = name
        self.age = age
        self.retirement_age = retirement_age
        self.spending = spending
        self.state = state
        self.debt = 0
        self.earned_income: float = 0
        self.taxable_income: float = 0
        self.social_security_income: float = 0
        self.early_withdrawal_penalty: float = 0
        self.spouse = None
        self.filing_status = FilingStatus.SINGLE
        self.social_security: Optional[SocialSecurity] = None
        self.investment_returns = []
        self.is_deceased = False

        self.stat_money_spent = 0.0
        self.stat_base_living_spending = 0.0
        self.stat_one_time_expenses = 0.0
        self.stat_education_costs = 0.0
        self.stat_home_purchase_costs = 0.0
        self.stat_asset_sale_shortfalls = 0.0
        self.stat_taxes_paid = 0.0
        self.stat_one_time_income = 0.0
        self.stat_bank_balance = 0.0
        self.stat_housing_costs = 0.0
        self.stat_interest_paid = 0.0
        self.stat_ss_income = 0.0

        # Initialize services for business logic
        self.tax_service = TaxCalculationService(self)
        self.payment_service = PaymentService(self)

        self.family.members.append(self)

    @property
    def jobs(self):
        """Get all jobs for this person from the registry"""
        return self.model.registries.jobs.get_items(self)

    @property
    def bank_accounts(self):
        """Get all bank accounts for this person from the registry"""
        return self.model.registries.bank_accounts.get_items(self)

    @property
    def homes(self):
        """Get all homes for this person from the registry"""
        return self.model.registries.homes.get_items(self)

    @property
    def apartments(self):
        """Get all apartments for this person from the registry"""
        return self.model.registries.apartments.get_items(self)

    @property
    def life_insurance_policies(self):
        """Get all life insurance policies for this person from the registry"""
        return self.model.registries.life_insurance_policies.get_items(self)

    @property
    def plan_529s(self):
        """Get all 529 plans for this person from the registry"""
        return self.model.registries.plan_529s.get_items(self)

    @property
    def children(self):
        """Get all child dependents for this person from the registry"""
        return self.model.registries.children.get_items(self)

    @property
    def donations(self):
        """Get all donations for this person from the registry"""
        return self.model.registries.donations.get_items(self)

    @property
    def donor_advised_funds(self):
        """Get all donor advised funds for this person from the registry"""
        return self.model.registries.donor_advised_funds.get_items(self)

    @property
    def charitable_deductions(self) -> float:
        """Calculate total charitable deductions for the year

        Includes:
        - Direct donations actually paid this year
        - DAF contributions (deductible at time of contribution, not distribution)
        """
        # Sum deductions from direct donations
        donation_deductions = sum(d.get_tax_deduction_amount() for d in self.donations)

        # Sum deductions from DAF contributions (tracked in stat_contributions_this_year)
        # DAF contributions are deductible when contributed, not when distributed
        daf_contribution_deductions = sum(
            daf.stat_contributions_this_year for daf in self.donor_advised_funds
        )

        return donation_deductions + daf_contribution_deductions

    @property
    def planned_charitable_deductions(self) -> float:
        """Calculate same-year charitable deductions used for tax planning.

        Direct scheduled donations are deductible in the year they are planned,
        even though the donation cashflow is paid after essential bills. DAF
        contributions still use actual current-year contributions.
        """
        donation_deductions = sum(
            d.get_planned_tax_deduction_amount() for d in self.donations
        )
        daf_contribution_deductions = sum(
            daf.stat_contributions_this_year for daf in self.donor_advised_funds
        )
        return donation_deductions + daf_contribution_deductions

    @property
    def total_itemized_deductions(self) -> float:
        """Calculate total itemized deductions

        Currently includes:
        - Charitable contributions
        - Mortgage interest (if applicable)

        TODO: Add other itemized deductions (state taxes, medical expenses, etc.)
        """
        itemized = self.planned_charitable_deductions

        # Add mortgage interest deductions
        for home in self.homes:
            if hasattr(home, 'mortgage') and home.mortgage:
                itemized += home.mortgage.get_interest_for_year()

        return itemized

    @property
    def federal_deductions(self) -> float:
        """Get federal deductions - use greater of standard or itemized"""
        standard_deduction = self.model.tax_regime.standard_deduction(self.filing_status)
        itemized_deductions = self.total_itemized_deductions
        return max(standard_deduction, itemized_deductions)

    @property
    def all_retirement_accounts(self) -> List[Job401kAccount]:
        return [x.retirement_account for x in self.jobs if x.retirement_account is not None]

    @property
    def brokerage_accounts(self):
        return self.model.registries.brokerage_accounts.get_items(self)

    @property
    def traditional_iras(self):
        return self.model.registries.traditional_iras.get_items(self)

    @property
    def roth_iras(self):
        return self.model.registries.roth_iras.get_items(self)

    @property
    def hsa_accounts(self):
        return self.model.registries.hsa_accounts.get_items(self)

    @property
    def pretax_retirement_balance(self) -> float:
        return (
            sum(account.pretax_balance for account in self.all_retirement_accounts)
            + sum(account.balance for account in self.traditional_iras)
        )

    @property
    def is_retired(self) -> bool:
        """Check if person is retired"""
        return self.age >= self.retirement_age

    def _repr_html_(self):
        desc = self.name
        desc += '<ul>'
        desc += f'<li>Age: {self.age}</li>'
        desc += f'<li>Retirement Age: {self.retirement_age}</li>'
        desc += ''.join(f"<li>{x._repr_html_()}</li>" for x in self.jobs)
        desc += ''.join(f"<li>{x._repr_html_()}</li>" for x in self.bank_accounts)
        desc += ''.join(f"<li>{x._repr_html_()}</li>" for x in self.homes)
        desc += ''.join(f"<li>{x._repr_html_()}</li>" for x in self.apartments)
        desc += f'<li>Debt: {self.debt}</li>'
        desc += '</ul>'
        return desc

    @property
    def bank_account_balance(self) -> float:
        return sum(x.balance for x in self.bank_accounts)

    def deduct_from_bank_accounts(self, amount: float) -> float:
        """Deducts money from bank accounts.

        Args:
            amount (float): Amount to deduct.

        Returns:
            float: Amount that could not be deducted.
        """
        spending_balance = amount
        for bank_account in self.bank_accounts:
            if (spending_balance == 0):
                break
            spending_balance -= bank_account.withdraw(spending_balance)
        return spending_balance

    @property
    def uses_joint_cashflow(self) -> bool:
        """Whether this person should use the family cash pool for direct cashflows."""
        return self.filing_status == FilingStatus.MARRIED_FILING_JOINTLY

    def deduct_from_cashflow_bank_accounts(self, amount: float) -> float:
        """Deduct from the bank pool that funds this person's cashflow.

        Single-person projections use the person's own bank accounts. Married
        filing jointly projections use the household bank pool so direct
        payments such as loans and insurance can be funded by either spouse.
        """
        if self.uses_joint_cashflow:
            return self.family.deduct_from_bank_accounts(amount)
        return self.deduct_from_bank_accounts(amount)

    def deduct_from_pretax_401ks(self, amount: float) -> float:
        """Deducts money from pre-tax retirement accounts.

        Args:
            amount (float): Amount to deduct.

        Returns:
            float: Amount that could not be deducted.
        """
        spending_balance = amount
        for retirement_account in self.all_retirement_accounts:
            if (spending_balance == 0):
                break
            spending_balance -= retirement_account.deduct_pretax(spending_balance)
        for ira in self.traditional_iras:
            if spending_balance == 0:
                break
            spending_balance -= ira.withdraw(spending_balance)
        return spending_balance

    def deduct_from_roth_401ks(self, amount: float) -> float:
        """Deducts money from Roth retirement accounts.

        Args:
            amount (float): Amount to deduct.

        Returns:
            float: Amount that could not be deducted.
        """
        spending_balance = amount
        for retirement_account in self.all_retirement_accounts:
            if (spending_balance == 0):
                break
            spending_balance -= retirement_account.deduct_roth(spending_balance)
        for ira in self.roth_iras:
            if spending_balance == 0:
                break
            spending_balance -= ira.withdraw(spending_balance)
        return spending_balance

    def deduct_from_investments(self, amount: float) -> float:
        """Liquidates personal investment balances to cover spending.

        Args:
            amount (float): Amount to deduct.

        Returns:
            float: Amount that could not be deducted.
        """
        spending_balance = amount
        for investment_return in self.investment_returns:
            if spending_balance == 0:
                break
            spending_balance -= investment_return.withdraw(spending_balance)
        for brokerage in self.brokerage_accounts:
            if spending_balance == 0:
                break
            spending_balance -= brokerage.withdraw(spending_balance)
        return spending_balance

    def deduct_from_hsa_medical(self, amount: float) -> float:
        """Use HSA balances for qualified medical spending before cash bills."""
        remaining = amount
        for account in self.hsa_accounts:
            if remaining == 0:
                break
            remaining -= account.withdraw_medical(remaining)
        return remaining

    def withdraw_from_pretax_401ks(self, amount: float) -> float:
        """Withdraws money from pre-tax retirement accounts.

        Args:
            amount (float): Amount to withdraw.

        Returns:
            float: Amount actually withdrawn (capped at available balances).
        """
        amount -= self.deduct_from_pretax_401ks(amount)
        self.taxable_income += amount
        if amount > 0 and self.age < federal_retirement_age():
            self.early_withdrawal_penalty += amount * (early_withdrawal_penalty_rate() / 100)
        self.deposit_into_bank_account(amount)
        return amount

    def deposit_into_bank_account(self, amount: float):
        """Deposits money into bank account.

        Args:
            amount (float): Amount to deposit.
        """
        if len(self.bank_accounts) == 0:
            raise ModelSetupException('No Bank Account. Create a bank account before making deposits.')
        self.bank_accounts[0].balance += amount

    def deposit_into_cashflow_bank_account(self, amount: float):
        """Deposit into the bank pool that receives this person's cashflow."""
        if amount <= 0:
            return
        if self.uses_joint_cashflow:
            self.family.deposit_into_bank_account(amount)
            return
        self.deposit_into_bank_account(amount)

    def add_one_time_income(self, amount: float, taxable: bool = True):
        """Add a one-off cash inflow to this person's current-year cashflow."""
        if amount <= 0:
            return
        self.deposit_into_cashflow_bank_account(amount)
        self.stat_one_time_income += amount
        self.stat_gross_income += amount
        if taxable:
            self.taxable_income += amount

    def pay_bills(self, spending_balance: float) -> float:
        """Pays bills using payment service for optimal prioritization.

        Args:
            spending_balance (float): Amount of money spent.

        Returns:
            float: Amount that could not be paid.
        """
        return self.payment_service.pay_bills_with_prioritization(spending_balance)

    def get_income_taxes_due(self, additional_income: float = 0) -> TaxesDue:
        """Gets income taxes due for the year.

        Args:
            additional_income (float, optional): Additional income to include, not present in taxable_income.

        Raises:
            NotImplementedError: Unsupported filing status.

        Returns:
            float: Federal taxes due.
        """

        income_amount = self.taxable_income + additional_income
        if self.filing_status == FilingStatus.SINGLE:
            taxes = self.model.tax_regime.income_taxes(
                TaxInput(
                    income_amount,
                    self.federal_deductions,
                    self.filing_status,
                    self.state,
                    self.earned_income,
                    ss_benefits=self.social_security_income,
                )
            )
            taxes.penalties = self.early_withdrawal_penalty
            return taxes
        if self.filing_status == FilingStatus.MARRIED_FILING_JOINTLY:
            return self.family.get_income_taxes_due(additional_income)

        raise NotImplementedError(f"Unsupported filing status: {self.filing_status}")

    def die(self):
        """Mark the person deceased at the start of the current year.

        The model treats death as a year-boundary event: the deceased earns
        no wages, spends nothing, and pays no taxes from the death year on.
        Wage income stops, personal spending zeroes, life insurance death
        benefits pay out to the survivor, annuities apply their payout-type
        death rules, and bank balances transfer to the surviving spouse as an
        untaxed spousal transfer. The survivor files single from the death
        year (the model has no qualifying-widow(er) status). Estate taxes and
        probate are not modeled.
        """
        if self.is_deceased:
            return
        self.is_deceased = True

        for job in self.jobs:
            job.retire()
        self.spending.base = 0

        for policy in self.model.registries.life_insurance_policies.get_items(self):
            policy.process_death_benefit()
            policy.is_active = False
            # The insurer keeps the cash value; any policy loan was already
            # netted out of the death benefit
            policy.cash_value = 0.0
            policy.outstanding_loan_balance = 0.0
            policy.stat_cash_value = 0.0
            policy.stat_life_insurance_loan_balance = 0.0

        for annuity in self.model.registries.annuities.get_items(self):
            annuity.handle_owner_death()

        survivor = self.spouse
        if survivor is not None:
            for account in self.bank_accounts:
                balance = account.balance
                if balance > 0:
                    account.balance = 0
                    survivor.deposit_into_cashflow_bank_account(balance)
            # Spousal rollover: 401k balances move into the survivor's first
            # retirement account untaxed, preserving pretax/Roth character
            # and Roth basis. RMDs thereafter follow the survivor's age. If
            # the survivor has no retirement account the balances stay frozen
            # (no inherited-IRA machinery is modeled).
            survivor_accounts = survivor.all_retirement_accounts
            if survivor_accounts:
                target = survivor_accounts[0]
                for account in self.all_retirement_accounts:
                    if account.pretax_balance > 0:
                        target.pretax_balance += account.pretax_balance
                        account.pretax_balance = 0
                    if account.roth_balance > 0:
                        target.roth_balance += account.roth_balance
                        target.roth_basis += account.roth_basis
                        account.roth_balance = 0
                        account.roth_basis = 0
                    account.stat_401k_balance = 0
                    account.stat_useable_balance = 0
            else:
                # The surviving spouse has no retirement account to receive a
                # rollover. The conservative treatment liquidates the inherited
                # 401k rather than leaving it frozen and untaxed: the pretax
                # balance is recognized as a lump-sum taxable distribution on
                # the survivor's return this year (the highest immediate tax,
                # mirroring the trust conversion -- value never escapes income
                # tax), while the Roth balance passes tax-free. Proceeds land
                # in the survivor's bank and their settlement pays the tax.
                # (The survivor has already run pre_step, so income added here
                # persists to settlement.)
                for account in self.all_retirement_accounts:
                    if account.pretax_balance > 0:
                        survivor.taxable_income += account.pretax_balance
                        survivor.deposit_into_cashflow_bank_account(account.pretax_balance)
                        account.pretax_balance = 0
                    if account.roth_balance > 0:
                        survivor.deposit_into_cashflow_bank_account(account.roth_balance)
                        account.roth_balance = 0
                        account.roth_basis = 0
                    account.stat_401k_balance = 0
                    account.stat_useable_balance = 0
            # Revocable trusts hand over to the surviving spouse: the
            # survivor becomes grantor (and beneficiary where the deceased
            # was), so growth is taxed on the survivor's return from the
            # death year on. Irrevocable trusts are separate taxpayers and
            # only need their beneficiary redirected.
            for trust in list(self.model.registries.trusts.get_items(self)):
                self.model.registries.trusts.unregister(self, trust)
                trust.grantor = survivor
                if trust.beneficiary is self:
                    trust.beneficiary = survivor
                self.model.registries.trusts.register(survivor, trust)
            self.spouse = None
            survivor.spouse = None
            survivor.filing_status = FilingStatus.SINGLE
        else:
            # No surviving spouse: a revocable trust can no longer be revoked
            # once its grantor dies, so it converts to an irrevocable trust —
            # a separate taxpayer at the flat trust rate (the conservative
            # treatment; growth never accrues untaxed against the deceased's
            # empty return). Distributions redirect to the first surviving
            # family member, or suspend when nobody is left to receive them.
            from ..account.trust import TrustType
            heir = next(
                (member for member in self.family.members
                 if member is not self and not getattr(member, 'is_deceased', False)),
                None,
            )
            for trust in self.model.registries.trusts.get_items(self):
                trust.trust_type = TrustType.IRREVOCABLE
                if trust.beneficiary is self or getattr(trust.beneficiary, 'is_deceased', False):
                    if heir is not None:
                        trust.beneficiary = heir
                    else:
                        trust.annual_distribution = 0
                        trust.distribution_percent = None
        self.filing_status = FilingStatus.SINGLE

        self.model.event_log.add(Event(f"{self.name} died at age {self.age}"))

    def get_married(self, spouse: 'Person', link_spouse: bool = True):
        """Get  married.

        Args:
            spouse (Person): Spouse to get married to.
            link_spouse (bool, optional): Whether to call get_married on the spouse object as well. Defaults to True.
        """
        self.spouse = spouse
        self.filing_status = FilingStatus.MARRIED_FILING_JOINTLY
        if link_spouse:
            spouse.get_married(self, False)
            event_str = f"{self.name} and {spouse.name} got married at age {self.age} and {spouse.age}"
            self.model.event_log.add(Event(event_str))

    def get_year_at_age(self, age: int) -> int:
        """Gets the year at a given age.

        Args:
            age (int): Age of the person.

        Returns:
            int: Year at the given age.
        """
        return self.model.year + (age - self.age)

    def pre_step(self):
        self.earned_income = 0
        self.taxable_income = 0
        self.social_security_income = 0
        self.early_withdrawal_penalty = 0
        self.stat_gross_income = 0
        self.stat_one_time_income = 0
        self.age += 1

    def prepare_start_year_stats(self):
        self.stat_money_spent = 0.0
        self.stat_base_living_spending = 0.0
        self.stat_one_time_expenses = 0.0
        self.stat_education_costs = 0.0
        self.stat_home_purchase_costs = 0.0
        self.stat_asset_sale_shortfalls = 0.0
        self.stat_bank_balance = self.bank_account_balance
        self.stat_housing_costs = 0.0
        self.stat_interest_paid = 0.0
        self.stat_taxes_paid = 0.0
        self.stat_gross_income = 0.0
        self.stat_one_time_income = 0.0
        self.stat_debt = self.debt

    def step(self):
        discretionary_spending = self.spending.get_yearly_spending()
        child_cash_expenses = sum(child.stat_child_cash_outflow for child in self.children)
        home_spending = sum(x.make_yearly_payment() for x in self.homes)
        apartment_rent = sum(x.yearly_rent for x in self.apartments)
        all_bills_except_taxes = discretionary_spending + child_cash_expenses + home_spending + apartment_rent
        home_interest_paid = sum(x.stat_mortgage_interest_paid for x in self.homes)

        # Retire from all jobs at retirement age
        if self.age == self.retirement_age:
            for job in self.jobs:
                job.retire()

        # Pay off spending, taxes, and debts for the year
        # - People filing single is handled individually here, MFJ is handled in family
        if self.filing_status == FilingStatus.SINGLE:
            cash_investment_return = self._apply_cash_investment_returns()
            self.flush_brokerage_gains()

            # Size the withdrawal to cover carried shortfall debt too, mirroring
            # the married-household path (family.py). Without this, carried debt
            # is only paid from leftover bank after sizing, so it can persist
            # indefinitely even with an ample pretax balance.
            cash_need_without_taxes = all_bills_except_taxes + self.debt
            withdrawal_amount, yearly_taxes = \
                self.tax_service.calculate_total_401k_withdrawal(cash_need_without_taxes)

            # Pay bills, carried debt, and taxes in a single waterfall; whatever
            # remains unpaid rolls forward as the new shortfall debt.
            self.debt = self.pay_bills(cash_need_without_taxes + yearly_taxes.total)
        else:
            cash_investment_return = 0
            withdrawal_amount = 0
            yearly_taxes = TaxesDue()

        if (self.age == int(federal_retirement_age())):
            self.model.event_log.add(Event(f"{self.name} reached retirement age (age {federal_retirement_age()})"))

        self.stat_money_spent = discretionary_spending
        self.stat_base_living_spending = self.spending.base
        self.stat_one_time_expenses = self.spending.get_category_amount(Spending.CATEGORY_ONE_TIME)
        self.stat_education_costs = self.spending.get_category_amount(Spending.CATEGORY_EDUCATION)
        self.stat_home_purchase_costs = self.spending.get_category_amount(Spending.CATEGORY_HOME_PURCHASE)
        self.stat_asset_sale_shortfalls = self.spending.get_category_amount(Spending.CATEGORY_ASSET_SALE_SHORTFALL)
        self.stat_taxes_paid = yearly_taxes.total
        self.stat_bank_balance = self.bank_account_balance
        self.stat_debt = self.debt
        self.stat_retirement_withdrawals = withdrawal_amount
        self.stat_cash_investment_return = cash_investment_return
        self.stat_housing_costs = home_spending + apartment_rent
        self.stat_interest_paid = home_interest_paid

        # Additional tax stats
        self.stat_taxes_paid_federal = yearly_taxes.federal
        self.stat_taxes_paid_state = yearly_taxes.state
        self.stat_taxes_paid_ss = yearly_taxes.ss
        self.stat_taxes_paid_medicare = yearly_taxes.medicare
        self.stat_taxes_paid_penalties = yearly_taxes.penalties
        self.stat_tax_agi = yearly_taxes.agi
        self.stat_taxable_income = yearly_taxes.taxable_income
        self.stat_tax_deductions = yearly_taxes.deductions
        self.stat_federal_marginal_rate = yearly_taxes.federal_marginal_rate
        self.stat_effective_tax_rate = yearly_taxes.effective_tax_rate

    def post_step(self):
        pass

    def _apply_cash_investment_returns(self) -> float:
        cash_investment_return = 0.0
        for investment_return in self.investment_returns:
            cash_investment_return += investment_return.apply_to_person_cashflow()
        return cash_investment_return

    def flush_brokerage_gains(self):
        """Recognize brokerage gains realized during last year's bill paying.

        Brokerage liquidations that plug a cash gap realize gains over cost
        basis; those are deferred to the following year's settlement (matching
        InvestmentReturn), so this is called at the start of settlement before
        taxes are computed.
        """
        for brokerage in self.brokerage_accounts:
            self.taxable_income += brokerage.flush_realized_gains()


class Spending(LifeModelAgent):
    CATEGORY_ONE_TIME = "one_time_expenses"
    CATEGORY_EDUCATION = "education_costs"
    CATEGORY_HOME_PURCHASE = "home_purchase_costs"
    CATEGORY_ASSET_SALE_SHORTFALL = "asset_sale_shortfalls"
    CATEGORY_HEALTHCARE = "healthcare_costs"
    CATEGORY_REAL_ASSET = "real_asset_costs"

    def __init__(self, model: LifeModel, base: float = 0, yearly_increase: Optional[float] = None):
        """Spending

        Args:
            model (LifeModel): LifeModel instance.
            base (float): Base spending amount.
            yearly_increase (float): Yearly percentage increase in spending.
                If None, the configured inflation rate is used. 10 = 10%.
        """
        super().__init__(model)
        self.base = base
        self.yearly_increase = (
            config.financial.get_inflation_rate()
            if yearly_increase is None else yearly_increase
        )
        self.one_time_expenses = 0
        self.expenses_by_category = {}

    def add_expense(self, amount: float, category: str = CATEGORY_ONE_TIME):
        """Adds a one-time expense.

        Args:
            amount (float): Amount of expense to add.
            category (str): Mutually exclusive spending subcategory.
        """
        self.expenses_by_category[category] = self.expenses_by_category.get(category, 0) + amount
        if category == self.CATEGORY_ONE_TIME:
            self.one_time_expenses += amount

    def get_category_amount(self, category: str) -> float:
        return self.expenses_by_category.get(category, 0)

    def get_yearly_spending(self) -> float:
        """Gets yearly spending."""
        return self.base + sum(self.expenses_by_category.values())

    def adjust_base(self, base_percent: float):
        """Adjusts base spending.

        Args:
            percent_increase (float): Percentage increase in spending. 50 = 50%.
        """
        self.base = self.base * (base_percent / 100)

    def step(self):
        self.base += (self.base * (self.yearly_increase / 100))

    def post_step(self):
        self.one_time_expenses = 0
        self.expenses_by_category = {}
