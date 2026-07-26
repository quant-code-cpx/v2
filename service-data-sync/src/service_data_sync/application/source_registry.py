"""组合根使用的数据源注册表，按能力选择获准适配器。"""

from __future__ import annotations

from service_data_sync.application.ports.data_source import DataSourcePort


class DuplicateProviderError(ValueError):
    """注册同名数据源实现时抛出，避免运行时来源归属不明确。"""

    pass


class UnknownProviderError(KeyError):
    """按标识查询未注册数据源时抛出。"""

    pass


class SourceRegistry:
    """仅由组合根填充的内存数据源注册表。"""

    def __init__(self) -> None:
        """初始化由组合根独占管理的空注册表。"""
        self._providers: dict[str, DataSourcePort] = {}

    def register(self, provider: DataSourcePort) -> None:
        """注册一个标识唯一的数据源实现。"""
        provider_id = provider.provider_id.strip()
        if not provider_id:
            raise ValueError("provider_id must not be blank")
        if provider_id in self._providers:
            raise DuplicateProviderError(provider_id)
        self._providers[provider_id] = provider

    def get(self, provider_id: str) -> DataSourcePort:
        """返回已注册数据源；不存在时转换为领域错误。"""
        try:
            return self._providers[provider_id]
        except KeyError as error:
            raise UnknownProviderError(provider_id) from error

    def provider_ids(self) -> frozenset[str]:
        """返回已注册数据源标识的不可变快照。"""
        return frozenset(self._providers)

    def for_capability(self, capability: str) -> tuple[DataSourcePort, ...]:
        """按稳定标识顺序返回声明指定中立能力的数据源。"""
        return tuple(
            provider
            for _, provider in sorted(self._providers.items())
            if capability in provider.capabilities()
        )
