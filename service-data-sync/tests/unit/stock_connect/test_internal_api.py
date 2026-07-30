"""互联互通内部查询的父 publication、认证和版本头合同测试。"""

from __future__ import annotations

from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_data_sync.infrastructure.persistence.stock_connect_read_repository import (
    SqlAlchemyStockConnectReadRepository,
    StockConnectParentPublicationMismatch,
    StockConnectReadResult,
)
from service_data_sync.interfaces.internal_stock_connect_api import (
    register_stock_connect_routes,
)

_PARENT_VERSION = "stock-connect-overview:2026-07-29:rev-10"
_READINESS_VERSION = "b" * 64


class FakeStockConnectReadRepository:
    """捕获活跃榜查询并返回与父版本完全一致的固定 publication。"""

    def __init__(self, *, mismatch: bool = False) -> None:
        """保存是否模拟父版本不匹配并初始化调用记录。"""
        self._mismatch = mismatch
        self.parent_versions: list[str] = []
        self.readiness_channels: list[tuple[str, ...]] = []

    def readiness(
        self,
        *,
        mode: str,
        exact_date: object,
        channels: tuple[str, ...],
    ) -> StockConnectReadResult:
        """返回固定 readiness 表示，并记录 API 是否稳定排序通道。"""
        del exact_date
        self.readiness_channels.append(channels)
        return StockConnectReadResult(
            body={
                "schemaVersion": "quant-v2.stock-connect-readiness.v1",
                "dataVersion": _READINESS_VERSION,
                "mode": mode,
                "selectedChannels": list(channels),
                "requestedExactDate": None,
                "candidateTradeDate": "2026-07-29",
                "readyTradeDate": None,
                "observedAt": "2026-07-29T10:00:00Z",
                "calendar": {
                    "dataVersion": "c" * 64,
                    "observedAt": "2026-07-29T09:55:00Z",
                    "sourceFileSha256": "d" * 64,
                    "sourcePublicationAt": "2026-07-29T09:50:00Z",
                    "publicationAvailability": "REPORTED",
                },
                "channels": [
                    {
                        "channel": channel,
                        "calendarState": "OPEN",
                        "state": "PENDING",
                        "reasonCode": "PREFLIGHT_PENDING",
                        "bundleDataVersion": None,
                        "evidenceObservedAt": "2026-07-29T10:00:00Z",
                    }
                    for channel in channels
                ],
            },
            data_version=_READINESS_VERSION,
        )

    def active_securities(
        self,
        *,
        mode: str,
        exact_date: object,
        channel: str,
        ranking: str,
        cursor: str | None,
        limit: int,
        parent_publication_data_version: str,
    ) -> StockConnectReadResult:
        """记录父版本；需要时抛出仓储的稳定 409 语义。"""
        del mode, exact_date, channel, ranking, cursor, limit
        self.parent_versions.append(parent_publication_data_version)
        if self._mismatch:
            raise StockConnectParentPublicationMismatch(
                "parent stock-connect publication does not match request"
            )
        return StockConnectReadResult(
            body={
                "resolvedTradeDate": "2026-07-29",
                "dateResolution": "LATEST_COMMON",
                "channel": "SH_NORTHBOUND",
                "ranking": "SOURCE_ACTIVE",
                "rankingAvailability": "REPORTED",
                "rankingScope": "SOURCE_ACTIVE_SECURITIES_ONLY",
                "items": [],
                "nextCursor": None,
                "publication": {
                    "bundleReleaseId": ("20000000-0000-4000-8000-000000000010"),
                    "dataVersion": _PARENT_VERSION,
                    "tradeDate": "2026-07-29",
                    "publishedAt": "2026-07-29T10:00:00Z",
                    "qualityStatus": "APPROVED",
                    "qualityIssues": [],
                    "sourceRefs": [],
                },
            },
            data_version=_PARENT_VERSION,
        )


def _client(
    repository: FakeStockConnectReadRepository,
) -> TestClient:
    """构造只注册互联互通 POST 路由的最小内部应用。"""
    app = FastAPI()
    register_stock_connect_routes(
        app,
        repository=cast(SqlAlchemyStockConnectReadRepository, repository),
        read_bearer_token="stock-connect-read-token",
    )
    return TestClient(app)


def _body(*, include_parent: bool = True) -> dict[str, object]:
    """构造 latest 活跃榜请求，可选择省略强制父版本字段。"""
    body: dict[str, object] = {
        "date": {"mode": "LATEST", "exactDate": None},
        "channel": "SH_NORTHBOUND",
        "ranking": "SOURCE_ACTIVE",
        "cursor": None,
        "limit": 20,
    }
    if include_parent:
        body["parentPublicationDataVersion"] = _PARENT_VERSION
    return body


def _headers() -> dict[str, str]:
    """返回同时满足服务认证和链路追踪的最小请求头。"""
    return {
        "Authorization": "Bearer stock-connect-read-token",
        "X-Request-Id": "stock-connect-parent-test",
    }


def test_active_query_requires_and_echoes_parent_publication_version() -> None:
    """活跃榜必须把父版本传到仓储，并保持正文 publication 与响应头一致。"""
    repository = FakeStockConnectReadRepository()
    client = _client(repository)

    response = client.post(
        "/internal/v1/stock-connect/active-securities/query",
        headers=_headers(),
        json=_body(),
    )
    missing = client.post(
        "/internal/v1/stock-connect/active-securities/query",
        headers=_headers(),
        json=_body(include_parent=False),
    )

    assert response.status_code == 200
    assert response.headers["x-data-version"] == _PARENT_VERSION
    assert response.headers["x-request-id"] == "stock-connect-parent-test"
    assert repository.parent_versions == [_PARENT_VERSION]
    assert missing.status_code == 400
    assert missing.json()["code"] == "VALIDATION_FAILED"
    assert response.json()["publication"]["dataVersion"] == _PARENT_VERSION


def test_active_query_maps_parent_mismatch_to_stable_conflict() -> None:
    """父版本不存在或筛选不匹配时必须返回稳定 409，不能回退 latest。"""
    client = _client(FakeStockConnectReadRepository(mismatch=True))

    response = client.post(
        "/internal/v1/stock-connect/active-securities/query",
        headers=_headers(),
        json=_body(),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "PARENT_PUBLICATION_MISMATCH"


def test_active_query_accepts_bounded_non_uuid_parent_version_and_rejects_control_char() -> None:
    """父 publication 版本是 1..160 普通文本，不得误收窄为 UUID 或接收控制字符。"""
    repository = FakeStockConnectReadRepository()
    client = _client(repository)
    invalid_body = _body()
    invalid_body["parentPublicationDataVersion"] = "unsafe\nversion"

    accepted = client.post(
        "/internal/v1/stock-connect/active-securities/query",
        headers=_headers(),
        json=_body(),
    )
    rejected = client.post(
        "/internal/v1/stock-connect/active-securities/query",
        headers=_headers(),
        json=invalid_body,
    )

    assert accepted.status_code == 200
    assert rejected.status_code == 400
    assert rejected.json()["code"] == "VALIDATION_FAILED"


def test_readiness_query_returns_body_hash_header_and_stable_channel_order() -> None:
    """readiness 正文版本必须等于响应头，且通道按合同稳定顺序进入仓储和表示。"""
    repository = FakeStockConnectReadRepository()
    client = _client(repository)

    response = client.post(
        "/internal/v1/stock-connect/readiness/query",
        headers=_headers(),
        json={
            "date": {"mode": "LATEST", "exactDate": None},
            "channels": ["SZ_SOUTHBOUND", "SH_NORTHBOUND"],
        },
    )

    assert response.status_code == 200
    assert response.headers["x-data-version"] == _READINESS_VERSION
    assert response.json()["dataVersion"] == _READINESS_VERSION
    assert response.json()["selectedChannels"] == [
        "SH_NORTHBOUND",
        "SZ_SOUTHBOUND",
    ]
    assert repository.readiness_channels == [("SH_NORTHBOUND", "SZ_SOUTHBOUND")]
