"""Snapshot-backed deterministic provider adapter base classes."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar, Protocol
from urllib.parse import urlparse

from pydantic import ValidationError

from advisor.assumptions.contracts import (
    AssumptionArtifact,
    AssumptionEvidence,
    AssumptionStatus,
    PermittedUse,
    SourceClass,
)
from advisor.assumptions.providers.contracts import (
    AuthoritativeSourceSnapshot,
    ProviderAdapterError,
    ProviderCandidateBatch,
    ProviderErrorCode,
    ProviderRequest,
)


DEFAULT_SNAPSHOT_ROOT = Path(__file__).with_name("snapshots")
MAX_SNAPSHOT_BYTES = 1_000_000


class AuthoritativeProviderAdapter(Protocol):
    provider_id: str
    supported_variables: frozenset[str]

    def collect_candidates(
        self,
        request: ProviderRequest,
        *,
        retrieved_at: datetime | None = None,
    ) -> ProviderCandidateBatch: ...


class GovernmentSnapshotAdapter(ABC):
    """Load, validate, and normalize a reviewed government-source snapshot."""

    provider_id: ClassVar[str]
    publisher: ClassVar[str]
    allowed_hosts: ClassVar[frozenset[str]]
    supported_variables: ClassVar[frozenset[str]]

    def __init__(self, *, snapshot_root: Path | None = None) -> None:
        self.snapshot_root = snapshot_root or DEFAULT_SNAPSHOT_ROOT

    def collect_candidates(
        self,
        request: ProviderRequest,
        *,
        retrieved_at: datetime | None = None,
    ) -> ProviderCandidateBatch:
        retrieved_at = retrieved_at or datetime.now(timezone.utc)
        if retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")

        selected_variables = (
            request.variable_keys
            if request.variable_keys
            else tuple(sorted(self.supported_variables))
        )
        unsupported = set(selected_variables).difference(self.supported_variables)
        if unsupported:
            variable_key = sorted(unsupported)[0]
            raise ProviderAdapterError(
                ProviderErrorCode.VARIABLE_NOT_SUPPORTED,
                f"{self.provider_id} does not support {variable_key}",
                provider_id=self.provider_id,
                variable_key=variable_key,
                effective_year=request.effective_year,
            )

        raw = self._read_snapshot(request.effective_year)
        digest = hashlib.sha256(raw).hexdigest()
        document = self._parse_snapshot(raw, request.effective_year)
        self._validate_document(document, retrieved_at=retrieved_at)

        missing = set(selected_variables).difference(document.variables)
        if missing:
            variable_key = sorted(missing)[0]
            raise ProviderAdapterError(
                ProviderErrorCode.SNAPSHOT_INVALID,
                f"snapshot does not contain {variable_key}",
                provider_id=self.provider_id,
                variable_key=variable_key,
                effective_year=request.effective_year,
            )

        effective_from = datetime(
            request.effective_year, 1, 1, tzinfo=timezone.utc
        )
        effective_to = datetime(
            request.effective_year + 1, 1, 1, tzinfo=timezone.utc
        ) - timedelta(microseconds=1)
        artifacts: list[AssumptionArtifact] = []
        for variable_key in selected_variables:
            snapshot_variable = document.variables[variable_key]
            self.validate_value(variable_key, snapshot_variable.value)
            evidence = tuple(
                AssumptionEvidence(
                    source_id=(
                        f"{self.provider_id}:"
                        f"{document.sources[source_ref].document_id}"
                    ),
                    source_type="government_primary_snapshot",
                    publisher=self.publisher,
                    title=document.sources[source_ref].title,
                    url=document.sources[source_ref].url,
                    published_at=document.sources[source_ref].published_at,
                    retrieved_at=retrieved_at,
                    content_hash=digest,
                )
                for source_ref in snapshot_variable.evidence_refs
            )
            artifacts.append(
                AssumptionArtifact(
                    artifact_id=(
                        f"candidate:{self.provider_id}:{request.effective_year}:"
                        f"{variable_key}:{digest[:16]}"
                    ),
                    variable_key=variable_key,
                    source_class=SourceClass.PUBLIC_AUTHORITATIVE,
                    value=snapshot_variable.value,
                    unit=snapshot_variable.unit,
                    jurisdiction=document.jurisdiction,
                    effective_from=effective_from,
                    effective_to=effective_to,
                    status=AssumptionStatus.CANDIDATE,
                    permitted_uses=(PermittedUse.REPORTING,),
                    evidence=evidence,
                    created_at=retrieved_at,
                )
            )

        return ProviderCandidateBatch(
            provider_id=self.provider_id,
            effective_year=request.effective_year,
            snapshot_sha256=digest,
            retrieved_at=retrieved_at,
            artifacts=tuple(artifacts),
        )

    def _snapshot_path(self, effective_year: int) -> Path:
        return self.snapshot_root / self.provider_id / f"{effective_year}.json"

    def _read_snapshot(self, effective_year: int) -> bytes:
        path = self._snapshot_path(effective_year)
        try:
            size = path.stat().st_size
        except FileNotFoundError as exc:
            raise ProviderAdapterError(
                ProviderErrorCode.SNAPSHOT_NOT_FOUND,
                f"no reviewed {self.provider_id} snapshot for {effective_year}",
                provider_id=self.provider_id,
                effective_year=effective_year,
            ) from exc
        if size > MAX_SNAPSHOT_BYTES:
            raise ProviderAdapterError(
                ProviderErrorCode.SNAPSHOT_TOO_LARGE,
                f"{self.provider_id} snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes",
                provider_id=self.provider_id,
                effective_year=effective_year,
            )
        return path.read_bytes()

    def _parse_snapshot(
        self, raw: bytes, effective_year: int
    ) -> AuthoritativeSourceSnapshot:
        try:
            decoded = raw.decode("utf-8")
            payload = json.loads(decoded)
            document = AuthoritativeSourceSnapshot.model_validate(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
            raise ProviderAdapterError(
                ProviderErrorCode.SNAPSHOT_INVALID,
                f"invalid {self.provider_id} snapshot for {effective_year}",
                provider_id=self.provider_id,
                effective_year=effective_year,
            ) from exc
        if document.provider_id != self.provider_id:
            raise ProviderAdapterError(
                ProviderErrorCode.SNAPSHOT_INVALID,
                "snapshot provider does not match adapter",
                provider_id=self.provider_id,
                effective_year=effective_year,
            )
        if document.effective_year != effective_year:
            raise ProviderAdapterError(
                ProviderErrorCode.SNAPSHOT_INVALID,
                "snapshot effective year does not match request",
                provider_id=self.provider_id,
                effective_year=effective_year,
            )
        return document

    def _validate_document(
        self,
        document: AuthoritativeSourceSnapshot,
        *,
        retrieved_at: datetime,
    ) -> None:
        if document.reviewed_at > retrieved_at:
            raise ProviderAdapterError(
                ProviderErrorCode.SNAPSHOT_INVALID,
                f"{self.provider_id} snapshot review is in the future",
                provider_id=self.provider_id,
                effective_year=document.effective_year,
            )
        for source in document.sources.values():
            parsed = urlparse(source.url)
            hostname = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or hostname not in self.allowed_hosts:
                raise ProviderAdapterError(
                    ProviderErrorCode.SOURCE_NOT_ALLOWED,
                    f"{self.provider_id} snapshot contains a non-allowlisted source",
                    provider_id=self.provider_id,
                    effective_year=document.effective_year,
                )
            if source.published_at > retrieved_at:
                raise ProviderAdapterError(
                    ProviderErrorCode.SNAPSHOT_INVALID,
                    f"{self.provider_id} source publication is in the future",
                    provider_id=self.provider_id,
                    effective_year=document.effective_year,
                )

    @abstractmethod
    def validate_value(self, variable_key: str, value: Any) -> None:
        """Validate one provider-specific normalized value."""

    def invalid_value(
        self,
        variable_key: str,
        effective_year: int | None = None,
    ) -> ProviderAdapterError:
        return ProviderAdapterError(
            ProviderErrorCode.VALUE_INVALID,
            f"invalid normalized value for {variable_key}",
            provider_id=self.provider_id,
            variable_key=variable_key,
            effective_year=effective_year,
        )
