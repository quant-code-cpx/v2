"""板块成分 CLI 的来源准入、JSON 摘要和资源释放回归测试。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from service_data_sync.application.ports.sector_membership import PublishedSectorMembershipRelease
from service_data_sync.application.sector.membership_sync import (
    SectorMembershipSyncItem,
    SectorMembershipSyncResult,
)
from service_data_sync.domain.sector import SectorIdentifier, SectorScheme
from service_data_sync.entrypoints import sector_membership


class FakeRegistry:
    """返回唯一已准入的成分快照来源，阻止 CLI 擅自选择多个来源。"""

    def for_capability(self, capability: str) -> tuple[object, ...]:
        """校验 CLI 只请求板块成员 capability。"""
        assert capability == "sector.membership.snapshot.raw"
        return (object(),)


class FakeContainer:
    """提供 CLI 组合根最小依赖，并记录 finally 是否关闭资源。"""

    def __init__(self) -> None:
        """初始化不连接外部服务的依赖替身。"""
        self.database = object()
        self.object_storage = object()
        self.source_registry: object = FakeRegistry()
        self.closed = False

    def close(self) -> None:
        """记录所有执行路径都应触发的资源关闭。"""
        self.closed = True


class FakeSyncService:
    """替代真实同步，返回一个已发布的行业 release。"""

    def __init__(self, **kwargs: object) -> None:
        """验证 CLI 只传中立来源、仓储和 raw 存储端口。"""
        assert set(kwargs) == {"source", "repository", "raw_payload_store"}

    async def sync_scheme(
        self, *, scheme: SectorScheme, observation_date: date
    ) -> SectorMembershipSyncResult:
        """校验固定市场日并返回完整发布摘要。"""
        assert scheme is SectorScheme.EASTMONEY_INDUSTRY
        assert observation_date == date(2026, 7, 27)
        item = SectorMembershipSyncItem(
            identifier=SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, "BK0475"),
            snapshot_id=uuid4(),
            complete=True,
            pending_count=0,
            quarantine_count=0,
        )
        release = PublishedSectorMembershipRelease(
            release_id=uuid4(),
            data_version=uuid4(),
            quality_status="passed",
            fresh_sector_count=1,
            carried_forward_sector_count=0,
            published_at=datetime(2026, 7, 27, tzinfo=UTC),
        )
        return SectorMembershipSyncResult(
            scheme=scheme,
            items=(item,),
            failures=(),
            release=release,
        )


def test_cli_emits_release_summary_and_closes_composition_root(monkeypatch, capsys) -> None:
    """唯一批准来源、成功 release 与无失败时 CLI 必须返回零和机器可读摘要。"""
    container = FakeContainer()
    monkeypatch.setattr(sector_membership, "load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(sector_membership, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sector_membership, "build_container", lambda _settings: container)
    monkeypatch.setattr(
        sector_membership,
        "SqlAlchemySectorMembershipRepository",
        lambda _database: object(),
    )
    monkeypatch.setattr(sector_membership, "S3RawPayloadStore", lambda _storage: object())
    # 入口组合测试不触及对象存储；留证语义由原始载荷存储的专用单元测试覆盖。
    monkeypatch.setattr(
        sector_membership, "retain_failure_evidence", lambda _store, operation: operation()
    )
    monkeypatch.setattr(sector_membership, "SectorMembershipSyncService", FakeSyncService)

    assert (
        sector_membership.main(
            ["--scheme", "eastmoney.industry", "--observation-date", "2026-07-27"]
        )
        == 0
    )

    rendered = json.loads(capsys.readouterr().out)
    assert rendered["items"][0]["sector"] == "BK0475"
    assert rendered["release"]["quality_status"] == "passed"
    assert container.closed is True


def test_cli_rejects_zero_or_multiple_approved_sources(monkeypatch) -> None:
    """没有唯一获准来源时不能执行抓取，避免擅自 fallback 或混合多个成员口径。"""
    container = FakeContainer()

    class InvalidRegistry:
        """返回两个来源，模拟未决 source policy。"""

        def for_capability(self, capability: str) -> tuple[object, ...]:
            """校验能力后故意违反唯一来源约束。"""
            assert capability == "sector.membership.snapshot.raw"
            return object(), object()

    container.source_registry = InvalidRegistry()
    monkeypatch.setattr(sector_membership, "load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(sector_membership, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sector_membership, "build_container", lambda _settings: container)

    with pytest.raises(SystemExit, match="exactly one approved"):
        sector_membership.main(
            ["--scheme", "eastmoney.industry", "--observation-date", "2026-07-27"]
        )

    assert container.closed is True
