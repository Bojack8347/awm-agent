"""Deterministic validators for reviewed IRS, SSA, and CMS snapshots."""

from __future__ import annotations

from typing import Any

from advisor.assumptions.providers.base import GovernmentSnapshotAdapter


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _positive_amount(value: Any) -> bool:
    return _is_number(value) and value > 0


class IRSProviderAdapter(GovernmentSnapshotAdapter):
    provider_id = "irs"
    publisher = "Internal Revenue Service"
    allowed_hosts = frozenset({"irs.gov", "www.irs.gov"})
    supported_variables = frozenset(
        {
            "federal_standard_deduction",
            "federal_tax_brackets",
            "retirement_contribution_limits",
        }
    )

    def validate_value(self, variable_key: str, value: Any) -> None:
        valid = False
        if variable_key == "federal_standard_deduction":
            valid = self._valid_standard_deduction(value)
        elif variable_key == "federal_tax_brackets":
            valid = self._valid_tax_brackets(value)
        elif variable_key == "retirement_contribution_limits":
            valid = self._valid_retirement_limits(value)
        if not valid:
            raise self.invalid_value(variable_key)

    @staticmethod
    def _valid_standard_deduction(value: Any) -> bool:
        required = {"single", "married_filing_jointly", "head_of_household"}
        if not isinstance(value, dict) or set(value) != required:
            return False
        if not all(_positive_amount(value[key]) for key in required):
            return False
        return (
            value["married_filing_jointly"] >= value["single"]
            and value["head_of_household"] >= value["single"]
        )

    @staticmethod
    def _valid_tax_brackets(value: Any) -> bool:
        if not isinstance(value, dict) or set(value) != {
            "single",
            "married_filing_jointly",
        }:
            return False
        for filing_status in ("single", "married_filing_jointly"):
            brackets = value[filing_status]
            if not isinstance(brackets, list) or len(brackets) < 2:
                return False
            expected_lower: int | float = 0
            previous_rate = 0.0
            for index, bracket in enumerate(brackets):
                if not isinstance(bracket, dict) or set(bracket) != {
                    "lower_bound",
                    "upper_bound",
                    "rate_percent",
                }:
                    return False
                lower = bracket["lower_bound"]
                upper = bracket["upper_bound"]
                rate = bracket["rate_percent"]
                if not _is_number(lower) or lower != expected_lower:
                    return False
                if not _is_number(rate) or not 0 < rate <= 100:
                    return False
                if rate <= previous_rate:
                    return False
                if index == len(brackets) - 1:
                    if upper is not None:
                        return False
                else:
                    if not _is_number(upper) or upper <= lower:
                        return False
                    expected_lower = upper
                previous_rate = float(rate)
        return True

    @staticmethod
    def _valid_retirement_limits(value: Any) -> bool:
        if not isinstance(value, dict) or set(value) != {"401k", "ira", "hsa"}:
            return False
        required = {
            "401k": {"base", "catch_up_age_50", "catch_up_age_60_to_63"},
            "ira": {"base", "catch_up_age_50"},
            "hsa": {"self_only", "family"},
        }
        for account_type, fields in required.items():
            item = value.get(account_type)
            if not isinstance(item, dict) or set(item) != fields:
                return False
            if not all(_positive_amount(item[field]) for field in fields):
                return False
        return (
            value["401k"]["catch_up_age_60_to_63"]
            >= value["401k"]["catch_up_age_50"]
            and value["hsa"]["family"] >= value["hsa"]["self_only"]
        )


class SSAProviderAdapter(GovernmentSnapshotAdapter):
    provider_id = "ssa"
    publisher = "Social Security Administration"
    allowed_hosts = frozenset({"ssa.gov", "www.ssa.gov"})
    supported_variables = frozenset(
        {"social_security_cola", "social_security_taxable_maximum"}
    )

    def validate_value(self, variable_key: str, value: Any) -> None:
        if variable_key == "social_security_cola":
            valid = _is_number(value) and 0 <= value <= 25
        elif variable_key == "social_security_taxable_maximum":
            valid = _positive_amount(value) and float(value).is_integer()
        else:
            valid = False
        if not valid:
            raise self.invalid_value(variable_key)


class CMSProviderAdapter(GovernmentSnapshotAdapter):
    provider_id = "cms"
    publisher = "Centers for Medicare & Medicaid Services"
    allowed_hosts = frozenset({"cms.gov", "www.cms.gov"})
    supported_variables = frozenset({"medicare_part_b_premium"})

    def validate_value(self, variable_key: str, value: Any) -> None:
        valid = (
            variable_key == "medicare_part_b_premium"
            and _positive_amount(value)
            and value < 5_000
        )
        if not valid:
            raise self.invalid_value(variable_key)
