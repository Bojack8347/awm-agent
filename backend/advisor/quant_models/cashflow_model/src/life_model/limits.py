# Copyright 2022 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

from .config.config_manager import config


def job_401k_contrib_limit(age) -> int:
    """Get 401k contribution limit based on age"""
    return config.financial.get_job_401k_contrib_limit(age)


def federal_retirement_age() -> float:
    """Get federal retirement age"""
    return config.financial.get('retirement.federal_retirement_age', 59.5)


def early_withdrawal_penalty_rate() -> float:
    """Penalty rate (percent) on pre-tax retirement withdrawals taken
    before the federal retirement age (IRC §72(t))."""
    return config.financial.get('retirement.early_withdrawal_penalty_rate', 10.0)


# IRS Publication 590-B Appendix B, Table III. The built-in fallback starts at
# the current general RMD age of 73; the configurable default can override this.
rmd_distribution_period = [
    # Age, Distribution Period
    [73,  26.5],
    [74,  25.5],
    [75,  24.6],
    [76,  23.7],
    [77,  22.9],
    [78,  22.0],
    [79,  21.1],
    [80,  20.2],
    [81,  19.4],
    [82,  18.5],
    [83,  17.7],
    [84,  16.8],
    [85,  16.0],
    [86,  15.2],
    [87,  14.4],
    [88,  13.7],
    [89,  12.9],
    [90,  12.2],
    [91,  11.5],
    [92,  10.8],
    [93,  10.1],
    [94,  9.5],
    [95,  8.9],
    [96,  8.4],
    [97,  7.8],
    [98,  7.3],
    [99,  6.8],
    [100, 6.4],
    [101, 6.0],
    [102, 5.6],
    [103, 5.2],
    [104, 4.9],
    [105, 4.6],
    [106, 4.3],
    [107, 4.1],
    [108, 3.9],
    [109, 3.7],
    [110, 3.5],
    [111, 3.4],
    [112, 3.3],
    [113, 3.1],
    [114, 3.0],
    [115, 2.9],
    [116, 2.8],
    [117, 2.7],
    [118, 2.5],
    [119, 2.3],
    [120, 2.0],
]


def get_rmd_distribution_periods() -> list:
    """Get RMD distribution periods from configuration"""
    return config.financial.get('retirement.rmd_distribution_periods', rmd_distribution_period)


def required_min_distrib(age, balance) -> float:
    """Calculate required minimum distribution"""
    periods = get_rmd_distribution_periods()
    age = int(age)
    if age < periods[0][0]:
        return 0
    elif age > periods[-1][0]:
        return balance / periods[-1][1]
    else:
        return balance / [x[1] for x in periods if x[0] == age][0]
