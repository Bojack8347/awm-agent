# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

from typing import Optional, Dict, Any
from .financial_config import FinancialConfig
from .scenarios import get_scenario, list_scenarios


class GlobalConfigManager:
    """Global configuration manager for the life-model package"""

    _instance: Optional['GlobalConfigManager'] = None
    _financial_config: Optional[FinancialConfig] = None

    def __new__(cls) -> 'GlobalConfigManager':
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._financial_config is None:
            self._financial_config = FinancialConfig()

    @property
    def financial(self) -> FinancialConfig:
        """Access to financial configuration"""
        return self._financial_config

    def apply_scenario(self, scenario_name: str, overrides: Optional[Dict[str, Any]] = None) -> None:
        """Apply scenario-specific configuration overrides

        Args:
            scenario_name: Name of the scenario (e.g., 'recession', 'high_inflation')
            overrides: Configuration overrides for this scenario. If None, will use
                      predefined scenario with matching name.
        """
        if overrides is None:
            overrides = get_scenario(scenario_name)

        self._financial_config.apply_scenario(scenario_name, overrides)

    def apply_predefined_scenario(self, scenario_name: str) -> None:
        """Apply a predefined scenario by name

        Args:
            scenario_name: Name of the predefined scenario
        """
        overrides = get_scenario(scenario_name)
        self._financial_config.apply_scenario(scenario_name, overrides)

    def list_available_scenarios(self) -> list:
        """Get a list of all available predefined scenarios"""
        return list_scenarios()

    def reset_to_defaults(self) -> None:
        """Reset all configurations to their default values"""
        self._financial_config.reset_to_defaults()

    def get_current_scenario(self) -> Optional[str]:
        """Get the currently applied scenario name"""
        return self._financial_config.scenario


# Global configuration instance
config = GlobalConfigManager()
