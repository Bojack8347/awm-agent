# Copyright 2023 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

from ..config.config_manager import config
from .federal import FilingStatus


def _normalize_state_jurisdiction(state: str) -> str:
    jurisdiction = config.financial.get('tax.state.jurisdiction', 'flat') if state is None else state
    if jurisdiction.lower() == 'flat':
        return 'flat'
    return jurisdiction.upper()


def get_state_tax_rate() -> float:
    """Get the configured flat state tax rate."""
    return config.financial.get('tax.state.tax_rate', 0.0) or 0.0


def _filing_status_key(filing_status: FilingStatus) -> str:
    if filing_status == FilingStatus.MARRIED_FILING_JOINTLY:
        return 'married_filing_jointly'
    return 'single'


def _round_tax(amount: float) -> int:
    return int(amount + 0.5)


def _calculate_bracket_tax(taxable_income: float, brackets: list) -> float:
    total_tax = 0.0
    for start, end, percent in brackets:
        amount_in_bracket = min(max(taxable_income - start, 0), end - start)
        if amount_in_bracket == 0:
            break
        total_tax += amount_in_bracket * (percent / 100)
        if taxable_income >= end:
            total_tax = _round_tax(total_tax)
    return total_tax


def _calculate_tax_table_tax(taxable_income: float, brackets: list) -> float:
    if taxable_income < 13:
        return 0
    if taxable_income < 25:
        table_income = 18.5
    elif taxable_income < 50:
        table_income = 37.5
    elif taxable_income < 100:
        table_income = 75
    else:
        table_income = int(taxable_income // 50) * 50 + 25

    return _calculate_bracket_tax(table_income, brackets)


def _phase_in_ratio(value: float, threshold: float) -> float:
    excess = min(max(value - threshold, 0), 50000)
    return round(excess / 50000, 4)


def _apply_high_income_tax_computation(
    adjusted_gross_income: float,
    taxable_income: float,
    schedule_tax: float,
    filing_key: str,
) -> float:
    rules = config.financial.get(f'tax.state.high_income_tax_computation.{filing_key}')
    if not rules or adjusted_gross_income <= rules['agi_threshold']:
        return schedule_tax

    final_flat_rate = rules['final_flat_rate']
    if adjusted_gross_income > final_flat_rate['agi_over']:
        return taxable_income * (final_flat_rate['rate'] / 100)

    low_income_recapture = rules['low_income_recapture']
    if taxable_income <= low_income_recapture['taxable_income_not_over']:
        target_tax = taxable_income * (low_income_recapture['rate'] / 100)
        if adjusted_gross_income >= rules['phase_in_end']:
            return target_tax
        ratio = _phase_in_ratio(adjusted_gross_income, rules['agi_threshold'])
        return schedule_tax + ((target_tax - schedule_tax) * ratio)

    for taxable_over, taxable_not_over, recapture_base, incremental_benefit, agi_excess_over in rules['recapture_segments']:
        if taxable_over < taxable_income <= taxable_not_over:
            ratio = _phase_in_ratio(adjusted_gross_income, agi_excess_over)
            return schedule_tax + recapture_base + (incremental_benefit * ratio)

    return schedule_tax


def new_york_state_income_tax(adjusted_gross_income: float, filing_status: FilingStatus = FilingStatus.SINGLE) -> int:
    """Calculate NY resident income tax from the configured annual IT-201 schedule.

    The model currently uses income passed into this function as a proxy for
    New York adjusted gross income. NY additions, subtractions, credits, NYC tax,
    Yonkers tax, and MCTMT are outside this calculation.
    """
    filing_key = _filing_status_key(filing_status)
    standard_deduction = config.financial.get(f'tax.state.standard_deduction.{filing_key}', 0)
    brackets = config.financial.get(f'tax.state.tax_brackets.{filing_key}', [])
    taxable_income = max(adjusted_gross_income - standard_deduction, 0)
    tax_table = config.financial.get('tax.state.tax_table')
    if (
        tax_table
        and adjusted_gross_income <= tax_table['agi_not_over']
        and taxable_income < tax_table['taxable_income_less_than']
    ):
        return _round_tax(_calculate_tax_table_tax(taxable_income, brackets))

    schedule_tax = _calculate_bracket_tax(taxable_income, brackets)
    tax = _apply_high_income_tax_computation(adjusted_gross_income, taxable_income, schedule_tax, filing_key)
    return _round_tax(tax)


def state_income_tax(
    income: float,
    filing_status: FilingStatus = FilingStatus.SINGLE,
    state: str = None,
) -> float:
    """Calculates state income taxes due.

    Args:
        income (float): Income subject to state income taxes.
        filing_status (FilingStatus): Filing status for tax purposes.
        state (str, optional): State tax jurisdiction override. If None, the
            configured model default is used.

    Returns:
        total_tax: Amount of tax due based on the taxable income.
    """
    jurisdiction = _normalize_state_jurisdiction(state)
    if jurisdiction == 'NY':
        return new_york_state_income_tax(income, filing_status)
    if jurisdiction != 'flat':
        raise NotImplementedError(f"State tax calculation for {jurisdiction} is not implemented")

    return income * get_state_tax_rate() / 100


# Legacy compatibility
state_tax_rate = get_state_tax_rate()
