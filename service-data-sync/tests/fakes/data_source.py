from __future__ import annotations

from datetime import UTC, datetime

from service_data_sync.application.ports.data_source import ProviderBatch, SourceRequest


class FakeDataSource:
    provider_id = "fake"

    def capabilities(self) -> frozenset[str]:
        return frozenset({"health"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        if request.capability not in self.capabilities():
            raise ValueError(request.capability)
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=b"fake",
            observed_at=datetime.now(UTC),
        )
