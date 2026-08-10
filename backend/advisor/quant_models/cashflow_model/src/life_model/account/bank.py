# Copyright 2022 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

import html
from ..model import compound_interest
from ..people.person import Person
from ..base_classes import FinancialAccount
from ..config.config_manager import config


class BankAccount(FinancialAccount):
    def __init__(self, owner: Person, company: str, type: str = 'Bank',
                 balance: float = 0, interest_rate: float = None):
        """Class modeling bank accounts

        Args:
            owner (Person): Person that owns the Bank Account.
            company (str): Company at which the bank account belongs.
            type (str, optional): Type of account. Defaults to 'Bank'.
            balance (float, optional): Balance of account. Defaults to 0.
            interest_rate (float, optional): Interest rate. Uses configured default if None.
        """
        super().__init__(owner, balance)
        self.company = company
        self.type = type
        self.interest_rate = (interest_rate if interest_rate is not None
                              else config.financial.get('accounts.bank.default_interest_rate', 0.0))
        self.compound_rate = config.financial.get('accounts.bank.compound_rate', 12)

        self.stat_total_interest = 0
        self.stat_useable_balance = 0
        self.stat_bank_interest = 0
        # Interest is credited after the year's bills settle, so it is
        # recognized as taxable income at the following year's settlement
        self._pending_interest_income = 0.0

        # Register with the model registry
        self.model.registries.bank_accounts.register(owner, self)

    def get_balance(self) -> float:
        """Get current account balance"""
        return self.balance

    def deposit(self, amount: float) -> bool:
        """Deposit amount into account. Returns success status"""
        if amount <= 0:
            return False
        self.balance += amount
        return True

    def withdraw(self, amount: float) -> float:
        """Withdraw amount from account. Returns actual amount withdrawn"""
        if amount <= 0:
            return 0.0
        amount_withdrawn = min(self.balance, amount)
        self.balance -= amount_withdrawn
        return amount_withdrawn

    def _repr_html_(self):
        return f"{self.type} account at {html.escape(self.company)} balance: ${self.balance:,}"

    def prepare_start_year_stats(self):
        self.stat_useable_balance = self.balance

    def pre_step(self):
        # Recognize last year's credited interest as ordinary income at this
        # year's settlement (the same deferred-recognition pattern used for
        # investment cash payouts and Roth earnings).
        if self._pending_interest_income > 0:
            self.person.taxable_income += self._pending_interest_income
            self._pending_interest_income = 0.0

    def step(self):
        # Apply interest
        interest = compound_interest(self.balance, self.interest_rate, self.compound_rate)
        self.balance += interest
        self._pending_interest_income += interest
        self.stat_bank_interest = interest
        self.stat_total_interest += interest
        self.stat_useable_balance = self.balance

        # Call parent step method to track balance history
        super().step()
