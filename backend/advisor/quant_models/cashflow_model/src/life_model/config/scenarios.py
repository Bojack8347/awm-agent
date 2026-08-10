# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

"""
Predefined scenario configurations for common economic and regulatory conditions.
These scenarios allow users to easily adjust multiple configuration parameters
to model different economic environments without manually changing individual values.
"""

import yaml
from pathlib import Path
from typing import Dict, Any


def _load_scenario_from_yaml(filename: str) -> Dict[str, Any]:
    """Load a scenario configuration from a YAML file

    Args:
        filename: Name of the YAML file (without path)

    Returns:
        Dictionary containing scenario configuration

    Raises:
        FileNotFoundError: If the scenario file cannot be found
    """
    # Try multiple potential locations for scenario files
    potential_paths = [
        Path('config/scenarios') / filename,  # From project root
        Path(__file__).parent.parent.parent.parent / 'config' / 'scenarios' / filename,  # Relative to this file
        Path.cwd() / 'config' / 'scenarios' / filename,  # From current directory
    ]

    for path in potential_paths:
        if path.exists():
            with open(path, 'r') as f:
                return yaml.safe_load(f)

    raise FileNotFoundError(f"Scenario file '{filename}' not found in any of the expected locations")


def get_scenario(name: str) -> Dict[str, Any]:
    """Get a predefined scenario by name

    Args:
        name: Name of the scenario

    Returns:
        Dictionary of configuration overrides for the scenario

    Raises:
        KeyError: If scenario name is not found
        FileNotFoundError: If the scenario file cannot be found
    """
    filename = f"{name}.yaml"
    try:
        return _load_scenario_from_yaml(filename)
    except FileNotFoundError:
        available = list_scenarios()
        raise KeyError(f"Scenario '{name}' not found. Available scenarios: {', '.join(available)}")


def list_scenarios() -> list:
    """Get a list of all available predefined scenarios"""
    scenarios = []

    # Check all potential scenario directories
    scenario_dir_paths = [
        Path('config/scenarios'),
        Path(__file__).parent.parent.parent.parent / 'config' / 'scenarios',
        Path.cwd() / 'config' / 'scenarios',
    ]

    for path in scenario_dir_paths:
        if path.exists() and path.is_dir():
            for yaml_file in path.glob('*.yaml'):
                scenario_name = yaml_file.stem
                if scenario_name not in scenarios:
                    scenarios.append(scenario_name)
            break  # Only use the first directory that exists

    return sorted(scenarios)
