"""Provider-neutral contract. This module must not depend on infrastructure."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ProviderErrorCode(StrEnum):
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION = "authentication"
    INVALID_REQUEST = "invalid_request"
    SCHEMA = "schema"


class ProviderError(RuntimeError):
    """A provider-neutral failure emitted by an adapter."""

    def __init__(self, code: ProviderErrorCode, message: str, *, retryable: bool) -> None:
        """Capture portable provider failure classification and retry policy."""
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class SourceRequest:
    """Minimal immutable request shape; data semantics are deferred."""

    capability: str
    parameters: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        """Reject requests that cannot name a usable provider capability."""
        if not self.capability.strip():
            raise ValueError("capability must not be blank")


@dataclass(frozen=True, slots=True)
class ProviderBatch:
    """Opaque adapter output. Normalization and persistence are future work."""

    provider_id: str
    capability: str
    payload: bytes
    observed_at: datetime
    content_type: str = "application/octet-stream"

    def __post_init__(self) -> None:
        """Ensure opaque adapter output retains provider, capability, and timezone provenance."""
        if not self.provider_id.strip():
            raise ValueError("provider_id must not be blank")
        if not self.capability.strip():
            raise ValueError("capability must not be blank")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must include a timezone")

    @classmethod
    def empty(cls, provider_id: str, capability: str) -> ProviderBatch:
        """Create timezone-aware empty provider result for a supported capability."""
        return cls(
            provider_id=provider_id,
            capability=capability,
            payload=b"",
            observed_at=datetime.now(UTC),
        )


@runtime_checkable
class DataSourcePort(Protocol):
    """Single permitted application-facing abstraction for external data sources."""

    provider_id: str

    def capabilities(self) -> frozenset[str]:
        """Return provider capabilities without making a network request."""
        ...

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """Fetch a provider batch or raise ProviderError."""
        ...
