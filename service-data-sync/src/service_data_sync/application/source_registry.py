from __future__ import annotations

from service_data_sync.application.ports.data_source import DataSourcePort


class DuplicateProviderError(ValueError):
    pass


class UnknownProviderError(KeyError):
    pass


class SourceRegistry:
    """In-memory registry populated only by the bootstrap composition root."""

    def __init__(self) -> None:
        """Initialize empty registry owned by bootstrap composition root."""
        self._providers: dict[str, DataSourcePort] = {}

    def register(self, provider: DataSourcePort) -> None:
        """Register one uniquely named provider implementation."""
        provider_id = provider.provider_id.strip()
        if not provider_id:
            raise ValueError("provider_id must not be blank")
        if provider_id in self._providers:
            raise DuplicateProviderError(provider_id)
        self._providers[provider_id] = provider

    def get(self, provider_id: str) -> DataSourcePort:
        """Return registered provider or domain-specific unknown-provider error."""
        try:
            return self._providers[provider_id]
        except KeyError as error:
            raise UnknownProviderError(provider_id) from error

    def provider_ids(self) -> frozenset[str]:
        """Expose immutable snapshot of registered provider identifiers."""
        return frozenset(self._providers)
