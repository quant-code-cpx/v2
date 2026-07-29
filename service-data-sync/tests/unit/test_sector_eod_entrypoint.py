"""板块 EOD 手工 CLI 的策略开关、组合根和输出测试。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

from service_data_sync.application.ports.sector_eod import SectorEodExecutionMode
from service_data_sync.application.sector.eod_snapshot_sync import SectorEodSnapshotSyncResult
from service_data_sync.domain.sector import SectorEodFinality, SectorEodSnapshot, SectorScheme
from service_data_sync.entrypoints import sector_eod


class FakeRegistry:
    """为 CLI 返回一个唯一获准 EOD 来源，避免测试访问网络。"""

    def for_capability(self, capability: str) -> tuple[object, ...]:
        """断言 CLI 选择 EOD 批量能力而非 K 线或逐板块能力。"""
        assert capability == "sector.quote.eod.snapshot.raw"
        return (object(),)


class FakeContainer:
    """提供 CLI 组合根依赖并记录 finally 资源关闭。"""

    def __init__(self) -> None:
        """初始化无外部连接的数据库、对象存储和来源替身。"""
        self.database = object()
        self.object_storage = object()
        self.source_registry = FakeRegistry()
        self.trading_calendar = object()
        self.closed = False

    def close(self) -> None:
        """记录 CLI 在成功或失败后释放容器资源。"""
        self.closed = True


class FakeSyncService:
    """返回固定 EOD 发布摘要，验证 CLI 只组合中立依赖。"""

    def __init__(self, **kwargs: object) -> None:
        """验证 CLI 构造同步服务时没有泄漏 SDK 或数据库细节。"""
        assert set(kwargs) == {"source", "repository", "raw_payload_store", "trading_calendar"}

    async def sync(self, **kwargs: object) -> SectorEodSnapshotSyncResult:
        """断言显式交易日与 16:15 策略截点后返回不可变发布版本。"""
        assert kwargs["scheme"] is SectorScheme.EASTMONEY_INDUSTRY
        assert kwargs["trade_date"] == date(2026, 7, 27)
        cutoff = kwargs["source_cutoff_at"]
        assert isinstance(cutoff, datetime)
        assert cutoff.hour == 16 and cutoff.minute == 15
        assert kwargs["execution_mode"] is SectorEodExecutionMode.SHADOW
        return SectorEodSnapshotSyncResult(
            snapshot=SectorEodSnapshot(
                snapshot_id=uuid4(),
                data_version=uuid4(),
                scheme=SectorScheme.EASTMONEY_INDUSTRY,
                trade_date=date(2026, 7, 27),
                source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
                observed_at=datetime(2026, 7, 27, 8, 20, tzinfo=UTC),
                finality=SectorEodFinality.POST_CLOSE_OBSERVATION,
                quality_status="passed",
                published_at=None,
            ),
            inserted=True,
            execution_mode=SectorEodExecutionMode.SHADOW,
        )


def test_cli_requires_enabled_policy_and_closes_container(monkeypatch, capsys) -> None:
    """显式开关开启时 CLI 应打印机器可读摘要，并在 finally 关闭组合根。"""
    container = FakeContainer()

    # 以下替身隔离配置、日志和组合根，避免 CLI 单测连接真实基础设施。
    def fake_load_settings() -> SimpleNamespace:
        """返回显式开启 EOD 策略的最小配置。"""
        return SimpleNamespace(sector_eod_enabled=True, sector_eod_publish_enabled=False)

    def fake_configure_logging(*_args: object, **_kwargs: object) -> None:
        """忽略测试中的日志初始化，避免修改全局日志处理器。"""

    def fake_build_container(_settings: object) -> FakeContainer:
        """返回共享容器替身，供测试断言 finally 是否关闭。"""
        return container

    def fake_repository(_database: object) -> object:
        """返回不连接 PostgreSQL 的仓储占位对象。"""
        return object()

    def fake_raw_store(_storage: object) -> object:
        """返回不连接对象存储的原始证据端口占位对象。"""
        return object()

    monkeypatch.setattr(sector_eod, "load_settings", fake_load_settings)
    monkeypatch.setattr(sector_eod, "configure_logging", fake_configure_logging)
    monkeypatch.setattr(sector_eod, "build_container", fake_build_container)
    monkeypatch.setattr(sector_eod, "SqlAlchemySectorEodRepository", fake_repository)
    monkeypatch.setattr(sector_eod, "S3RawPayloadStore", fake_raw_store)
    # 入口组合测试不触及对象存储；留证语义由原始载荷存储的专用单元测试覆盖。
    monkeypatch.setattr(
        sector_eod, "retain_failure_evidence", lambda _store, operation: operation()
    )
    monkeypatch.setattr(sector_eod, "SectorEodSnapshotSyncService", FakeSyncService)

    assert sector_eod.main(["--scheme", "eastmoney.industry", "--trade-date", "2026-07-27"]) == 0

    rendered = json.loads(capsys.readouterr().out)
    assert rendered["tradeDate"] == "2026-07-27"
    assert rendered["finality"] == "post_close_observation"
    assert rendered["state"] == "candidate"
    assert rendered["executionMode"] == "shadow"
    assert container.closed is True
