# Copyright 2025 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

"""Monte Carlo simulation results aggregation and analysis."""

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


class MonteCarloResults:
    """Aggregate paths in one paths-by-years-by-columns numeric array."""

    PERCENTILES = {
        "Top 5%": 0.95,
        "Top 10%": 0.90,
        "Top 25%": 0.75,
        "Median": 0.50,
        "Bottom 25%": 0.25,
        "Bottom 10%": 0.10,
        "Bottom 5%": 0.05,
    }

    def __init__(self, simulation_results: List[pd.DataFrame]):
        self._raw_results: Optional[List[pd.DataFrame]] = simulation_results
        self.num_simulations = len(simulation_results)
        self._sorted_column_cache: Dict[str, np.ndarray] = {}
        self._percentile_cache: Dict[str, Dict[str, tuple]] = {}

        if self.num_simulations:
            first = simulation_results[0]
            self._columns = list(first.columns)
            self._num_years = len(first)
            self._years = (
                first["Year"].astype(int).tolist()
                if "Year" in first.columns
                else list(range(self._num_years))
            )
            for path_index, result in enumerate(simulation_results):
                if list(result.columns) != self._columns:
                    raise ValueError(
                        f"Monte Carlo path {path_index} has different columns"
                    )
                if len(result) != self._num_years:
                    raise ValueError("Monte Carlo simulation paths must have equal lengths")
            self._data = np.stack(
                [result.to_numpy(dtype=float, copy=False) for result in simulation_results],
                axis=0,
            )
        else:
            self._columns = []
            self._num_years = 0
            self._years = []
            self._data = np.empty((0, 0, 0), dtype=float)
        self._column_index = {
            column: index for index, column in enumerate(self._columns)
        }

    @classmethod
    def from_array(
        cls,
        values: np.ndarray,
        *,
        columns: List[str],
        years: List[int],
    ) -> "MonteCarloResults":
        """Build results without materializing one DataFrame per path."""

        data = np.asarray(values, dtype=float)
        if data.ndim != 3:
            raise ValueError("Monte Carlo result array must be 3-D")
        if data.shape[2] != len(columns):
            raise ValueError("Monte Carlo result column dimension mismatch")
        if data.shape[1] != len(years):
            raise ValueError("Monte Carlo result year dimension mismatch")

        instance = cls([])
        instance._raw_results = None
        instance._data = data
        instance._columns = [str(column) for column in columns]
        instance._column_index = {
            column: index for index, column in enumerate(instance._columns)
        }
        instance._years = [int(year) for year in years]
        instance._num_years = data.shape[1]
        instance.num_simulations = data.shape[0]
        return instance

    @property
    def raw_results(self) -> List[pd.DataFrame]:
        """Materialize legacy per-path DataFrames only when requested."""

        if self._raw_results is None:
            self._raw_results = [
                pd.DataFrame(path.copy(), columns=self._columns)
                for path in self._data
            ]
            if "Year" in self._columns:
                for frame in self._raw_results:
                    frame["Year"] = frame["Year"].astype(int)
        return self._raw_results

    def _column_values(self, column: str) -> np.ndarray:
        index = self._column_index.get(column)
        if index is None:
            raise ValueError(
                f"Column '{column}' not found. Available: {self._columns}"
            )
        return self._data[:, :, index]

    def get_percentile_data(
        self,
        column: str = "Bank Balance",
    ) -> Dict[str, List[float]]:
        """Return historical nearest-rank percentile bands for each year."""

        if self.num_simulations == 0:
            return {name: [] for name in self.PERCENTILES}
        cached = self._percentile_cache.get(column)
        if cached is None:
            sorted_values = self._sorted_values(column)
            cached = {
                name: tuple(
                    sorted_values[
                        min(
                            int(self.num_simulations * percentile),
                            self.num_simulations - 1,
                        )
                    ].tolist()
                )
                for name, percentile in self.PERCENTILES.items()
            }
            self._percentile_cache[column] = cached
        return {name: list(values) for name, values in cached.items()}

    def _sorted_values(self, column: str) -> np.ndarray:
        cached = self._sorted_column_cache.get(column)
        if cached is None:
            cached = np.sort(self._column_values(column), axis=0)
            self._sorted_column_cache[column] = cached
        return cached

    def get_percentile_df(
        self,
        column: str = "Bank Balance",
    ) -> pd.DataFrame:
        data = self.get_percentile_data(column)
        frame = pd.DataFrame(data)
        frame["Year"] = self._years
        return frame.set_index("Year")

    def success_rate(
        self,
        column: str = "Bank Balance",
        min_balance: float = 0,
        all_years: bool = True,
    ) -> float:
        if self.num_simulations == 0:
            return 0.0
        values = self._column_values(column)
        successful = (
            np.all(values >= min_balance, axis=1)
            if all_years
            else values[:, -1] >= min_balance
        )
        return float(np.count_nonzero(successful) / self.num_simulations)

    def first_threshold_crossing_distribution(
        self,
        *,
        column: str,
        threshold: float,
        operator: str,
        event: str,
    ) -> Optional[Dict[str, Any]]:
        """Vectorize a first-event distribution across all retained paths."""

        if self.num_simulations == 0:
            return None
        values = self._column_values(column)[:, 1:]
        if operator == "less_than":
            matches = values < threshold
        elif operator == "greater_than":
            matches = values > threshold
        else:
            raise ValueError(f"Unsupported threshold operator: {operator}")
        matches &= ~np.isnan(values)
        has_event = np.any(matches, axis=1)
        first_offsets = np.argmax(matches, axis=1)
        event_years = np.asarray(self._years[1:], dtype=int)[
            first_offsets[has_event]
        ]
        unique_years, counts = np.unique(event_years, return_counts=True)
        event_counts = {
            int(year): int(count)
            for year, count in zip(unique_years.tolist(), counts.tolist())
        }
        eligible_paths = self.num_simulations
        return {
            "type": "first_threshold_crossing_distribution",
            "event": event,
            "column": column,
            "operator": operator,
            "threshold": float(threshold),
            "probability_by_year": {
                str(year): count / eligible_paths
                for year, count in sorted(event_counts.items())
            },
            "probability_never": (
                eligible_paths - int(np.count_nonzero(has_event))
            )
            / eligible_paths,
            "sample_count": eligible_paths,
            "source": "life_model.monte_carlo.raw_paths",
            "opening_baseline_excluded": True,
        }

    def get_years(self) -> List[int]:
        return self._years.copy()

    def get_final_values(self, column: str = "Bank Balance") -> np.ndarray:
        if self.num_simulations == 0:
            return np.array([])
        return self._column_values(column)[:, -1].copy()

    def get_statistics(
        self,
        column: str = "Bank Balance",
        year_idx: int = -1,
    ) -> Dict[str, float]:
        if self.num_simulations == 0:
            return {}
        values = self._column_values(column)[:, year_idx]
        return {
            "mean": float(np.mean(values)),
            "std": float(np.std(values)),
            "min": float(np.min(values)),
            "max": float(np.max(values)),
            "p5": float(np.percentile(values, 5)),
            "p25": float(np.percentile(values, 25)),
            "p50": float(np.percentile(values, 50)),
            "p75": float(np.percentile(values, 75)),
            "p95": float(np.percentile(values, 95)),
        }

    def get_available_columns(self) -> List[str]:
        return list(self._columns)

    def to_array(self, *, copy: bool = True) -> np.ndarray:
        """Return the paths-by-years-by-columns numeric representation."""

        return self._data.copy() if copy else self._data

    def __repr__(self) -> str:
        return (
            f"MonteCarloResults(num_simulations={self.num_simulations}, "
            f"num_years={self._num_years})"
        )
