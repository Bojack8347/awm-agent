"""Activation Mutator — single entry point for deriving AND validating mutations.

Consolidates ActivationFactDeriver (LLM) and validate_mutations (deterministic)
into one call so the Flask route can never skip validation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .deriver import ActivationFactDeriver
from domain.activation.validator import validate_mutations, ValidationError


class ActivationMutator:
    """Derives household model mutations from policy activation and validates them.

    Single entry point: derive_and_validate() runs the LLM derivation followed
    by deterministic validation. If validation fails, ValidationError is raised
    before any mutations are returned — the caller cannot accidentally skip it.
    """

    def __init__(
        self,
        llm_api_key: str = "",
        model_chain: Optional[List[Tuple[str, str]]] = None,
        prompts_dir: Optional[Path] = None,
        llm_timeout_ms: Optional[int] = None,
        temperature: Optional[float] = None,
        reasoning_effort: Optional[str] = None,
    ):
        self._deriver = ActivationFactDeriver(
            llm_api_key=llm_api_key,
            model_chain=model_chain,
            prompts_dir=prompts_dir,
            llm_timeout_ms=llm_timeout_ms,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )

    def derive_and_validate(
        self,
        journey_id: str,
        journey_type: str,
        solution: Dict[str, Any],
        current_facts: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Derive mutations via LLM, then validate against deterministic rules.

        Returns:
            {
                "mutations": [...],       # validated fact mutations
                "model_used": str,
                "provider_used": str,
            }

        Raises:
            ValidationError: if derived mutations fail deterministic validation.
        """
        # Step 1: LLM derives mutations
        derive_result = self._deriver.derive(
            journey_id, journey_type, solution, current_facts,
        )
        mutations = derive_result.get("mutations", [])

        # Step 2: Deterministic validation (cannot be skipped)
        if mutations:
            validate_mutations(mutations, current_facts)

        return derive_result
