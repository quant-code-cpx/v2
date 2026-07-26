"""板块有界 CLI 的组合、关闭和机器可读输出单元测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

from service_data_sync.application.sector.bar_sync import SectorBarSyncResult
from service_data_sync.domain.sector import SectorIdentifier, SectorPeriod
from service_data_sync.entrypoints import sector_bars


class FakeRegistry:
    """为 CLI 返回一个周期匹配的中立来源对象。"""

    def for_capability(self, capability: str) -> tuple[object, ...]:
        """断言周线 capability 被直接选择，而非误用日线能力。"""
        assert capability == "sector.bar.1w.raw"
        return (object(),)


class FakeContainer:
    """模拟组合根的必要依赖与关闭状态。"""

    def __init__(self) -> None:
        """初始化无网络替身依赖。"""
        self.database = object()
        self.object_storage = object()
        self.source_registry = FakeRegistry()
        self.closed = False

    def close(self) -> None:
        """记录 CLI 的 finally 资源释放。"""
        self.closed = True


class FakeSyncService:
    """避免外部 I/O，返回包含传入身份和周期的确定性发布摘要。"""

    def __init__(self, **kwargs: object) -> None:
        """验证 CLI 构造了同步用例的三个中立依赖。"""
        assert set(kwargs) == {"source", "repository", "raw_payload_store"}

    async def sync(self, **kwargs: object) -> SectorBarSyncResult:
        """返回断言调用方所选周期的确定性成功结果。"""
        identifier = kwargs["identifier"]
        period = kwargs["period"]
        assert isinstance(identifier, SectorIdentifier)
        assert period is SectorPeriod.WEEK_1
        return SectorBarSyncResult(
            sector=identifier,
            period=period,
            data_version=uuid4(),
            inserted_count=3,
            unchanged_count=1,
        )


def test_cli_requires_bounded_window_and_closes_composition_root(monkeypatch, capsys) -> None:
    """传入明确周线日期窗时渲染发布摘要，并始终关闭组合根。"""
    container = FakeContainer()
    # 以下匿名回调只替换组合根依赖，避免 CLI 单测连接外部服务。
    monkeypatch.setattr(sector_bars, "load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(sector_bars, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sector_bars, "build_container", lambda _settings: container)
    monkeypatch.setattr(
        sector_bars, "SqlAlchemySectorMarketDataRepository", lambda _database: object()
    )
    monkeypatch.setattr(sector_bars, "S3RawPayloadStore", lambda _storage: object())
    monkeypatch.setattr(sector_bars, "SectorBarSyncService", FakeSyncService)

    assert (
        sector_bars.main(
            [
                "--scheme",
                "eastmoney.industry",
                "--sector",
                "BK0475",
                "--period",
                "1w",
                "--start",
                "2026-06-01",
                "--end",
                "2026-06-30",
            ]
        )
        == 0
    )

    rendered = json.loads(capsys.readouterr().out)
    assert rendered["period"] == "1w"
    assert rendered["inserted_count"] == 3
    assert container.closed is True
