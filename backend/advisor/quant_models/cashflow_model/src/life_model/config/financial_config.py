# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

import yaml
from pathlib import Path
from typing import TYPE_CHECKING, Optional, List, Dict
from .base_config import ScenarioConfig
from .models import FinancialConfigModel
from pydantic import ValidationError

if TYPE_CHECKING:
    from ..tax.federal import FilingStatus


class FinancialConfig(ScenarioConfig):
    """Configuration for financial parameters, limits, and rates"""

    def __init__(self, config_file: Optional[str] = None):
        """Initialize financial configuration from YAML file

        Args:
            config_file: Path to configuration file. If None, uses default location.
        """
        self.config_file = config_file
        super().__init__()

    def _initialize_defaults(self) -> None:
        """Initialize default financial configuration values from YAML file"""
        # Determine config file path
        if self.config_file is None:
            # Look for config file relative to the project root
            # Try multiple potential locations
            potential_paths = [
                Path('config/financial_defaults.yaml'),  # From project root
                # Relative to this file
                Path(__file__).parent.parent.parent.parent / 'config' / 'financial_defaults.yaml',
                Path.cwd() / 'config' / 'financial_defaults.yaml',  # From current directory
            ]

            config_found = False
            for path in potential_paths:
                if path.exists():
                    self.config_file = str(path)
                    config_found = True
                    break

            if not config_found:
                raise FileNotFoundError(
                    "Configuration file 'financial_defaults.yaml' not found in any expected location. "
                    "Please ensure the configuration files are included with the package."
                )

        # Load YAML configuration
        with open(self.config_file, 'r') as f:
            raw_config = yaml.safe_load(f)

        # Validate with Pydantic model
        try:
            validated_config = FinancialConfigModel(**raw_config)
            self._config_data = validated_config.model_dump()
        except ValidationError as e:
            raise ValueError(f"Invalid configuration in {self.config_file}: {e}")

    # Convenience methods for commonly accessed values
    def get_federal_standard_deduction(self, filing_status: 'FilingStatus') -> float:
        """Get federal standard deduction for filing status"""
        # Use integer comparison to avoid importing FilingStatus
        key = 'single' if filing_status.value == 1 else 'married_filing_jointly'
        return self.get(f'tax.federal.standard_deduction.{key}')

    def get_federal_tax_brackets(self, filing_status: 'FilingStatus') -> list:
        """Get federal tax brackets for filing status"""
        # Use integer comparison to avoid importing FilingStatus
        key = 'single' if filing_status.value == 1 else 'married_filing_jointly'
        return self.get(f'tax.federal.tax_brackets.{key}')

    def get_job_401k_contrib_limit(self, age: int) -> int:
        """Get 401k contribution limit based on age"""
        base = self.get('retirement.job_401k_contrib_limit.base')
        catch_up_age = self.get('retirement.job_401k_contrib_limit.catch_up_age')
        catch_up_amount = self.get('retirement.job_401k_contrib_limit.catch_up_amount')
        super_catch_up_start_age = self.get('retirement.job_401k_contrib_limit.super_catch_up_start_age')
        super_catch_up_end_age = self.get('retirement.job_401k_contrib_limit.super_catch_up_end_age')
        super_catch_up_amount = self.get('retirement.job_401k_contrib_limit.super_catch_up_amount')

        if super_catch_up_start_age <= age <= super_catch_up_end_age:
            return base + super_catch_up_amount
        return base + (catch_up_amount if age >= catch_up_age else 0)

    def get_max_tax_rate(self, filing_status: 'FilingStatus') -> float:
        """Get maximum tax rate for filing status"""
        brackets = self.get_federal_tax_brackets(filing_status)
        return brackets[-1][2] if brackets else 0.0

    def get_inflation_rate(self) -> float:
        """Get the default annual inflation rate"""
        return self.get('economy.inflation_rate', 0.0)

    # Social Security convenience methods
    def get_avg_wage_index(self, year: int) -> float:
        """Get average wage index for a given year"""
        return self.get(f'social_security.avg_wage_index.{year}')

    def get_cost_of_living_adj(self, year: int) -> float:
        """Get cost of living adjustment for a given year"""
        return self.get(f'social_security.cost_of_living_adj.{year}')

    def get_bend_points(self, year: int) -> List[int]:
        """Get bend points for a given year"""
        return self.get(f'social_security.bend_points.{year}')

    def get_social_security_config(self) -> Dict:
        """Get all social security configuration"""
        return self.get('social_security', {})
