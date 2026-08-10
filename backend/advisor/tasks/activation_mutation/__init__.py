"""Activation Mutation Framework — AI-driven fact derivation + deterministic validation.

When a client activates a policy, this framework:
1. Uses an LLM to derive what household model changes the activation implies
2. Validates the derived mutations against deterministic financial invariants
3. Returns clean, validated fact mutations ready for the Knowledge Updater
"""

from .deriver import ActivationFactDeriver
from .mutator import ActivationMutator
from domain.activation.validator import validate_mutations, ValidationError

__all__ = [
    "ActivationFactDeriver",
    "ActivationMutator",
    "validate_mutations",
    "ValidationError",
]
