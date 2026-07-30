"""互联互通只读仓储的父版本和共同总览历史测试。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from service_data_sync.infrastructure.database.models.market import (
    StockConnectBundlePublication,
    StockConnectOverviewPublication,
)
from service_data_sync.infrastructure.persistence.stock_connect_read_repository import (
    StockConnectParentPublicationMismatch,
    _overview_trend,
    _resolve_active_parent,
)


class FakeScalarResult:
    """提供 SQLAlchemy 结果对象在本组测试需要的最小标量接口。"""

    def __init__(self, value: object) -> None:
        """保存单值或标量列表。"""
        self._value = value

    def scalar_one_or_none(self) -> object | None:
        """返回单个标量或空值。"""
        return self._value

    def scalars(self) -> FakeScalarResult:
        """返回自身以支持 `scalars().all()` 链。"""
        return self

    def all(self) -> list[object]:
        """返回预置标量列表。"""
        assert isinstance(self._value, list)
        return self._value


class FakeSession:
    """按预定顺序返回 SQLAlchemy 标量结果，不访问真实数据库。"""

    def __init__(self, values: list[object]) -> None:
        """保存每次 execute 应返回的值。"""
        self._values = values

    def execute(self, statement: object) -> FakeScalarResult:
        """消费下一项结果；查询表达式仅证明函数确实发起了解析。"""
        del statement
        return FakeScalarResult(self._values.pop(0))


def _bundle(
    *,
    release_id: str,
    data_version: str,
    channel: str,
    direction: str,
) -> StockConnectBundlePublication:
    """构造读投影所需字段齐全的不可变通道 bundle。"""
    return StockConnectBundlePublication(
        bundle_release_id=UUID(release_id),
        data_version=data_version,
        trade_date=date(2026, 7, 29),
        channel=channel,
        direction=direction,
        summary_json={
            "stats": {"turnoverAmount": {"amount": "100", "currency": "CNY"}},
            "status": {"sessionState": "CLOSED"},
        },
        active_securities_json=[],
        quality_status="APPROVED",
        quality_issues=[],
        source_refs=[],
        published_at=datetime(2026, 7, 29, 10, tzinfo=UTC),
        superseded_at=None,
    )


def _overview(
    *,
    data_version: str,
    components: dict[str, str],
) -> StockConnectOverviewPublication:
    """构造固定组件 UUID 的共同总览 publication。"""
    return StockConnectOverviewPublication(
        overview_release_id=UUID("30000000-0000-4000-8000-000000000001"),
        data_version=data_version,
        trade_date=date(2026, 7, 29),
        channel_set=",".join(sorted(components)),
        component_bundle_ids=components,
        quality_status="APPROVED",
        quality_issues=[],
        source_refs=[],
        published_at=datetime(2026, 7, 29, 10, tzinfo=UTC),
        superseded_at=None,
    )


def test_active_parent_resolves_overview_fixed_component_without_latest() -> None:
    """父总览直接选择 manifest 中的 bundle，之后出现新 latest 也不改变结果。"""
    sh = _bundle(
        release_id="20000000-0000-4000-8000-000000000001",
        data_version="20000000-0000-4000-8000-000000000011",
        channel="SH",
        direction="NORTHBOUND",
    )
    overview = _overview(
        data_version="10000000-0000-4000-8000-000000000001",
        components={"SH_NORTHBOUND": str(sh.bundle_release_id)},
    )
    session = cast(Session, FakeSession([overview, None, [sh]]))

    resolved = _resolve_active_parent(
        session,
        parent_data_version=overview.data_version,
        mode="LATEST",
        exact_date=None,
        channel="SH_NORTHBOUND",
    )

    assert resolved.bundle is sh
    assert resolved.data_version == overview.data_version
    assert resolved.publication["dataVersion"] == overview.data_version
    assert resolved.date_resolution == "LATEST_COMMON"


def test_active_parent_rejects_date_or_channel_mismatch() -> None:
    """父 publication 与精确日期或通道不一致时必须冲突，不得自动改读当前版本。"""
    sh = _bundle(
        release_id="20000000-0000-4000-8000-000000000002",
        data_version="20000000-0000-4000-8000-000000000012",
        channel="SH",
        direction="NORTHBOUND",
    )
    session = cast(Session, FakeSession([None, sh]))

    with pytest.raises(StockConnectParentPublicationMismatch, match="does not match"):
        _resolve_active_parent(
            session,
            parent_data_version=sh.data_version,
            mode="EXACT",
            exact_date=date(2026, 7, 28),
            channel="SH_NORTHBOUND",
        )


def test_overview_trend_uses_one_common_version_for_every_channel_point() -> None:
    """同一交易日的多通道趋势点必须共用持久化总览版本。"""
    sh = _bundle(
        release_id="20000000-0000-4000-8000-000000000003",
        data_version="20000000-0000-4000-8000-000000000013",
        channel="SH",
        direction="NORTHBOUND",
    )
    sz = _bundle(
        release_id="20000000-0000-4000-8000-000000000004",
        data_version="20000000-0000-4000-8000-000000000014",
        channel="SZ",
        direction="NORTHBOUND",
    )
    overview = _overview(
        data_version="10000000-0000-4000-8000-000000000002",
        components={
            "SH_NORTHBOUND": str(sh.bundle_release_id),
            "SZ_NORTHBOUND": str(sz.bundle_release_id),
        },
    )
    session = cast(Session, FakeSession([[overview], [sh, sz]]))

    points = _overview_trend(
        session,
        channels=("SH_NORTHBOUND", "SZ_NORTHBOUND"),
        channel_set=overview.channel_set,
        resolved_date=overview.trade_date,
        limit=20,
    )

    assert [point["channel"] for point in points] == [
        "SH_NORTHBOUND",
        "SZ_NORTHBOUND",
    ]
    assert {point["dataVersion"] for point in points} == {overview.data_version}
