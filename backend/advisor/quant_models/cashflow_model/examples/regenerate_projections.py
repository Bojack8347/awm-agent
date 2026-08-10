# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

"""Regenerate the CSV artifacts in outputs/projections.

The original files were produced by ad-hoc scripts that were never committed.
This script reconstructs each scenario from the parameters visible in the old
artifacts and is now the canonical generator, so the outputs stay reproducible
as the engine evolves.

Reconstruction notes (parameters recovered from the prior CSVs):
- Single saver (projection_history_2025_2075): age 38, retires at 69, NY;
  salary 90,000 +3%/yr with 5% bonus; 401k 60,000 at 12% pretax contribution,
  50% match, 6% growth; spending 36,000 +2%/yr; bank 25,000 at 0%.
- NY family (ny_family_* files): both spouses age 40 at the 2026 baseline,
  retire at 65 (during 2051), claim Social Security at 67; combined salary
  230,000 +3%/yr
  (split 135,000/95,000 at 10%/8% pretax so contributions equal the old
  21,100 with a 4% match); 401k balances 165,000/110,000 at 6.7% growth;
  family investment 250,000 at 6.8% with 50% cash payout; rent 3,800/month;
  combined spending 110,000; bank 95,000 at 0%. Social Security income
  history is seeded back to age 25 along the same 3% salary path (the old
  seed was not recorded).
- Child comparison files reuse the NY family as the base household. The old
  files used a slightly different household whose inputs were not committed.

Run from the repository root:
    py examples/regenerate_projections.py
"""

import json
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from life_model.cli import (  # noqa: E402
    load_scenario,
    projection_payload,
    run_scenario,
)
from life_model.model import LifeModel  # noqa: E402
from life_model.people.family import Family  # noqa: E402
from life_model.people.person import Person, Spending  # noqa: E402
from life_model.account.bank import BankAccount  # noqa: E402
from life_model.account.investment_return import InvestmentReturn  # noqa: E402
from life_model.account.job401k import Job401kAccount  # noqa: E402
from life_model.dependents.child import Child  # noqa: E402
from life_model.housing.apartment import Apartment  # noqa: E402
from life_model.insurance.social_security import SocialSecurity  # noqa: E402
from life_model.work.job import Job, Salary  # noqa: E402

OUTPUT_DIR = REPO_ROOT / "outputs" / "projections"
SCENARIO_TEMPLATE = REPO_ROOT / "examples" / "scenarios" / "cashflow_scenario_template.json"

TEMPLATE_CSV_COLUMNS = [
    "Year", "Income", "Bank Balance", "401k Balance", "Useable Balance", "Debt",
    "Taxes", "Federal Taxes", "State Taxes", "SS Taxes", "Medicare Taxes",
    "Spending", "401k Contrib", "401k Match", "401k Withdrawals",
    "Investment Return", "Investment Balance", "Cash Investment Return",
    "Bank Interest", "Housing", "Child Costs", "Child Family Contributions",
    "529 Contributions", "529 Withdrawals", "529 Balance", "Owns Home",
    "Rents Apartment",
]

SIMPLE_HISTORY_COLUMNS = [
    "Year", "Income", "Bank Balance", "401k Balance", "Useable Balance", "Debt",
    "Taxes", "Spending", "401k Contrib", "401k Match", "RMDs", "Housing",
    "Interest Paid", "SS Income", "Charity", "Federal Taxes", "State Taxes",
    "SS Taxes", "Medicare Taxes", "Life Ins Premiums", "Life Ins Cash Value",
    "Death Benefits",
]

FAMILY_HISTORY_COLUMNS = [
    "Year", "Income", "Bank Balance", "401k Balance", "Useable Balance", "Debt",
    "Taxes", "Spending", "401k Contrib", "401k Match", "Investment Return",
    "Family Investment Return", "Reinvested Family Return", "Investment Balance",
    "Cash Investment Return", "RMDs", "Housing", "Interest Paid", "SS Income",
    "Charity", "Federal Taxes", "State Taxes", "SS Taxes", "Medicare Taxes",
    "Life Ins Premiums", "Life Ins Cash Value", "Death Benefits",
    "Total Outflows", "Total Cash Inflows", "Net Cashflow", "Total Taxes",
]

FAMILY_INFLOW_COLUMNS = [
    "Year", "Income", "Wage Cash Inflow", "Bank Balance", "401k Balance",
    "Useable Balance", "Debt", "Taxes", "Spending", "401k Contrib", "401k Match",
    "401k Withdrawals", "Investment Return", "Family Investment Return",
    "Reinvested Family Return", "Investment Balance", "Cash Investment Return",
    "Bank Interest", "RMDs", "Housing", "Interest Paid", "SS Income", "Charity",
    "Federal Taxes", "State Taxes", "SS Taxes", "Medicare Taxes",
    "Life Ins Premiums", "Life Ins Cash Value", "Death Benefits",
    "Total Outflows", "Total Cash Inflows", "Net Cashflow", "Total Taxes",
]

COMPARISON_METRICS = [
    "Cash Inflows", "Spending", "Child Costs", "Child Family Contributions",
    "Housing", "Taxes", "Cash Outflows", "Net Cash Flow Estimate",
    "Bank Balance Change", "Bank Balance", "401k Balance", "Investment Balance",
    "Debt",
]

CHILD_PARAMS = dict(
    birth_year=2028,
    birth_adoption_cost=5000,
    childcare_annual_cost=14000,
    school_activity_annual_cost=3500,
    college_annual_cost=30000,
    annual_529_contribution=0,
    childcare_start_age=0,
    childcare_end_age=5,
    school_start_age=6,
    school_end_age=17,
    college_start_age=18,
    college_end_age=21,
    independence_age=22,
    yearly_increase=2.5,
)

CHILD_CONTRIBUTION_PARAMS = dict(
    child_income=30000,
    child_income_start_age=22,
    child_income_end_age=26,
    child_income_yearly_increase=2.0,
    family_contribution_percent=20,
)


def write_csv(df: pd.DataFrame, filename: str, float_format=None) -> None:
    path = OUTPUT_DIR / filename
    df.to_csv(path, index=False, float_format=float_format, lineterminator="\n")
    print(f"wrote {path.relative_to(REPO_ROOT)}")


def regenerate_template_outputs() -> None:
    """cashflow_projection.csv and prediction.csv from the committed template."""
    scenario = load_scenario(SCENARIO_TEMPLATE)
    model, projection = run_scenario(scenario)

    df = projection.rename(columns={"Cashflow Shortfall Debt": "Debt"})
    write_csv(df[TEMPLATE_CSV_COLUMNS], "cashflow_projection.csv", float_format="%.2f")

    payload = projection_payload(model, projection)
    path = OUTPUT_DIR / "prediction.csv"
    path.write_text(json.dumps(payload), encoding="utf-8")
    print(f"wrote {path.relative_to(REPO_ROOT)} (JSON payload, legacy filename)")


def regenerate_single_saver() -> None:
    """projection_history_2025_2075.csv — single NY saver."""
    model = LifeModel(start_year=2025, end_year=2075)
    family = Family(model, "Single NY Saver")
    person = Person(
        family=family,
        name="Sam",
        age=38,
        retirement_age=69,
        spending=Spending(model, base=36000, yearly_increase=2.0),
        state="NY",
    )
    BankAccount(owner=person, company="Bank", type="Checking", balance=25000, interest_rate=0.0)
    job = Job(
        owner=person,
        company="Employer",
        role="Professional",
        salary=Salary(model, base=90000, yearly_increase=3.0, yearly_bonus=5.0),
    )
    Job401kAccount(
        job=job,
        pretax_balance=60000,
        pretax_contrib_percent=12,
        average_growth=6.0,
        company_match_percent=50,
    )
    model.run()

    df = model.datacollector.get_model_vars_dataframe()
    df = df.rename(columns={"Cashflow Shortfall Debt": "Debt"})
    write_csv(df[SIMPLE_HISTORY_COLUMNS], "projection_history_2025_2075.csv")


def build_ny_family(child_variant: str = "none"):
    """Build the NY family model.

    child_variant: 'none', 'independent' (child without family contributions),
    or 'contributes' (child sends back 20% of early-career income).
    """
    model = LifeModel(start_year=2026, end_year=2076)
    family = Family(model, "NY Family")
    avery = Person(
        family=family,
        name="Avery",
        age=40,
        retirement_age=65,
        spending=Spending(model, base=55000, yearly_increase=None),
        state="NY",
    )
    blake = Person(
        family=family,
        name="Blake",
        age=40,
        retirement_age=65,
        spending=Spending(model, base=55000, yearly_increase=None),
        state="NY",
    )
    avery.get_married(blake)

    BankAccount(owner=avery, company="NY Bank", type="Checking", balance=95000, interest_rate=0.0)
    Apartment(person=avery, name="Rental", monthly_rent=3800, yearly_increase=None)

    job_a = Job(owner=avery, company="Employer A", role="Professional",
                salary=Salary(model, base=135000, yearly_increase=3.0))
    Job401kAccount(job=job_a, pretax_balance=165000, pretax_contrib_percent=10,
                   average_growth=6.7, company_match_percent=4)
    job_b = Job(owner=blake, company="Employer B", role="Manager",
                salary=Salary(model, base=95000, yearly_increase=3.0))
    Job401kAccount(job=job_b, pretax_balance=110000, pretax_contrib_percent=8,
                   average_growth=6.7, company_match_percent=4)

    investment = InvestmentReturn(
        family=family,
        balance=250000,
        growth_rate=6.8,
        payout_to_bank=True,
        cash_payout_rate=50,
        taxable=True,
    )

    # Seed each spouse's covered-earnings history back to age 25 along the
    # same 3% salary path. The year-specific wage base caps each entry.
    for person, base_salary in ((avery, 135000), (blake, 95000)):
        history = [
            (year, base_salary * (1.03 ** (year - 2026)))
            for year in range(2011, 2026)
        ]
        SocialSecurity(person=person, withdrawal_start_age=67, income_history=history)

    if child_variant != "none":
        params = dict(CHILD_PARAMS)
        if child_variant == "contributes":
            params.update(CHILD_CONTRIBUTION_PARAMS)
        Child(person=avery, name="Casey", **params)

    return model, investment


def family_projection_frames(child_variant: str = "none"):
    """Run the NY family and return (model df, family investment return series)."""
    model, investment = build_ny_family(child_variant)
    model.run()
    df = model.datacollector.get_model_vars_dataframe().reset_index(drop=True)
    df = df.rename(columns={"Cashflow Shortfall Debt": "Debt"})

    agent_df = model.datacollector.get_agent_vars_dataframe()
    family_return = (
        agent_df.xs(investment.unique_id, level="AgentID")["Investment Return"]
        .reset_index(drop=True)
    )
    df["Family Investment Return"] = family_return
    df["Reinvested Family Return"] = df["Family Investment Return"] - df["Cash Investment Return"]
    return df


def add_family_cashflow_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Derived columns shared by the two family history files.

    Cash inflows/outflows treat retirement-account movements (contributions
    aside) as internal transfers, matching the original files' convention.
    """
    df = df.copy()
    df["Wage Cash Inflow"] = df["Income"] - df["401k Contrib"]
    inflows_excluding_wages = (
        df["Cash Investment Return"] + df["Bank Interest"] + df["SS Income"]
        + df["Child Family Contributions"]
    )
    outflows_excluding_contrib = (
        df["Taxes"] + df["Spending"] + df["Housing"] + df["Charity"] + df["Child Costs"]
    )
    df["Total Cash Inflows"] = df["Income"] + inflows_excluding_wages
    df["Total Outflows"] = outflows_excluding_contrib + df["401k Contrib"]
    df["Net Cashflow"] = df["Total Cash Inflows"] - df["Total Outflows"]
    df["Total Taxes"] = df["Taxes"]
    return df


def regenerate_family_histories() -> None:
    df = add_family_cashflow_columns(family_projection_frames("none"))
    write_csv(df[FAMILY_HISTORY_COLUMNS], "ny_family_projection_history_2026_2076.csv")

    inflow_df = df.copy()
    # The all-cash-inflows variant nets 401k contributions out of both sides.
    inflow_df["Total Cash Inflows"] = inflow_df["Total Cash Inflows"] - inflow_df["401k Contrib"]
    inflow_df["Total Outflows"] = inflow_df["Total Outflows"] - inflow_df["401k Contrib"]
    write_csv(inflow_df[FAMILY_INFLOW_COLUMNS],
              "ny_family_projection_history_2026_2076_all_cash_inflows.csv")


def comparison_frame(df: pd.DataFrame, scenario_name: str, start_bank: float) -> pd.DataFrame:
    """Per-year cashflow comparison rows for one scenario."""
    df = df.copy()
    df["Scenario"] = scenario_name
    df["Cash Inflows"] = (
        df["Income"] - df["401k Contrib"] + df["Cash Investment Return"]
        + df["Bank Interest"] + df["SS Income"] + df["Child Family Contributions"]
    )
    df["Cash Outflows"] = (
        df["Spending"] + df["Child Costs"] + df["529 Contributions"]
        + df["Housing"] + df["Taxes"]
    )
    df["Net Cash Flow Estimate"] = df["Cash Inflows"] - df["Cash Outflows"]
    df["Bank Balance Change"] = df["Bank Balance"].diff()
    df.loc[df.index[0], "Bank Balance Change"] = df["Bank Balance"].iloc[0] - start_bank
    # The opening row only holds start-of-simulation stats
    df = df[df["Year"] > df["Year"].min()]
    return df[["Year", "Scenario", "Income"] + COMPARISON_METRICS]


def summary_metrics(comparison: pd.DataFrame, end_year: int) -> dict:
    last_row = comparison[comparison["Year"] == end_year].iloc[0]
    return {
        "Cumulative child costs": comparison["Child Costs"].sum(),
        "Cumulative child family contributions": comparison["Child Family Contributions"].sum(),
        "Cumulative cash outflows": comparison["Cash Outflows"].sum(),
        "Cumulative net cash flow estimate": comparison["Net Cash Flow Estimate"].sum(),
        f"Ending bank balance ({end_year})": last_row["Bank Balance"],
        f"Ending 401k balance ({end_year})": last_row["401k Balance"],
        f"Ending debt ({end_year})": last_row["Debt"],
    }


def delta_frame(base: pd.DataFrame, variants: dict) -> pd.DataFrame:
    """Per-year metric deltas of each variant against the base scenario."""
    delta = pd.DataFrame({"Year": base["Year"].values})
    base_indexed = base.set_index("Year")
    for name, frame in variants.items():
        variant_indexed = frame.set_index("Year")
        for metric in COMPARISON_METRICS:
            column = f"Delta {metric} ({name} - No child)"
            delta[column] = (variant_indexed[metric] - base_indexed[metric]).values
    return delta


def regenerate_child_comparisons() -> None:
    end_year = 2076
    start_bank = 95000

    base = comparison_frame(family_projection_frames("none"), "No child", start_bank)
    independent = comparison_frame(family_projection_frames("independent"),
                                   "Child independent", start_bank)
    contributes = comparison_frame(family_projection_frames("contributes"),
                                   "Child contributes", start_bank)

    # Choice files: no child vs one child (no family contributions)
    choice = pd.concat(
        [base, independent.assign(Scenario="Have one child")],
        ignore_index=True,
    ).sort_values(["Year", "Scenario"], kind="stable")
    choice_columns = [c for c in choice.columns if c != "Child Family Contributions"]
    write_csv(choice[choice_columns],
              "ny_family_child_choice_projection_comparison_2026_2076.csv")

    choice_delta = delta_frame(base, {"child": independent})
    choice_delta.columns = [c.replace("(child - No child)", "(child - no child)")
                            for c in choice_delta.columns]
    drop = [c for c in choice_delta.columns if "Child Family Contributions" in c]
    write_csv(choice_delta.drop(columns=drop),
              "ny_family_child_choice_projection_delta_2026_2076.csv")

    base_summary = summary_metrics(base, end_year)
    child_summary = summary_metrics(independent, end_year)
    choice_summary = pd.DataFrame([
        {
            "Metric": metric,
            "No child": base_summary[metric],
            "Have one child": child_summary[metric],
            "Difference": child_summary[metric] - base_summary[metric],
        }
        for metric in base_summary
        if metric != "Cumulative child family contributions"
    ])
    write_csv(choice_summary, "ny_family_child_choice_summary_2026_2076.csv")

    # Subscenario files: no child vs independent child vs contributing child
    subscenario = pd.concat([base, independent, contributes], ignore_index=True)
    subscenario = subscenario.sort_values(["Year", "Scenario"], kind="stable")
    write_csv(subscenario,
              "ny_family_child_subscenario_projection_comparison_2026_2076.csv")

    subscenario_delta = delta_frame(
        base,
        {"Child independent": independent, "Child contributes": contributes},
    )
    write_csv(subscenario_delta,
              "ny_family_child_subscenario_projection_delta_2026_2076.csv")

    subscenario_summary = pd.DataFrame([
        {"Scenario": name, **summary_metrics(frame, end_year)}
        for name, frame in (
            ("No child", base),
            ("Child independent", independent),
            ("Child contributes", contributes),
        )
    ])
    write_csv(subscenario_summary, "ny_family_child_subscenario_summary_2026_2076.csv")


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    regenerate_template_outputs()
    regenerate_single_saver()
    regenerate_family_histories()
    regenerate_child_comparisons()
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
