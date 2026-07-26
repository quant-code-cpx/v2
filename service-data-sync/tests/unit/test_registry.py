"""数据源端口值对象与注册表的单元测试。"""

from __future__ import annotations

import asyncio

import pytest
from tests.fakes.data_source import FakeDataSource

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.source_registry import (
    DuplicateProviderError,
    SourceRegistry,
    UnknownProviderError,
)


def test_fake_adapter_satisfies_port_contract() -> None:
    """确认数据源替身始终在结构上兼容中立端口。"""
    fake = FakeDataSource()

    assert isinstance(fake, DataSourcePort)
    batch = asyncio.run(fake.fetch(SourceRequest(capability="health")))

    assert batch.provider_id == "fake"
    assert batch.payload == b"fake"


def test_registry_keeps_provider_implementation_replaceable() -> None:
    """通过标识解析已注册数据源，不暴露其具体类型。"""
    registry = SourceRegistry()
    fake = FakeDataSource()
    registry.register(fake)

    assert registry.provider_ids() == frozenset({"fake"})
    assert registry.get("fake") is fake


def test_registry_rejects_duplicate_or_unknown_provider() -> None:
    """拒绝歧义注册和注册表中不存在的查询。"""
    registry = SourceRegistry()
    registry.register(FakeDataSource())

    with pytest.raises(DuplicateProviderError):
        registry.register(FakeDataSource())
    with pytest.raises(UnknownProviderError):
        registry.get("missing")


def test_port_value_objects_reject_invalid_inputs() -> None:
    """拒绝格式错误的数据源无关请求和批次值。"""
    with pytest.raises(ValueError, match="capability"):
        SourceRequest(capability=" ")
    with pytest.raises(ValueError, match="provider_id"):
        ProviderBatch.empty(" ", "health")
    with pytest.raises(ValueError, match="capability"):
        ProviderBatch.empty("fake", " ")

    error = ProviderError(ProviderErrorCode.RATE_LIMITED, "retry later", retryable=True)

    assert error.code is ProviderErrorCode.RATE_LIMITED
    assert error.retryable is True


def test_registry_rejects_blank_provider_identifier() -> None:
    """拒绝规范化后为空的数据源标识。"""
    fake = FakeDataSource()
    fake.provider_id = " "

    with pytest.raises(ValueError, match="provider_id"):
        SourceRegistry().register(fake)
