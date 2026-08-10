# Copyright 2023 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

from contextlib import contextmanager
from contextvars import ContextVar
import math
from typing import Iterator, Optional

from .federal import FilingStatus
from ..config.config_manager import config


_authorized_social_security_max_income: ContextVar[Optional[float]] = ContextVar(
    "authorized_social_security_max_income",
    default=None,
)


@contextmanager
def authorized_social_security_max_income(value: float) -> Iterator[None]:
    """Apply one trusted taxable-maximum override only for the current run."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("authorized Social Security maximum must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError("authorized Social Security maximum must be finite and positive")
    token = _authorized_social_security_max_income.set(normalized)
    try:
        yield
    finally:
        _authorized_social_security_max_income.reset(token)


def get_social_security_rate() -> float:
    """Get the configured social security tax rate"""
    return config.financial.get('tax.fica.social_security_rate', 6.2)


def get_social_security_max_income() -> float:
    """Get the configured social security maximum income"""
    authorized_value = _authorized_social_security_max_income.get()
    if authorized_value is not None:
        return authorized_value
    return config.financial.get('tax.fica.social_security_max_income', 184500)


def get_medicare_rate() -> float:
    """Get the configured medicare tax rate"""
    return config.financial.get('tax.fica.medicare_rate', 1.45)


def get_medicare_additional_rate() -> float:
    """Get the configured additional medicare tax rate"""
    return config.financial.get('tax.fica.medicare_additional_rate', 0.9)


def get_medicare_additional_rate_threshold(filing_status: FilingStatus) -> float:
    """Get the configured medicare additional rate threshold for filing status"""
    key = 'single' if filing_status == FilingStatus.SINGLE else 'married_filing_jointly'
    return config.financial.get(f'tax.fica.medicare_additional_rate_threshold.{key}', 200000)


# Legacy compatibility
social_security_rate = get_social_security_rate()
social_security_max_income = get_social_security_max_income()
medicare_rate = get_medicare_rate()
medicare_additional_rate = get_medicare_additional_rate()

medicare_additional_rate_threshold = {
    FilingStatus.SINGLE: get_medicare_additional_rate_threshold(FilingStatus.SINGLE),
    FilingStatus.MARRIED_FILING_JOINTLY: get_medicare_additional_rate_threshold(FilingStatus.MARRIED_FILING_JOINTLY)
}


# https://www.ssa.gov/oact/cola/cbb.html
# https://www.irs.gov/taxtopics/tc751
# https://smartasset.com/taxes/all-about-the-fica-tax
def social_security_tax(income: float) -> float:
    """ Calculates FICA taxes due
        This includes Social Security and Medicare taxes. """

    # TODO: This code does not account for self-employed individuals, who pay both the
    # employee and employer portions of FICA taxes. See the following for more information:
    # https://www.irs.gov/businesses/small-businesses-self-employed/self-employment-tax-social-security-and-medicare-taxes

    # Calculate social security tax
    social_security_rate = get_social_security_rate()
    social_security_max_income = get_social_security_max_income()
    if income > social_security_max_income:
        tax_amount = social_security_max_income * social_security_rate / 100
    else:
        tax_amount = income * social_security_rate / 100

    return tax_amount


def medicare_tax(income: float, filing_status: FilingStatus) -> float:
    """ Calculates FICA taxes due
        This includes Social Security and Medicare taxes. """

    # TODO: This code does not account for self-employed individuals, who pay both the
    # employee and employer portions of FICA taxes. See the following for more information:
    # https://www.irs.gov/businesses/small-businesses-self-employed/self-employment-tax-social-security-and-medicare-taxes

    # Calculate medicare tax
    medicare_rate = get_medicare_rate()
    medicare_additional_rate = get_medicare_additional_rate()
    tax_amount = income * medicare_rate / 100
    medicare_additional_rate_max = get_medicare_additional_rate_threshold(filing_status)
    if income > medicare_additional_rate_max:
        tax_amount += (income - medicare_additional_rate_max) * medicare_additional_rate / 100

    return tax_amount
