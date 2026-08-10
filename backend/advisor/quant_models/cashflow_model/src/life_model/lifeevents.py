# Copyright 2022 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, Optional, List, Callable, Union
from .model import LifeModelAgent


class LifeEventName(str, Enum):
    """Fixed names for model-aware life events."""

    MARRIAGE = "Marriage"
    DIVORCE = "Divorce"
    CHILD_BIRTH_OR_ADOPTION = "Child birth/adoption"
    HOME_PURCHASE = "Home purchase"
    GO_TO_COLLEGE = "Go to college"


FIXED_LIFE_EVENT_NAMES = tuple(event.value for event in LifeEventName)


class RecurringInvestmentContribution(LifeModelAgent):
    """Transfer available cash to the modeled taxable investment account."""

    def __init__(self, person, annual_amount: float, end_year: int):
        super().__init__(person.model)
        if annual_amount <= 0:
            raise ValueError("Recurring investment contribution must be positive")
        self.person = person
        self.annual_amount = float(annual_amount)
        self.end_year = int(end_year)
        self.total_contributed = 0.0
        self.last_contribution_year = None

    def pre_step(self):
        self.contribute_for_current_year()

    def contribute_for_current_year(self):
        if (
            self.model.year > self.end_year
            or self.person.is_deceased
            or self.last_contribution_year == self.model.year
        ):
            return
        accounts = list(getattr(self.person, "investment_returns", []))
        if not accounts:
            raise ValueError(
                "Recurring investment contribution found no modeled taxable "
                f"investment account for {self.person.name}"
            )
        remaining = self.person.deduct_from_cashflow_bank_accounts(
            self.annual_amount
        )
        contributed = self.annual_amount - remaining
        if contributed > 0:
            accounts[0].deposit(contributed)
            self.total_contributed += contributed
        self.last_contribution_year = self.model.year


@dataclass
class FinancialDecisionEvent:
    """Structured financial decision that can be chosen by another agent.

    This object is intentionally data-oriented: a decision agent can choose an
    event name, year, and payload without knowing how the cashflow model applies
    the resulting state changes.
    """

    year: int
    event_name: Union[str, LifeEventName]
    payload: Dict[str, Any] = field(default_factory=dict)
    description: Optional[str] = None

    def __post_init__(self):
        try:
            self.event_name = (
                self.event_name
                if isinstance(self.event_name, LifeEventName)
                else LifeEventName(str(self.event_name))
            )
        except ValueError as exc:
            raise ValueError(
                f"Unsupported financial decision event: {self.event_name}. "
                f"Expected one of {FIXED_LIFE_EVENT_NAMES}."
            ) from exc

    @property
    def name(self) -> str:
        return self.event_name.value

    def to_life_event(self) -> 'LifeEvent':
        """Convert this structured decision into an executable life event."""
        return LifeEvent.from_financial_decision(self)

    @classmethod
    def marriage(cls, year: int, person, spouse, description: Optional[str] = None):
        return cls(
            year=year,
            event_name=LifeEventName.MARRIAGE,
            payload={'person': person, 'spouse': spouse},
            description=description,
        )

    @classmethod
    def divorce(cls, year: int, person, spouse=None, description: Optional[str] = None):
        return cls(
            year=year,
            event_name=LifeEventName.DIVORCE,
            payload={'person': person, 'spouse': spouse},
            description=description,
        )

    @classmethod
    def child_birth_or_adoption(cls, year: int, person, name: str, description: Optional[str] = None, **child_kwargs):
        return cls(
            year=year,
            event_name=LifeEventName.CHILD_BIRTH_OR_ADOPTION,
            payload={'person': person, 'name': name, **child_kwargs},
            description=description,
        )

    @classmethod
    def home_purchase(
        cls,
        year: int,
        person,
        name: str,
        purchase_price: float,
        down_payment: float,
        description: Optional[str] = None,
        **home_kwargs,
    ):
        return cls(
            year=year,
            event_name=LifeEventName.HOME_PURCHASE,
            payload={
                'person': person,
                'name': name,
                'purchase_price': purchase_price,
                'down_payment': down_payment,
                **home_kwargs,
            },
            description=description,
        )

    @classmethod
    def go_to_college(
        cls,
        year: int,
        person,
        annual_cost: float,
        years: int = 4,
        description: Optional[str] = None,
        **college_kwargs,
    ):
        return cls(
            year=year,
            event_name=LifeEventName.GO_TO_COLLEGE,
            payload={
                'person': person,
                'annual_cost': annual_cost,
                'years': years,
                **college_kwargs,
            },
            description=description,
        )


def _payload_value(decision: FinancialDecisionEvent, key: str):
    if key not in decision.payload:
        raise ValueError(f"{decision.name} decision requires payload field '{key}'")
    return decision.payload[key]


def _apply_divorce(person, spouse=None):
    from .model import Event
    from .tax.federal import FilingStatus

    spouse = spouse or person.spouse
    person.spouse = None
    person.filing_status = FilingStatus.SINGLE

    if spouse is not None:
        spouse.spouse = None
        spouse.filing_status = FilingStatus.SINGLE
        event_str = f"{person.name} and {spouse.name} got divorced"
    else:
        event_str = f"{person.name} got divorced"

    person.model.event_log.add(Event(event_str))


def _apply_child_birth_or_adoption(person, name: str, event_year: int, child_kwargs: dict):
    from .dependents.child import Child
    from .model import Event

    child_kwargs = dict(child_kwargs)
    birth_year = child_kwargs.pop('birth_year', None)
    adoption_year = child_kwargs.pop('adoption_year', None)

    if birth_year is None:
        birth_year = event_year
    if adoption_year is None and birth_year != event_year:
        adoption_year = event_year

    child = Child(
        person=person,
        name=name,
        birth_year=birth_year,
        adoption_year=adoption_year,
        **child_kwargs,
    )
    child.apply_current_year_transactions()

    event_type = "adopted" if adoption_year == event_year and birth_year != event_year else "had"
    person.model.event_log.add(Event(f"{person.name} {event_type} child {name}"))
    return child


def _apply_home_purchase(person, event_year: int, home_kwargs: dict):
    from .config.config_manager import config
    from .housing.home import Home, HomeExpenses, Mortgage
    from .model import Event

    home_kwargs = dict(home_kwargs)
    name = home_kwargs['name']
    purchase_price = home_kwargs['purchase_price']
    down_payment = home_kwargs['down_payment']
    mortgage = home_kwargs['mortgage']
    expenses = home_kwargs['expenses']

    if home_kwargs['stop_renting']:
        for apartment in list(person.apartments):
            person.model.registries.apartments.unregister(person, apartment)

        if mortgage is None:
            loan_amount = max(0, purchase_price - down_payment)
            mortgage = Mortgage(
                loan_amount=loan_amount,
                start_date=event_year,
                length_years=home_kwargs['mortgage_years'],
                yearly_interest_rate=home_kwargs['mortgage_rate'],
                mortgage_type=home_kwargs['mortgage_type'],
            )

    if expenses is None:
        inflation = config.financial.get_inflation_rate()
        maintenance_increase = home_kwargs['maintenance_increase']
        improvement_increase = home_kwargs['improvement_increase']
        hoa_increase = home_kwargs['hoa_increase']

        expenses = HomeExpenses(
            model=person.model,
            property_tax_percent=home_kwargs['property_tax_percent'],
            home_insurance_percent=home_kwargs['home_insurance_percent'],
            maintenance_amount=home_kwargs['maintenance_amount'],
            maintenance_increase=inflation if maintenance_increase is None else maintenance_increase,
            improvement_amount=home_kwargs['improvement_amount'],
            improvement_increase=inflation if improvement_increase is None else improvement_increase,
            hoa_amount=home_kwargs['hoa_amount'],
            hoa_increase=inflation if hoa_increase is None else hoa_increase,
        )

    home = Home(
        person=person,
        name=name,
        purchase_price=purchase_price,
        value_yearly_increase=home_kwargs['value_yearly_increase'],
        down_payment=down_payment,
        mortgage=mortgage,
        expenses=expenses,
    )

    upfront_cash = down_payment + home_kwargs['closing_costs']
    if upfront_cash:
        person.spending.add_expense(upfront_cash, 'home_purchase_costs')

    person.model.event_log.add(Event(f"{person.name} bought home {name}"))
    return home


def _coerce_student_loan_type(value):
    from .debt.student_loan import StudentLoanType

    if isinstance(value, StudentLoanType):
        return value
    normalized = str(value).strip().lower().replace(' ', '_').replace('-', '_')
    aliases = {
        'federal_subsidized': StudentLoanType.FEDERAL_SUBSIDIZED,
        'subsidized': StudentLoanType.FEDERAL_SUBSIDIZED,
        'federal_unsubsidized': StudentLoanType.FEDERAL_UNSUBSIDIZED,
        'unsubsidized': StudentLoanType.FEDERAL_UNSUBSIDIZED,
        'private': StudentLoanType.PRIVATE,
        'plus': StudentLoanType.PLUS,
    }
    if normalized in aliases:
        return aliases[normalized]
    raise ValueError(
        "Unsupported student loan_type. Expected one of: "
        "federal_subsidized, federal_unsubsidized, private, plus"
    )


def _apply_go_to_college(person, event_year: int, college_kwargs: dict):
    from .model import Event

    college_kwargs = dict(college_kwargs)
    annual_cost = float(college_kwargs['annual_cost'])
    years = int(college_kwargs['years'])
    if years < 1:
        raise ValueError("Go to college event requires years >= 1")

    cost_yearly_increase = float(college_kwargs['cost_yearly_increase'])
    defer_income = bool(college_kwargs['defer_income'])
    during_college_salary = float(college_kwargs['during_college_salary'])
    post_college_salary = college_kwargs['post_college_salary']
    salary_yearly_increase = college_kwargs['salary_yearly_increase']
    yearly_bonus = college_kwargs['yearly_bonus']
    job_company = college_kwargs['job_company']
    job_role = college_kwargs['job_role']
    finance_with_student_loan = bool(college_kwargs.get('finance_with_student_loan', False))

    if defer_income:
        for job in person.jobs:
            job.salary.base = during_college_salary

    student_loan = None
    if finance_with_student_loan:
        from .debt.student_loan import StudentLoan

        student_loan = StudentLoan(
            person=person,
            loan_type=_coerce_student_loan_type(college_kwargs.get('loan_type', 'federal_unsubsidized')),
            loan_amount=annual_cost,
            yearly_interest_rate=float(college_kwargs.get('loan_interest_rate', 6.5)),
            length_years=int(college_kwargs.get('loan_term_years', 10)),
            school_name=str(college_kwargs.get('school_name', 'College')),
            deferment_end_year=event_year + years - 1,
        )
    else:
        person.spending.add_expense(annual_cost, 'education_costs')
    person.model.event_log.add(Event(f"{person.name} started college"))

    follow_up_events = []
    for offset in range(1, years):
        cost = annual_cost * ((1 + cost_yearly_increase / 100) ** offset)
        if student_loan is not None:
            follow_up_events.append(
                LifeEvent(
                    event_year + offset,
                    "College loan disbursement",
                    student_loan.borrow,
                    cost,
                )
            )
        else:
            follow_up_events.append(
                LifeEvent(
                    event_year + offset,
                    "College cost",
                    person.spending.add_expense,
                    cost,
                    'education_costs',
                )
            )

    def graduate():
        from .work.job import Job, Salary

        if post_college_salary is not None:
            if person.jobs:
                job = person.jobs[0]
                job.salary.base = float(post_college_salary)
                if salary_yearly_increase is not None:
                    job.salary.yearly_increase = float(salary_yearly_increase)
                if yearly_bonus is not None:
                    job.salary.yearly_bonus = float(yearly_bonus)
                if job_company is not None:
                    job.company = str(job_company)
                if job_role is not None:
                    job.role = str(job_role)
            else:
                salary = Salary(
                    person.model,
                    base=float(post_college_salary),
                    yearly_increase=(
                        None
                        if salary_yearly_increase is None
                        else float(salary_yearly_increase)
                    ),
                    yearly_bonus=0.0 if yearly_bonus is None else float(yearly_bonus),
                )
                Job(
                    owner=person,
                    company="Employer" if job_company is None else str(job_company),
                    role="Post-college job" if job_role is None else str(job_role),
                    salary=salary,
                )

        person.model.event_log.add(Event(f"{person.name} graduated college"))

    follow_up_events.append(
        LifeEvent(
            event_year + years,
            "College graduation",
            graduate,
            run_in_decision_step=True,
        )
    )
    LifeEvents(person.model, follow_up_events)


class LifeEvents(LifeModelAgent):
    def __init__(self, model, life_events: Optional[Iterable[Union['LifeEvent', FinancialDecisionEvent]]] = None):
        """List of life events

        Args:
            model (LifeModel): LifeModel in which the life events take place.
            life_events: Executable life events or structured financial
                decision events. Defaults to None.
        """
        super().__init__(model)
        self.life_events = []
        self.financial_decision_events = []
        self.add_events([] if life_events is None else life_events)

    def _normalize_event(self, event: Union['LifeEvent', FinancialDecisionEvent]) -> 'LifeEvent':
        if isinstance(event, FinancialDecisionEvent):
            self.financial_decision_events.append(event)
            return event.to_life_event()
        if isinstance(event, LifeEvent):
            return event
        raise TypeError(f"Unsupported life event type: {type(event).__name__}")

    def add_event(self, event: Union['LifeEvent', FinancialDecisionEvent]) -> 'LifeEvent':
        """Schedule a life event or structured financial decision event."""
        life_event = self._normalize_event(event)
        self.life_events.append(life_event)
        return life_event

    def add_events(self, events: Iterable[Union['LifeEvent', FinancialDecisionEvent]]):
        """Schedule multiple life events or structured financial decision events."""
        for event in events:
            self.add_event(event)

    def add_decision_event(self, event: FinancialDecisionEvent) -> 'LifeEvent':
        """Schedule a structured financial decision event."""
        return self.add_event(event)

    def _repr_html_(self):
        table = "<table>"
        table += "<tr><th>Year:</th><th>Event:</th></tr>\n"
        table += "".join(f"<tr><td>{x.year}</td><td>{x.name}</td></tr>\n" for x in self.life_events)
        table += "</table>"
        return table

    def _apply_due_events(self, decision_step_only: bool = False):
        remaining_events = []
        for event in self.life_events:
            if decision_step_only and not event.run_in_decision_step:
                remaining_events.append(event)
                continue
            if event.eval_event(self.model.year):
                continue
            remaining_events.append(event)
        self.life_events = remaining_events

    def decision_step(self):
        # Some events must update salaries before job pre_step creates income.
        self._apply_due_events(decision_step_only=True)

    def pre_step(self):
        # Decision agents may add current-year events during decision_step.
        self._apply_due_events()

    @staticmethod
    def fixed_event_names():
        """Return the fixed event names recognized by adaptive event helpers."""
        return FIXED_LIFE_EVENT_NAMES


class LifeEvent():
    def __init__(
        self,
        year: int,
        name: Union[str, LifeEventName],
        event: Callable,
        *event_args,
        description: Optional[str] = None,
        run_in_decision_step: bool = False,
    ):
        """Life Event.

        Args:
            year (int): Year in which the life event takes place.
            name (str): Name of the event. Prefer ``LifeEventName`` for
                model-aware events.
            event (Callable): Callable performed at the specified year.
            *event_args: Arguments to pass to the event callable.
        """
        self.year = year
        self.event_type = self._get_event_type(name)
        self.name = self.event_type.value if self.event_type is not None else str(name)
        self.event = event
        self.event_args = event_args
        self.description = description or self.name
        self.decision_event = None
        self.result = None
        self.run_in_decision_step = run_in_decision_step

    @staticmethod
    def _get_event_type(name: Union[str, LifeEventName]) -> Optional[LifeEventName]:
        if isinstance(name, LifeEventName):
            return name
        try:
            return LifeEventName(str(name))
        except ValueError:
            return None

    @staticmethod
    def fixed_event_names():
        """Return the fixed event names recognized by adaptive event helpers."""
        return FIXED_LIFE_EVENT_NAMES

    @classmethod
    def from_financial_decision(cls, decision: FinancialDecisionEvent):
        """Create an executable event from a structured financial decision."""
        payload = dict(decision.payload)

        if decision.event_name == LifeEventName.MARRIAGE:
            event = cls.marriage(
                decision.year,
                _payload_value(decision, 'person'),
                _payload_value(decision, 'spouse'),
            )
        elif decision.event_name == LifeEventName.DIVORCE:
            event = cls.divorce(
                decision.year,
                _payload_value(decision, 'person'),
                payload.get('spouse'),
            )
        elif decision.event_name == LifeEventName.CHILD_BIRTH_OR_ADOPTION:
            person = _payload_value(decision, 'person')
            name = _payload_value(decision, 'name')
            payload.pop('person', None)
            payload.pop('name', None)
            event = cls.child_birth_or_adoption(decision.year, person, name, **payload)
        elif decision.event_name == LifeEventName.HOME_PURCHASE:
            person = _payload_value(decision, 'person')
            name = _payload_value(decision, 'name')
            purchase_price = _payload_value(decision, 'purchase_price')
            down_payment = _payload_value(decision, 'down_payment')
            payload.pop('person', None)
            payload.pop('name', None)
            payload.pop('purchase_price', None)
            payload.pop('down_payment', None)
            event = cls.home_purchase(
                decision.year,
                person,
                name,
                purchase_price,
                down_payment,
                **payload,
            )
        elif decision.event_name == LifeEventName.GO_TO_COLLEGE:
            person = _payload_value(decision, 'person')
            annual_cost = _payload_value(decision, 'annual_cost')
            payload.pop('person', None)
            payload.pop('annual_cost', None)
            event = cls.go_to_college(decision.year, person, annual_cost, **payload)
        else:
            raise ValueError(f"Unsupported financial decision event: {decision.name}")

        event.description = decision.description or event.description
        event.decision_event = decision
        return event

    @classmethod
    def marriage(cls, year: int, person, spouse, description: Optional[str] = None):
        """Create an adaptive marriage event."""
        return cls(year, LifeEventName.MARRIAGE, person.get_married, spouse, description=description)

    @classmethod
    def divorce(cls, year: int, person, spouse=None, description: Optional[str] = None):
        """Create an adaptive divorce event."""
        return cls(year, LifeEventName.DIVORCE, _apply_divorce, person, spouse, description=description)

    @classmethod
    def child_birth_or_adoption(
        cls,
        year: int,
        person,
        name: str,
        description: Optional[str] = None,
        **child_kwargs,
    ):
        """Create an adaptive child birth/adoption event.

        ``birth_year`` and ``adoption_year`` can be passed in ``child_kwargs``.
        If omitted, the child is treated as born in the event year.
        """
        return cls(
            year,
            LifeEventName.CHILD_BIRTH_OR_ADOPTION,
            _apply_child_birth_or_adoption,
            person,
            name,
            year,
            child_kwargs,
            description=description,
        )

    @classmethod
    def home_purchase(
        cls,
        year: int,
        person,
        name: str,
        purchase_price: float,
        down_payment: float,
        value_yearly_increase: float = 0.0,
        mortgage=None,
        expenses=None,
        mortgage_years: int = 30,
        mortgage_rate: float = 0.0,
        mortgage_type: str = 'fixed_rate',
        property_tax_percent: float = 0.0,
        home_insurance_percent: float = 0.0,
        maintenance_amount: float = 0.0,
        maintenance_increase: Optional[float] = None,
        improvement_amount: float = 0.0,
        improvement_increase: Optional[float] = None,
        hoa_amount: float = 0.0,
        hoa_increase: Optional[float] = None,
        stop_renting: bool = True,
        closing_costs: float = 0.0,
        description: Optional[str] = None,
    ):
        """Create an adaptive home purchase event.

        The event registers a ``Home``, adds down payment and closing costs to
        same-year one-time spending, and optionally stops apartment rent.
        """
        home_kwargs = {
            'name': name,
            'purchase_price': purchase_price,
            'down_payment': down_payment,
            'value_yearly_increase': value_yearly_increase,
            'mortgage': mortgage,
            'expenses': expenses,
            'mortgage_years': mortgage_years,
            'mortgage_rate': mortgage_rate,
            'mortgage_type': mortgage_type,
            'property_tax_percent': property_tax_percent,
            'home_insurance_percent': home_insurance_percent,
            'maintenance_amount': maintenance_amount,
            'maintenance_increase': maintenance_increase,
            'improvement_amount': improvement_amount,
            'improvement_increase': improvement_increase,
            'hoa_amount': hoa_amount,
            'hoa_increase': hoa_increase,
            'stop_renting': stop_renting,
            'closing_costs': closing_costs,
        }
        return cls(
            year,
            LifeEventName.HOME_PURCHASE,
            _apply_home_purchase,
            person,
            year,
            home_kwargs,
            description=description,
        )

    @classmethod
    def go_to_college(
        cls,
        year: int,
        person,
        annual_cost: float,
        years: int = 4,
        cost_yearly_increase: float = 0.0,
        defer_income: Optional[bool] = None,
        during_college_salary: float = 0.0,
        post_college_salary: Optional[float] = None,
        salary_yearly_increase: Optional[float] = None,
        yearly_bonus: Optional[float] = None,
        job_company: Optional[str] = None,
        job_role: Optional[str] = None,
        finance_with_student_loan: bool = False,
        loan_interest_rate: float = 6.5,
        loan_term_years: int = 10,
        loan_type: str = 'federal_unsubsidized',
        school_name: str = 'College',
        description: Optional[str] = None,
    ):
        """Create an adaptive college-attendance event.

        The event adds annual education costs for ``years`` years. When
        ``post_college_salary`` is supplied, existing job income is paused
        during school by default, then the first job's salary is updated at
        graduation. If no job exists, a post-college job is created.

        When ``finance_with_student_loan`` is true, the annual costs are
        borrowed onto a StudentLoan instead of paid from cash. The loan is in
        deferment during school (interest accrues and capitalizes except for
        federal subsidized loans) and repayment is amortized over
        ``loan_term_years`` starting the year after graduation.
        """
        if defer_income is None:
            defer_income = post_college_salary is not None
        college_kwargs = {
            'annual_cost': annual_cost,
            'years': years,
            'cost_yearly_increase': cost_yearly_increase,
            'defer_income': defer_income,
            'during_college_salary': during_college_salary,
            'post_college_salary': post_college_salary,
            'salary_yearly_increase': salary_yearly_increase,
            'yearly_bonus': yearly_bonus,
            'job_company': job_company,
            'job_role': job_role,
            'finance_with_student_loan': finance_with_student_loan,
            'loan_interest_rate': loan_interest_rate,
            'loan_term_years': loan_term_years,
            'loan_type': loan_type,
            'school_name': school_name,
        }
        return cls(
            year,
            LifeEventName.GO_TO_COLLEGE,
            _apply_go_to_college,
            person,
            year,
            college_kwargs,
            description=description,
            run_in_decision_step=True,
        )

    def eval_event(self, year):
        # Fire at the first simulated year at or after the scheduled year, so
        # events dated in the baseline year (or earlier) still take effect.
        if year >= self.year:
            self.result = self.event(*self.event_args)
            return True
        return False
