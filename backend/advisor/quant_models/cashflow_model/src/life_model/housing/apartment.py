# Copyright 2022 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

import html
from typing import Optional
from ..people.person import Person
from ..model import LifeModelAgent
from ..config.config_manager import config


class Apartment(LifeModelAgent):
    def __init__(self, person: Person, name: str, monthly_rent: float, yearly_increase: Optional[float] = None):
        """Apartment

        Args:
            person (Person): Primary resident or person paying apartment rent.
            name (string): Apartment Name.
            monthly_rent (float): Amount of rent charged monthly.
            yearly_increase (float, optional): Percentage of rent increase every
                year. If None, the configured inflation rate is used.
        """
        super().__init__(person.model)
        self.name = name
        self.monthly_rent = monthly_rent
        self.yearly_increase = (
            config.financial.get_inflation_rate()
            if yearly_increase is None else yearly_increase
        )

        # Register with the model registry
        self.model.registries.apartments.register(person, self)

    @property
    def yearly_rent(self):
        return self.monthly_rent * 12

    def _repr_html_(self):
        return f"{html.escape(self.name)}, monthly rent ${self.monthly_rent:,}"

    def step(self):
        self.monthly_rent += self.monthly_rent * (self.yearly_increase / 100)
