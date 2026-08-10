"""Narrow application interface over durable Companion turn receipts."""

from api.persistence.companion_turn_runs import (
    accept_companion_turn,
    get_companion_turn,
    list_companion_turns,
    update_companion_turn,
)


class CompanionTurnRunService:
    accept = staticmethod(accept_companion_turn)
    get = staticmethod(get_companion_turn)
    list = staticmethod(list_companion_turns)
    update = staticmethod(update_companion_turn)
