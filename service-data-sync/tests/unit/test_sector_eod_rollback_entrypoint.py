"""板块 EOD publication rollback 运维入口测试。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from service_data_sync.domain.sector import SectorEodFinality, SectorEodSnapshot, SectorScheme
from service_data_sync.entrypoints import sector_eod_rollback


class FakeContainer:
    """提供 rollback 入口所需数据库占位，并记录 finally 资源关闭。"""

    def __init__(self) -> None:
        """初始化无连接数据库替身与关闭标记。"""
        self.database = object()
        self.closed = False

    def close(self) -> None:
        """记录入口始终执行资源释放。"""
        self.closed = True


class FakeRepository:
    """返回固定已发布 revision，隔离 PostgreSQL rollback SQL。"""

    def __init__(self, _database: object) -> None:
        """接受组合根数据库参数，保持生产仓储构造形状。"""

    def rollback_published_snapshot(
        self, *, scheme: SectorScheme, trade_date: date, revision: int
    ) -> SectorEodSnapshot:
        """断言入口仅请求明确 target，返回恢复后的可见快照。"""
        assert scheme is SectorScheme.EASTMONEY_INDUSTRY
        assert trade_date == date(2026, 7, 27)
        assert revision == 1
        return SectorEodSnapshot(
            snapshot_id=uuid4(),
            data_version=uuid4(),
            scheme=scheme,
            trade_date=trade_date,
            source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
            observed_at=datetime(2026, 7, 27, 8, 20, tzinfo=UTC),
            finality=SectorEodFinality.POST_CLOSE_OBSERVATION,
            quality_status="passed",
            published_at=datetime(2026, 7, 27, 8, 21, tzinfo=UTC),
        )


def test_rollback_requires_publish_policy(monkeypatch) -> None:
    """默认 publish policy 关闭时，rollback 入口不能修改 consumer publication。"""

    def settings() -> SimpleNamespace:
        """返回默认禁止 publication 操作的最小配置。"""
        return SimpleNamespace(sector_eod_publish_enabled=False)

    monkeypatch.setattr(sector_eod_rollback, "load_settings", settings)

    with pytest.raises(SystemExit, match="PUBLISH_ENABLED"):
        sector_eod_rollback.main(
            ["--scheme", "eastmoney.industry", "--trade-date", "2026-07-27", "--revision", "1"]
        )


def test_rollback_restores_explicit_revision_and_closes_container(monkeypatch, capsys) -> None:
    """开关批准后入口必须仅恢复指定 revision，输出机器可读摘要并关闭容器。"""
    container = FakeContainer()

    def settings() -> SimpleNamespace:
        """返回允许受控 publication rollback 的最小配置。"""
        return SimpleNamespace(sector_eod_publish_enabled=True)

    def build(_settings: object) -> FakeContainer:
        """返回测试容器，避免创建真实基础设施连接。"""
        return container

    def configure(*_args: object, **_kwargs: object) -> None:
        """忽略测试日志配置，不影响全局日志处理器。"""

    monkeypatch.setattr(sector_eod_rollback, "load_settings", settings)
    monkeypatch.setattr(sector_eod_rollback, "build_container", build)
    monkeypatch.setattr(sector_eod_rollback, "configure_logging", configure)
    monkeypatch.setattr(sector_eod_rollback, "SqlAlchemySectorEodRepository", FakeRepository)

    assert (
        sector_eod_rollback.main(
            ["--scheme", "eastmoney.industry", "--trade-date", "2026-07-27", "--revision", "1"]
        )
        == 0
    )

    rendered = json.loads(capsys.readouterr().out)
    assert rendered["tradeDate"] == "2026-07-27"
    assert rendered["revision"] == 1
    assert rendered["state"] == "published"
    assert container.closed is True
