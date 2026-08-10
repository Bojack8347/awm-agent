# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

from typing import Dict, Any, Optional
from abc import ABC, abstractmethod


class ConfigurationManager(ABC):
    """Abstract base class for configuration management"""

    def __init__(self):
        self._config_data: Dict[str, Any] = {}
        self._initialize_defaults()

    @abstractmethod
    def _initialize_defaults(self) -> None:
        """Initialize default configuration values"""
        pass

    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value by key

        Args:
            key: Configuration key (supports dot notation e.g., 'tax.federal.max_rate')
            default: Default value if key not found

        Returns:
            Configuration value or default
        """
        keys = key.split('.')
        current = self._config_data

        for k in keys:
            if isinstance(current, dict) and k in current:
                current = current[k]
            else:
                return default

        return current

    def set(self, key: str, value: Any) -> None:
        """Set a configuration value by key

        Args:
            key: Configuration key (supports dot notation)
            value: Value to set
        """
        keys = key.split('.')
        current = self._config_data

        # Navigate to the parent of the target key
        for k in keys[:-1]:
            if k not in current:
                current[k] = {}
            current = current[k]

        # Set the final key
        current[keys[-1]] = value

    def update(self, config_dict: Dict[str, Any]) -> None:
        """Update configuration with values from a dictionary

        Args:
            config_dict: Dictionary of configuration values
        """
        self._merge_config(self._config_data, config_dict)

    def _merge_config(self, base: Dict[str, Any], update: Dict[str, Any]) -> None:
        """Recursively merge configuration dictionaries"""
        for key, value in update.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._merge_config(base[key], value)
            else:
                base[key] = value

    def get_all(self) -> Dict[str, Any]:
        """Get all configuration data"""
        return self._config_data.copy()

    def reset_to_defaults(self) -> None:
        """Reset configuration to default values"""
        self._config_data.clear()
        self._initialize_defaults()


class ScenarioConfig(ConfigurationManager):
    """Configuration manager that supports scenario-specific overrides"""

    def __init__(self, scenario: Optional[str] = None):
        self.scenario = scenario
        super().__init__()

    def _initialize_defaults(self) -> None:
        """Initialize with base defaults"""
        pass  # Will be overridden by specific config classes

    def apply_scenario(self, scenario: str, overrides: Dict[str, Any]) -> None:
        """Apply scenario-specific configuration overrides

        Args:
            scenario: Name of the scenario
            overrides: Configuration overrides for this scenario
        """
        self.scenario = scenario
        self.update(overrides)

    def reset_to_defaults(self) -> None:
        """Reset configuration to default values and clear scenario"""
        super().reset_to_defaults()
        self.scenario = None
