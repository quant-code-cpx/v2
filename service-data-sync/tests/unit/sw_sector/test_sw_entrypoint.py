"""申万同步 CLI 的历史 replay 边界、输出与资源释放测试。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from service_data_sync.application.ports.sw_sector import (
    SwPublishedCapability,
    SwPublishResult,
)
from service_data_sync.application.sector.sw_snapshot_sync import SwSnapshotSyncResult
from service_data_sync.entrypoints import sw_sector

_DATE = date(2026, 7, 28)


class FakeContainer:
    """提供 CLI 所需基础设施形状并记录关闭状态。"""

    def __init__(self) -> None:
        """初始化不连接真实 PostgreSQL 或对象存储的依赖。"""
        self.database = object()
        self.object_storage = object()
        self.closed = False

    def close(self) -> None:
        """记录 CLI `finally` 块已经释放组合根。"""
        self.closed = True


class FakeSwSyncService:
    """只允许历史 replay，并返回确定性的双发布摘要。"""

    def replay(self, *, snapshot_date: date) -> SwSnapshotSyncResult:
        """断言精确历史日期并返回已重放结果。"""
        assert snapshot_date == _DATE
        return SwSnapshotSyncResult(publications=_publications(), replayed=True)


def test_sw_cli_replays_exact_date_and_closes_container(monkeypatch, capsys) -> None:
    """历史模式必须使用 replay-only 组合并输出两个独立 dataVersion。"""
    container = FakeContainer()
    captured: dict[str, object] = {}

    # 以下替身只隔离组合根与日志，不改变 CLI 参数和结果投影。
    monkeypatch.setattr(sw_sector, "load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(sw_sector, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sw_sector, "build_container", lambda _settings: container)

    def build_service(
        _settings: object,
        *,
        database: object,
        object_storage: object,
        replay_only: bool,
    ) -> FakeSwSyncService:
        """捕获 CLI 组合参数并返回无外部 I/O 的 replay 服务。"""
        captured.update(
            {
                "database": database,
                "objectStorage": object_storage,
                "replayOnly": replay_only,
            }
        )
        return FakeSwSyncService()

    monkeypatch.setattr(sw_sector, "build_sw_sync_service", build_service)

    assert sw_sector.main(["--snapshot-date", _DATE.isoformat(), "--replay-raw"]) == 0

    rendered = json.loads(capsys.readouterr().out)
    assert rendered["replayed"] is True
    assert rendered["taxonomy"]["dataVersion"] != rendered["valuation"]["dataVersion"]
    assert captured == {
        "database": container.database,
        "objectStorage": container.object_storage,
        "replayOnly": True,
    }
    assert container.closed is True


def test_sw_cli_rejects_historical_live_fetch() -> None:
    """非当天日期没有已归档 replay 标志时必须在连接基础设施前拒绝。"""
    with pytest.raises(SystemExit) as captured:
        sw_sector.main(["--snapshot-date", "2000-01-01"])

    assert captured.value.code == 2


def _publications() -> SwPublishResult:
    """构造 taxonomy 与估值两个独立消费者发布。"""
    published_at = datetime(2026, 7, 28, 10, tzinfo=UTC)
    taxonomy = SwPublishedCapability(
        capability="sector.sw.taxonomy",
        data_version=UUID("10000000-0000-4000-8000-000000000001"),
        snapshot_date=_DATE,
        published_at=published_at,
        inserted_count=3,
        unchanged_count=0,
        row_count=3,
        content_sha256="a" * 64,
    )
    valuation = SwPublishedCapability(
        capability="sector.sw.valuation",
        data_version=UUID("10000000-0000-4000-8000-000000000002"),
        snapshot_date=_DATE,
        published_at=published_at,
        inserted_count=3,
        unchanged_count=0,
        row_count=3,
        content_sha256="b" * 64,
    )
    return SwPublishResult(taxonomy=taxonomy, valuation=valuation)
