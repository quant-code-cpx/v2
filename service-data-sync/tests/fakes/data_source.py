"""供端口契约测试使用的确定性数据源替身。"""

from __future__ import annotations

from datetime import UTC, datetime

from service_data_sync.application.ports.data_source import ProviderBatch, SourceRequest


class FakeDataSource:
    """以最小能力实现数据源端口，避免测试依赖外部网络。"""

    provider_id = "fake"

    def capabilities(self) -> frozenset[str]:
        """仅暴露供契约测试使用的健康检查能力。"""
        return frozenset({"health"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """返回确定性批次；请求越出替身契约时拒绝。"""
        if request.capability not in self.capabilities():
            raise ValueError(request.capability)
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=b"fake",
            observed_at=datetime.now(UTC),
        )
