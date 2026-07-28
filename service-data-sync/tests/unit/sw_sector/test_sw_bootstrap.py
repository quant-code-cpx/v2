"""申万来源开关、组合工厂与 replay-only 防外联测试。"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from service_data_sync.application.ports.data_source import (
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.sector.sw_snapshot_sync import SwSnapshotSyncService
from service_data_sync.bootstrap import sw_sector
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.object_storage.client import ObjectStorageClient


def test_bootstrap_builds_enabled_source_and_both_service_modes() -> None:
    """开关启用后应构造固定 adapter，并支持在线与 replay-only 两种组合。"""
    settings = _settings(enabled=True)
    database = cast(DatabaseClient, object())
    object_storage = cast(ObjectStorageClient, object())

    source = sw_sector.build_sw_source(settings)
    live = sw_sector.build_sw_sync_service(
        settings,
        database=database,
        object_storage=object_storage,
    )
    replay = sw_sector.build_sw_sync_service(
        settings,
        database=database,
        object_storage=object_storage,
        replay_only=True,
    )

    assert source.provider_id == "akshare-legulegu-sw-industry"
    assert source.capabilities() == frozenset({"sector.sw.snapshot.raw"})
    assert isinstance(live, SwSnapshotSyncService)
    assert isinstance(replay, SwSnapshotSyncService)


def test_replay_only_source_declares_nothing_and_rejects_fetch() -> None:
    """replay-only 占位来源不得声明能力或意外访问 provider。"""
    source = sw_sector._ReplayOnlySource()

    assert source.capabilities() == frozenset()
    with pytest.raises(ProviderError) as captured:
        asyncio.run(
            source.fetch(
                SourceRequest(
                    capability="sector.sw.snapshot.raw",
                    parameters=(("snapshotDate", "2026-07-28"),),
                )
            )
        )

    assert captured.value.code == ProviderErrorCode.INVALID_REQUEST
    assert captured.value.retryable is False


def _settings(*, enabled: bool) -> Settings:
    """构造申万组合工厂读取的最小设置。"""
    return Settings.model_construct(
        akshare_enabled=enabled,
        sector_enabled=enabled,
        sw_sector_enabled=enabled,
        akshare_request_timeout_seconds=30,
    )
