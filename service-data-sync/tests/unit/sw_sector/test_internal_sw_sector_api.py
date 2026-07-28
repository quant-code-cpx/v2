"""申万内部 API 的分页、HMAC 游标、闭包和条件读取测试。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_data_sync.application.ports.sw_sector import (
    SwPublication,
    SwStoredNode,
    SwStoredValuation,
)
from service_data_sync.domain.sw_sector import (
    SwIndustryLevel,
    SwIndustryNode,
    SwIndustryValuation,
    SwMethodology,
)
from service_data_sync.interfaces import internal_sw_sector_api
from service_data_sync.interfaces.internal_sw_sector_api import register_sw_sector_routes

_DATE = date(2026, 7, 28)
_VERSION = UUID("10000000-0000-4000-8000-000000000001")


class FakeSwReadRepository:
    """提供一条三级父子链和冻结发布，不连接数据库。"""

    def __init__(self) -> None:
        """初始化三个按层级代码排序的节点。"""
        self.nodes = (
            _stored("801010.SI", "农林牧渔", 1, None),
            _stored("801016.SI", "种植业", 2, "801010.SI"),
            _stored("850111.SI", "种子", 3, "801016.SI"),
        )

    def get_publication(
        self, *, capability: str, snapshot_date: date | None
    ) -> SwPublication | None:
        """为 taxonomy 与估值返回同一测试版本和日期。"""
        assert snapshot_date in {None, _DATE}
        return SwPublication(
            capability=capability,  # type: ignore[arg-type]
            data_version=_VERSION,
            snapshot_date=_DATE,
            published_at=datetime(2026, 7, 28, 10, tzinfo=UTC),
            quality_status="passed",
            row_count=3,
            content_sha256="a" * 64,
            methodology=SwMethodology(
                code="test-sw",
                version=1,
                status="source_reported",
                upstream_source="test.sw",
                semantic_spec_sha256="b" * 64,
            ),
        )

    def list_nodes(
        self,
        *,
        snapshot_date: date,
        level: int | None,
        parent_code: str | None,
        after_level: int | None,
        after_code: str | None,
        limit: int,
    ) -> tuple[SwStoredNode, ...]:
        """模拟层级、父级和复合排序键续页。"""
        assert snapshot_date == _DATE
        rows = self.nodes
        if level is not None:
            rows = tuple(row for row in rows if row.node.level.value == level)
        if parent_code is not None:
            rows = tuple(row for row in rows if row.node.parent_code == parent_code)
        if after_level is not None and after_code is not None:
            rows = tuple(
                row
                for row in rows
                if (row.node.level.value, row.node.code) > (after_level, after_code)
            )
        return rows[:limit]

    def get_node(self, *, snapshot_date: date, code: str) -> SwStoredNode | None:
        """按日期与代码返回一个节点。"""
        return next((row for row in self.nodes if row.node.code == code), None)

    def list_ancestors(
        self, *, data_version: UUID, snapshot_date: date, descendant_code: str
    ) -> tuple[SwStoredNode, ...]:
        """为三级节点返回根到直接父级的冻结闭包。"""
        assert data_version == _VERSION and snapshot_date == _DATE
        if descendant_code == "850111.SI":
            return self.nodes[:2]
        return ()

    def list_valuations(
        self,
        *,
        snapshot_date: date,
        level: int | None,
        after_code: str | None,
        limit: int,
    ) -> tuple[SwStoredValuation, ...]:
        """为节点构造同日估值并按代码续页。"""
        rows = tuple(
            SwStoredValuation(
                node=row.node,
                valuation=SwIndustryValuation(
                    code=row.node.code,
                    snapshot_date=snapshot_date,
                    static_pe=Decimal("10"),
                    ttm_pe=Decimal("11"),
                    pb=Decimal("2"),
                    dividend_yield_ratio=Decimal("0.01"),
                ),
                revision=1,
            )
            for row in self.nodes
            if level is None or row.node.level.value == level
        )
        if after_code is not None:
            rows = tuple(row for row in rows if row.node.code > after_code)
        return rows[:limit]


class UnavailableSwReadRepository(FakeSwReadRepository):
    """模拟尚无任何通过质量门的申万发布。"""

    def get_publication(self, *, capability: str, snapshot_date: date | None) -> None:
        """无论能力和日期都返回未发布。"""
        del capability, snapshot_date
        return None


def test_internal_sw_api_pages_with_hmac_cursor_and_honors_etag() -> None:
    """taxonomy 页应绑定发布与筛选范围，并支持内部 GET 304。"""
    client = _client()

    first = client.get("/internal/v1/sw-industries?limit=1")
    second = client.get(f"/internal/v1/sw-industries?limit=1&cursor={first.json()['nextCursor']}")
    cached = client.get(
        "/internal/v1/sw-industries?limit=1",
        headers={"If-None-Match": first.headers["etag"]},
    )
    mismatched = client.get(
        f"/internal/v1/sw-industries?level=2&limit=1&cursor={first.json()['nextCursor']}"
    )

    assert first.status_code == 200
    assert first.json()["items"][0]["code"] == "801010.SI"
    assert second.json()["items"][0]["code"] == "801016.SI"
    assert cached.status_code == 304
    assert mismatched.status_code == 409


def test_internal_sw_api_returns_parent_closure_and_ratio_valuation() -> None:
    """详情应返回根到父级闭包，估值应保留一比一比例和供应商观察终态。"""
    client = _client()

    detail = client.get("/internal/v1/sw-industries/850111.SI")
    valuation = client.get("/internal/v1/sw-industries/valuations?level=3")

    assert [row["code"] for row in detail.json()["ancestors"]] == [
        "801010.SI",
        "801016.SI",
    ]
    assert valuation.json()["items"][0]["dividendYieldRatio"] == "0.01"
    assert valuation.json()["items"][0]["finality"] == "PROVIDER_OBSERVATION"


def test_internal_sw_api_handles_unavailable_release_missing_node_and_short_secret() -> None:
    """内部 API 应返回可重试发布缺失、稳定 404，并拒绝过短游标签名秘密。"""
    unavailable = _client(repository=UnavailableSwReadRepository())
    missing = _client().get("/internal/v1/sw-industries/999999.SI")

    response = unavailable.get("/internal/v1/sw-industries")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json()["code"] == "sw-publication-unavailable"
    assert missing.status_code == 404
    assert missing.json()["code"] == "sw-industry-not-found"
    with pytest.raises(ValueError, match="16 bytes"):
        _client(cursor_secret=b"short")


def test_internal_sw_valuation_cursor_pages_and_rejects_filter_or_signature_drift() -> None:
    """估值游标应稳定续页，并绑定发布、层级筛选和 HMAC 完整性。"""
    client = _client()
    first = client.get("/internal/v1/sw-industries/valuations?limit=1")
    cursor = first.json()["nextCursor"]

    second = client.get(f"/internal/v1/sw-industries/valuations?limit=1&cursor={cursor}")
    mismatched = client.get(
        f"/internal/v1/sw-industries/valuations?level=2&limit=1&cursor={cursor}"
    )
    tampered = client.get(f"/internal/v1/sw-industries/valuations?limit=1&cursor={cursor[:-1]}x")
    missing_after_code = internal_sw_sector_api._encode_cursor(
        {
            "kind": "sw-valuation",
            "dataVersion": str(_VERSION),
            "snapshotDate": _DATE.isoformat(),
            "level": None,
        },
        b"test-only-sw-cursor-secret-000000",
    )
    invalid_shape = client.get(
        f"/internal/v1/sw-industries/valuations?limit=1&cursor={missing_after_code}"
    )

    assert first.status_code == 200
    assert second.json()["items"][0]["code"] == "801016.SI"
    assert mismatched.status_code == 409
    assert tampered.status_code == 409
    assert invalid_shape.status_code == 409


def _client(
    *,
    repository: object | None = None,
    cursor_secret: bytes = b"test-only-sw-cursor-secret-000000",
) -> TestClient:
    """构造仅含申万路由且认证已由测试满足的 FastAPI client。"""
    app = FastAPI()

    def require_service_bearer() -> None:
        """测试中以空依赖表示已通过服务认证。"""

    register_sw_sector_routes(
        app,
        repository=repository or FakeSwReadRepository(),  # type: ignore[arg-type]
        require_service_bearer=require_service_bearer,
        cursor_secret=cursor_secret,
    )
    return TestClient(app)


def _stored(code: str, name: str, level: int, parent_code: str | None) -> SwStoredNode:
    """构造一个带 revision 的申万读取节点。"""
    return SwStoredNode(
        node=SwIndustryNode(
            code=code,
            name=name,
            level=SwIndustryLevel(level),
            parent_code=parent_code,
            component_count=8,
        ),
        revision=1,
    )
