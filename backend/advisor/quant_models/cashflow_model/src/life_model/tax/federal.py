# Copyright 2022 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

from enum import Enum
from ..config.config_manager import config


class FilingStatus(Enum):
    SINGLE = 1
    MARRIED_FILING_JOINTLY = 2


def get_federal_standard_deduction(filing_status: FilingStatus) -> float:
    """Get federal standard deduction for filing status"""
    return config.financial.get_federal_standard_deduction(filing_status)


def get_federal_tax_brackets(filing_status: FilingStatus) -> list:
    """Get federal tax brackets for filing status"""
    return config.financial.get_federal_tax_brackets(filing_status)


# Legacy compatibility - maintain old variable names for backward compatibility
def _get_federal_standard_deduction_dict():
    """Legacy compatibility function"""
    return {
        FilingStatus.SINGLE: get_federal_standard_deduction(FilingStatus.SINGLE),
        FilingStatus.MARRIED_FILING_JOINTLY: get_federal_standard_deduction(FilingStatus.MARRIED_FILING_JOINTLY)
    }


def _get_federal_tax_brackets_dict():
    """Legacy compatibility function"""
    return {
        FilingStatus.SINGLE: get_federal_tax_brackets(FilingStatus.SINGLE),
        FilingStatus.MARRIED_FILING_JOINTLY: get_federal_tax_brackets(FilingStatus.MARRIED_FILING_JOINTLY)
    }


# For backward compatibility, expose these as module attributes
federal_standard_deduction = _get_federal_standard_deduction_dict()
federal_tax_brackets = _get_federal_tax_brackets_dict()


def get_ss_taxation_thresholds(filing_status: FilingStatus) -> tuple:
    """Get the provisional-income thresholds for taxing Social Security benefits.

    These thresholds are set by statute (IRC §86) and are not inflation indexed:
    $25,000/$34,000 for single filers and $32,000/$44,000 for joint filers.
    """
    if filing_status == FilingStatus.MARRIED_FILING_JOINTLY:
        key, base_default, second_default = 'married_filing_jointly', 32000, 44000
    else:
        key, base_default, second_default = 'single', 25000, 34000
    base = config.financial.get(f'tax.federal.social_security_taxation_thresholds.{key}.base', base_default)
    second = config.financial.get(f'tax.federal.social_security_taxation_thresholds.{key}.second', second_default)
    return base, second


def taxable_social_security(ss_benefits: float, other_income: float, filing_status: FilingStatus) -> float:
    """Compute the taxable portion of Social Security benefits (IRC §86 worksheet).

    Provisional income is AGI excluding benefits plus half of the benefits
    (tax-exempt interest is not modeled). Up to 50% of benefits are taxable
    between the base and second thresholds, and up to 85% above the second.

    Args:
        ss_benefits (float): Social Security benefits received for the year.
        other_income (float): AGI excluding Social Security benefits.
        filing_status (FilingStatus): Filing status.

    Returns:
        float: Portion of benefits includable in gross income.
    """
    if ss_benefits <= 0:
        return 0.0

    base_threshold, second_threshold = get_ss_taxation_thresholds(filing_status)
    provisional_income = max(other_income, 0) + 0.5 * ss_benefits

    if provisional_income <= base_threshold:
        return 0.0
    if provisional_income <= second_threshold:
        return min(0.5 * (provisional_income - base_threshold), 0.5 * ss_benefits)
    lower_tier = min(0.5 * (second_threshold - base_threshold), 0.5 * ss_benefits)
    return min(0.85 * (provisional_income - second_threshold) + lower_tier, 0.85 * ss_benefits)


def federal_income_tax(income: float, filing_status: FilingStatus) -> float:
    """Calculates federal income tax due

    Args:
        income (float): Taxable income.
        filing_status (FilingStatus): Filing status for tax purposes.

    Returns:
        total_tax: Amount of tax due based on the taxable income.
    """
    bracket = get_federal_tax_brackets(filing_status)
    total_tax = 0
    for (start, end, percent) in bracket:
        amount_in_bracket = min(max(income - start, 0), end - start)
        if amount_in_bracket == 0:
            break
        total_tax += amount_in_bracket * (percent / 100)
    return round(total_tax)


def federal_marginal_tax_rate(income: float, filing_status: FilingStatus) -> float:
    """Return the ordinary federal marginal rate for the next dollar of taxable income."""
    bracket = get_federal_tax_brackets(filing_status)
    if not bracket:
        return 0.0
    taxable_income = max(income, 0)
    for _start, end, percent in bracket:
        if taxable_income < end:
            return percent
    return bracket[-1][2]


def max_tax_rate(filing_status: FilingStatus) -> float:
    """Get maximum tax rate for filing status"""
    return config.financial.get_max_tax_rate(filing_status)
