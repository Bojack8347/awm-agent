"""Injected repository adapter for deterministic fact-confirmation tools."""

from api.persistence.fact_confirmations import (
    bind_confirmation_prompt,
    create_confirmation_set,
    get_confirmation_set,
    get_latest_bound_confirmation_set,
    resolve_confirmation_set,
)


class FactConfirmationRepository:
    create = staticmethod(create_confirmation_set)
    get = staticmethod(get_confirmation_set)
    latest_bound = staticmethod(get_latest_bound_confirmation_set)
    bind = staticmethod(bind_confirmation_prompt)
    resolve = staticmethod(resolve_confirmation_set)
