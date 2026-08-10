# -*- coding: utf-8 -*-
"""Run the cashflow engine's own Monte Carlo on the Figure-5 household and
plot the two paper panels: (a) median + 10-90 band, (b) the path fan.

Uses the engine's _infer_assumptions + _run_single_path verbatim, with the
service's seeding scheme (seed 42, path i seeded 42+i), N = 100 paths.
"""
import sys
import random

from pathlib import Path as _P
REPO = str(_P(__file__).resolve().parents[1])
sys.path.insert(0, REPO + r"\backend")
sys.path.insert(0, REPO + r"\backend\advisor\quant_models\cashflow_model\api")

import numpy as np

from advisor.quant_models.cashflow_model.api.app import (  # noqa: E402
    _infer_assumptions,
    _run_single_path,
)

# The household of Figure 5 (verbatim from the conversation):
# 41, married; income 180k/yr; spending 108k/yr excluding the mortgage;
# 520k mortgage (5.5% fixed, 24y -> ~$3,256/mo P&I); 300k retirement,
# 80k cash; retire at 60. Horizon: age 85 (the projection page's horizon).
PAYLOAD = {
    "client_profile": {"age": 41, "retirement_age": 60, "life_expectancy": 85},
    "income": {"salary": 180000},                     # growth: engine default 3%/yr
    "expenses": {
        "base_spending": 108000,                       # growth: engine default 3%/yr
        "housing": {
            "mortgage_balance": 520000,
            "monthly_principal_interest": 3256,        # 5.5% fixed, 24y remaining
        },
    },
    "accounts": {
        "bank": [{"balance": 80000}],
        "retirement": [{"balance": 300000}],           # engine's balanced default
    },
}

N_PATHS, SEED = 100, 42
inputs = _infer_assumptions(PAYLOAD)
print("engine inputs:", {k: v for k, v in inputs.__dict__.items()
                         if k in ("age", "retirement_age", "life_expectancy", "salary",
                                  "annual_expenses", "income_growth", "expense_growth",
                                  "bank_balance", "retirement_balance", "mortgage_balance",
                                  "monthly_housing_cost", "effective_tax_rate",
                                  "retirement_return", "retirement_volatility")})

paths, successes = [], 0
for i in range(N_PATHS):
    r = _run_single_path(inputs, stochastic=True, rng=random.Random(SEED + i))
    snaps = r["yearly_snapshots"]
    paths.append([s["total_assets"] for s in snaps])
    successes += 1 if r["success"] else 0

ages = [s["age"] for s in snaps]
M = np.array(paths)  # [paths, years]
p10, p50, p90 = (np.percentile(M, q, axis=0) for q in (10, 50, 90))
print(f"paths={M.shape} success={successes}/{N_PATHS}")
print(f"terminal p10/p50/p90 = {p10[-1]:,.0f} / {p50[-1]:,.0f} / {p90[-1]:,.0f}")
print(f"peak of median = {p50.max():,.0f} at age {ages[int(p50.argmax())]:.0f}")

# ---------------- plotting ----------------
import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

BLUE = "#0072B2"       # paper's awmblue (Okabe-Ito)
INK = "#3A3F48"
MUT = "#6B7280"
GRID = "#E3E5E9"
PATHG = "#9AA0AA"

plt.rcParams.update({
    "font.family": "serif", "font.size": 6.4,
    "axes.linewidth": 0.5, "axes.edgecolor": MUT,
    "xtick.color": MUT, "ytick.color": MUT,
    "text.color": INK, "axes.labelcolor": INK,
    "xtick.labelsize": 6.0, "ytick.labelsize": 6.0,
})

def frame(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(axis="y", color=GRID, linewidth=0.4)
    ax.set_axisbelow(True)
    ax.tick_params(length=2, width=0.5)
    ax.yaxis.set_major_formatter(FuncFormatter(
        lambda v, _: (("-" if v < 0 else "") + (f"${abs(v)/1e6:.0f}M" if abs(v) >= 1e6 else f"${abs(v)/1e3:.0f}K"))))
    ax.set_xticks([45, 55, 65, 75])
    ax.set_xlim(41, 84.6)
    ax.axvline(60, color=MUT, linewidth=0.5)
    ax.text(60, ax.get_ylim()[1], " retire", fontsize=5.4, color=MUT,
            ha="left", va="top")

OUT = str(_P(__file__).with_name("figs_out"))
import os as _os
_os.makedirs(OUT, exist_ok=True)

# --- (a) median + 10-90 band ---
fig, ax = plt.subplots(figsize=(1.72, 1.52), dpi=300)
ax.fill_between(ages, p10, p90, color=BLUE, alpha=0.16, linewidth=0)
ax.plot(ages, p50, color=BLUE, linewidth=1.5)
ax.plot(ages, p10, color=BLUE, linewidth=0.55, alpha=0.55)
ax.plot(ages, p90, color=BLUE, linewidth=0.55, alpha=0.55)
frame(ax)
ax.text(ages[-1], p50[-1], "  median", fontsize=5.8, color=BLUE,
        ha="left", va="center", clip_on=False)
ax.text(ages[-1], p90[-1], "  p90", fontsize=5.4, color=MUT,
        ha="left", va="center", clip_on=False)
ax.text(ages[-1], p10[-1], "  p10", fontsize=5.4, color=MUT,
        ha="left", va="center", clip_on=False)
ax.set_xlabel("age", fontsize=6.0, labelpad=1.5)
fig.subplots_adjust(left=0.20, right=0.80, top=0.96, bottom=0.20)
fig.savefig(rf"{OUT}\mc-band.pdf")
plt.close(fig)

# --- (b) the fan: every seeded path + percentiles ---
fig, ax = plt.subplots(figsize=(1.72, 1.52), dpi=300)
Y_LO, Y_HI = float(p10.min()) * 1.12, float(p90.max()) * 2.6
clipped = int((M.max(axis=1) > Y_HI).sum())
for row in M:
    ax.plot(ages, row, color=PATHG, linewidth=0.35, alpha=0.38)
ax.set_ylim(Y_LO, Y_HI)
ax.plot(ages, p50, color=BLUE, linewidth=1.5)
ax.plot(ages, p10, color=BLUE, linewidth=0.6, alpha=0.75)
ax.plot(ages, p90, color=BLUE, linewidth=0.6, alpha=0.75)
frame(ax)
ax.text(ages[-1], p90[-1], "  p90", fontsize=5.4, color=BLUE,
        ha="left", va="center", clip_on=False)
ax.text(ages[-1], p50[-1], "  p50", fontsize=5.8, color=BLUE,
        ha="left", va="center", clip_on=False)
ax.text(ages[-1], p10[-1], "  p10", fontsize=5.4, color=BLUE,
        ha="left", va="center", clip_on=False)
ax.set_xlabel("age", fontsize=6.0, labelpad=1.5)
if clipped:
    ax.text(42.5, Y_HI * 0.84, f"{clipped} path{'s' if clipped != 1 else ''} above axis",
            fontsize=5.2, color=MUT, ha="left", va="top")
fig.subplots_adjust(left=0.20, right=0.80, top=0.96, bottom=0.20)
fig.savefig(rf"{OUT}\mc-fan.pdf")
plt.close(fig)

print("panels written: mc-band.pdf, mc-fan.pdf")
