"""基础设施客户端与组合根的单元测试。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from redis.exceptions import RedisError
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError

from service_data_sync.bootstrap import container as container_module
from service_data_sync.bootstrap.errors import DependencyUnavailable
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.database import connection as database_module
from service_data_sync.infrastructure.messaging import redis_client as redis_module
from service_data_sync.infrastructure.object_storage import client as storage_module


def test_database_client_builds_pings_and_closes(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """构建 PostgreSQL 封装，并转换驱动探测失败。"""
    engine = MagicMock()
    # 返回 fixture 引擎，使封装行为不依赖真实 PostgreSQL。
    monkeypatch.setattr(database_module, "create_engine", lambda *_args, **_kwargs: engine)
    client = database_module.DatabaseClient.from_settings(load_settings())

    client.ping()
    client.close()

    engine.connect.assert_called_once()
    engine.dispose.assert_called_once()

    engine.connect.side_effect = SQLAlchemyError("offline")
    with pytest.raises(DependencyUnavailable, match="postgres unavailable"):
        client.ping()


def test_database_client_creates_short_lived_transactional_sessions() -> None:
    """Session 必须关闭 autoflush，并在事务结束后可安全地关闭连接。"""
    client = database_module.DatabaseClient(create_engine("sqlite+pysqlite://"))

    with client.transaction() as session:
        assert session.autoflush is False
        assert session.execute(select(1)).scalar_one() == 1

    client.close()


def test_redis_client_builds_pings_and_closes(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """构建 Redis 封装，并转换驱动探测失败。"""
    backend = MagicMock()
    # 返回 fixture 后端，使封装行为不依赖真实 Redis。
    monkeypatch.setattr(redis_module.redis.Redis, "from_url", lambda *_args, **_kwargs: backend)
    client = redis_module.RedisClient.from_settings(load_settings())

    client.ping()
    client.close()

    backend.close.assert_called_once()
    backend.ping.side_effect = RedisError("offline")
    with pytest.raises(DependencyUnavailable, match="redis unavailable"):
        client.ping()


def test_object_storage_client_builds_pings_and_closes(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """构建 S3 封装，并转换对象存储探测失败。"""
    backend = MagicMock()
    # 返回 fixture 后端，使封装行为不依赖真实对象存储。
    monkeypatch.setattr(storage_module.boto3, "client", lambda *_args, **_kwargs: backend)
    client = storage_module.ObjectStorageClient.from_settings(load_settings())

    client.ping()
    client.close()

    backend.head_bucket.assert_called_once_with(Bucket="quant-data-sync-test")
    backend.close.assert_called_once()
    backend.head_bucket.side_effect = ClientError({"Error": {"Code": "403"}}, "HeadBucket")
    with pytest.raises(DependencyUnavailable, match="s3 unavailable"):
        client.ping()


def test_container_composes_dependencies_without_registering_provider_adapters(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仅组合基础设施客户端，并刻意保持数据源注册表为空。"""
    database = MagicMock()
    broker = MagicMock()
    object_storage = MagicMock()
    monkeypatch.setattr(
        container_module.DatabaseClient,
        "from_settings",
        # 注入 fixture 数据库，避免打开外部连接。
        classmethod(lambda _cls, _settings: database),
    )
    monkeypatch.setattr(
        container_module.RedisClient,
        "from_settings",
        # 注入 fixture broker，避免打开外部连接。
        classmethod(lambda _cls, _settings: broker),
    )
    monkeypatch.setattr(
        container_module.ObjectStorageClient,
        "from_settings",
        # 注入 fixture 对象存储，避免打开外部连接。
        classmethod(lambda _cls, _settings: object_storage),
    )

    container = container_module.build_container(load_settings())
    container.close()

    assert container.source_registry.provider_ids() == frozenset()
    database.close.assert_called_once()
    broker.close.assert_called_once()
    object_storage.close.assert_called_once()


def test_container_registers_sector_adapter_only_when_both_source_policies_are_enabled(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AKShare 总开关和板块开关同时开启时才暴露板块三周期能力。"""
    database = MagicMock()
    broker = MagicMock()
    object_storage = MagicMock()
    monkeypatch.setenv("DATA_SYNC_AKSHARE_ENABLED", "true")
    monkeypatch.setenv("DATA_SYNC_SECTOR_ENABLED", "true")
    # 组合测试只验证注册策略，所有基础设施客户端均替换为内存替身。
    monkeypatch.setattr(
        container_module.DatabaseClient,
        "from_settings",
        classmethod(lambda _cls, _settings: database),
    )
    monkeypatch.setattr(
        container_module.RedisClient,
        "from_settings",
        classmethod(lambda _cls, _settings: broker),
    )
    monkeypatch.setattr(
        container_module.ObjectStorageClient,
        "from_settings",
        classmethod(lambda _cls, _settings: object_storage),
    )

    container = container_module.build_container(load_settings())
    container.close()

    assert container.source_registry.provider_ids() == {
        "akshare-eastmoney-equity-catalog",
        "akshare-eastmoney-sector",
        "akshare-official-exchange-equity-lifecycle",
        "akshare-tencent",
    }


def test_registry_adds_equity_market_sources_only_with_explicit_capability_flag(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """个股周/月、因子、事件和概况 adapter 只在两级开关同时开启时注册。"""
    monkeypatch.setenv("DATA_SYNC_AKSHARE_ENABLED", "true")
    monkeypatch.setenv("DATA_SYNC_EQUITY_MARKET_ENABLED", "true")

    registry = container_module.build_source_registry(load_settings())

    assert {provider.provider_id for provider in registry.for_capability("equity.bar.1w.raw")} == {
        "akshare-eastmoney-equity-period"
    }
    assert {
        provider.provider_id for provider in registry.for_capability("equity.adjustment_factor")
    } == {"akshare-sina-adjustment-factor"}
    assert {
        provider.provider_id for provider in registry.for_capability("equity.corporate_action")
    } == {"akshare-eastmoney-corporate-action"}
    assert {provider.provider_id for provider in registry.for_capability("equity.profile")} == {
        "akshare-cninfo-company-profile"
    }
