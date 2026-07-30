"""十八族 data-status 聚合版本、双时态透传和条件读取专项测试。"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from service_data_sync.interfaces import internal_equity_workspace_api as reader
from service_data_sync.interfaces.internal_sector_api import InternalProblem


class _NoopSession:
    """供状态 helper 已替换时占位的 Session。"""


class _SessionContext(AbstractContextManager[_NoopSession]):
    """提供 data-status 测试 Session 上下文。"""

    def __enter__(self) -> _NoopSession:
        """返回无状态 Session。"""
        return _NoopSession()

    def __exit__(self, *_args: object) -> None:
        """退出时无需释放资源。"""
        return None


class _Database:
    """提供 data-status 路由所需的数据库外观。"""

    def session(self) -> _SessionContext:
        """创建 data-status 测试 Session。"""
        return _SessionContext()


def _require_bearer(authorization: str | None = Header(default=None)) -> None:
    """仅允许测试内部 Bearer。"""
    if authorization != "Bearer reader-secret":
        raise HTTPException(status_code=401)


def _app() -> FastAPI:
    """构造 data-status 专项测试应用。"""
    app = FastAPI()

    @app.exception_handler(InternalProblem)
    async def render_problem(_request: Request, error: InternalProblem) -> JSONResponse:
        """把 reader 稳定问题投影为可断言的最小响应。"""
        return JSONResponse(
            status_code=error.status,
            content={"code": error.code, "detail": error.detail},
        )

    reader.register_equity_workspace_routes(
        app,
        database=_Database(),  # type: ignore[arg-type]
        require_service_bearer=_require_bearer,
        cursor_secret=b"reader-cursor-secret",
    )
    return app


def _status(family: str, version: UUID) -> dict[str, Any]:
    """构造一个严格符合冻结合同的数据集状态。"""
    return {
        "family": family,
        "dataset": reader._STATUS_DATASETS[family],
        "availability": "AVAILABLE",
        "freshness": "FRESH",
        "dataVersion": str(version),
        "publishedAt": "2026-07-30T08:00:00Z",
        "effectiveAsOf": "2026-07-29",
        "knowledgeCutoff": "2026-07-30T08:00:00Z",
        "sourceLabel": None,
        "methodology": None,
        "reasonCode": None,
        "retryable": False,
    }


def test_data_status_returns_all_families_and_invalidates_on_component_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任一族版本改变都必须改变 X-Data-Version 与 ETag。"""
    versions = {family: uuid4() for family in reader._STATUS_DATASETS}
    identity_version = uuid4()
    name_state = {"version_id": uuid4()}
    captured: dict[str, Any] = {}

    def resolve_identity(*_args: object, **kwargs: object) -> tuple[Any, Any]:
        """记录查询并让永久锚当前投影故意不同于历史 identifier。"""
        captured["identity"] = kwargs
        return (
            SimpleNamespace(
                security_id=7,
                exchange="SZSE",
                symbol="000001",
                name="当前兼容名称",
            ),
            SimpleNamespace(
                version_id=identity_version,
                exchange="SSE",
                symbol="600519",
            ),
        )

    def resolve_name(*_args: object, **kwargs: object) -> Any:
        """返回请求双时态切片内的历史名称版本。"""
        captured["name"] = kwargs
        return SimpleNamespace(
            version_id=name_state["version_id"],
            name="历史名称",
        )

    def no_discovery(*_args: object, **kwargs: object) -> dict[str, Any]:
        """记录 discovery 双时态参数并返回无辅助元数据。"""
        captured["discovery"] = kwargs
        return {}

    def dataset_status(*_args: object, **kwargs: object) -> dict[str, Any]:
        """按当前测试版本生成一个精确族状态。"""
        captured.setdefault("statuses", []).append(kwargs)
        family = str(kwargs["family"])
        return _status(family, versions[family])

    monkeypatch.setattr(reader, "_identity", resolve_identity)
    monkeypatch.setattr(reader, "_identity_name", resolve_name)
    monkeypatch.setattr(reader, "_discovery_availability", no_discovery)
    monkeypatch.setattr(reader, "_dataset_status", dataset_status)
    client = TestClient(_app())
    headers = {"Authorization": "Bearer reader-secret"}
    body = {
        "asOf": "2026-07-29",
        "knownAt": "2026-07-30T08:00:00Z",
    }
    first = client.post(
        "/internal/v1/equities/SSE/600519/data-status/query",
        headers=headers,
        json=body,
    )

    assert first.status_code == 200
    assert len(first.json()["datasets"]) == 18
    assert first.json()["identity"] == {
        "exchange": "SSE",
        "symbol": "600519",
        "name": "历史名称",
        "identityAsOf": "2026-07-29",
    }
    assert captured["identity"]["as_of"] == date(2026, 7, 29)
    assert captured["identity"]["known_at"] == datetime(2026, 7, 30, 8, tzinfo=UTC)
    assert captured["name"]["as_of"] == date(2026, 7, 29)
    assert captured["name"]["known_at"] == datetime(2026, 7, 30, 8, tzinfo=UTC)
    assert all(
        item["known_at"] == datetime(2026, 7, 30, 8, tzinfo=UTC) for item in captured["statuses"]
    )

    unchanged = client.post(
        "/internal/v1/equities/SSE/600519/data-status/query",
        headers={**headers, "If-None-Match": first.headers["etag"]},
        json=body,
    )
    assert unchanged.status_code == 304

    name_state["version_id"] = uuid4()
    renamed = client.post(
        "/internal/v1/equities/SSE/600519/data-status/query",
        headers={**headers, "If-None-Match": first.headers["etag"]},
        json=body,
    )
    assert renamed.status_code == 200
    assert renamed.headers["x-data-version"] != first.headers["x-data-version"]

    versions["BARS_1D"] = uuid4()
    changed = client.post(
        "/internal/v1/equities/SSE/600519/data-status/query",
        headers={**headers, "If-None-Match": first.headers["etag"]},
        json=body,
    )

    assert changed.status_code == 200
    assert changed.headers["etag"] != first.headers["etag"]
    assert changed.headers["x-data-version"] != first.headers["x-data-version"]


def test_default_data_status_requires_identity_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认状态查询缺少身份 publication 时必须 503，禁止用服务器日期伪造锚点。"""

    def resolve_identity(*_args: object, **_kwargs: object) -> tuple[Any, Any]:
        """返回仍开放的当前身份，使测试只覆盖 publication 门禁。"""
        return (
            SimpleNamespace(
                security_id=7,
                exchange="SSE",
                symbol="600519",
                name="贵州茅台",
            ),
            SimpleNamespace(version_id=uuid4()),
        )

    def no_discovery(*_args: object, **_kwargs: object) -> dict[str, Any]:
        """模拟尚未形成 discovery publication 的数据库。"""
        return {}

    monkeypatch.setattr(reader, "_identity", resolve_identity)
    monkeypatch.setattr(reader, "_discovery_availability", no_discovery)
    response = TestClient(_app()).post(
        "/internal/v1/equities/SSE/600519/data-status/query",
        headers={"Authorization": "Bearer reader-secret"},
        json={},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "publication-unavailable"


def test_default_identity_as_of_comes_from_exact_component_publication() -> None:
    """默认身份锚点必须采用 discovery 行精确引用版本的 effectiveAsOf。"""
    component_version = uuid4()

    class _PublicationSession:
        """返回身份组件精确版本对应的 publication。"""

        def scalar(self, statement: Any) -> SimpleNamespace:
            """校验 exchange child dataset 约束并返回带业务日的 publication。"""
            assert "equity.master.catalog" in statement.compile().params.values()
            return SimpleNamespace(effective_as_of=date(2026, 7, 29))

    availability: Any = SimpleNamespace(component_data_version=component_version)

    result = reader._status_identity_as_of(
        _PublicationSession(),
        requested_as_of=None,
        known_at=datetime(2026, 7, 30, 8, tzinfo=UTC),
        availability_row=availability,
    )

    assert result == date(2026, 7, 29)


def test_identity_status_keeps_dataset_and_component_version_lineage_aligned() -> None:
    """身份状态必须把 exchange child dataset 与其真实 publication 版本成对返回。"""
    component_version = uuid4()
    publication = SimpleNamespace(
        dataset="equity.master.catalog",
        data_version=component_version,
        quality_status="passed",
        published_at=datetime(2026, 7, 30, 8, tzinfo=UTC),
        effective_as_of=date(2026, 7, 29),
        knowledge_cutoff=datetime(2026, 7, 30, 7, 30, tzinfo=UTC),
    )

    class _IdentityPublicationSession:
        """提供 discovery 身份行精确引用的 exchange child publication。"""

        def scalar(self, statement: Any) -> SimpleNamespace:
            """校验查询绑定真实 child dataset，而非父集合标签。"""
            assert "equity.master.catalog" in statement.compile().params.values()
            return publication

    availability: Any = SimpleNamespace(
        component_data_version=component_version,
        source_label="official-exchange",
        methodology=None,
    )

    status = reader._identity_dataset_status(
        _IdentityPublicationSession(),
        family="IDENTITY",
        as_of=date(2026, 7, 29),
        known_at=None,
        availability_row=availability,
    )

    assert status["dataset"] == "equity.master.catalog"
    assert status["dataVersion"] == str(component_version)


def test_financial_indicator_status_requires_both_real_component_publications() -> None:
    """供应商指标与平台派生指标必须各有唯一发布，缺一时只能声明 PARTIAL。"""

    def publication(*, quality_status: str = "passed") -> SimpleNamespace:
        """构造一个具备独立版本的真实 publication 投影。"""
        return SimpleNamespace(
            publication_id=uuid4(),
            data_version=uuid4(),
            quality_status=quality_status,
            published_at=datetime(2026, 7, 30, 8, tzinfo=UTC),
            effective_as_of=date(2026, 7, 29),
            knowledge_cutoff=datetime(2026, 7, 30, 7, 30, tzinfo=UTC),
        )

    provider_methodology = SimpleNamespace(
        source_code="provider",
        code="provider.financial-indicator",
        version=1,
    )
    derived_methodology = SimpleNamespace(
        source_code="platform",
        code="platform.financial-derivation",
        version=1,
    )
    provider = publication()
    derived = publication()
    complete_rows: Any = [
        (provider, "financial.provider-metric", provider_methodology),
        (derived, "financial.derived-metric", derived_methodology),
    ]

    complete = reader._financial_indicator_status(
        rows=complete_rows,
        family="FINANCIAL_INDICATOR",
        as_of=date(2026, 7, 29),
    )
    partial = reader._financial_indicator_status(
        rows=complete_rows[:1],
        family="FINANCIAL_INDICATOR",
        as_of=date(2026, 7, 29),
    )
    warned_rows: Any = [
        (
            publication(quality_status="warned"),
            "financial.provider-metric",
            provider_methodology,
        ),
        (publication(), "financial.derived-metric", derived_methodology),
    ]
    warned = reader._financial_indicator_status(
        rows=warned_rows,
        family="FINANCIAL_INDICATOR",
        as_of=date(2026, 7, 29),
    )
    failed_rows: Any = [
        (
            publication(quality_status="failed"),
            "financial.provider-metric",
            provider_methodology,
        ),
        (publication(), "financial.derived-metric", derived_methodology),
    ]
    failed = reader._financial_indicator_status(
        rows=failed_rows,
        family="FINANCIAL_INDICATOR",
        as_of=date(2026, 7, 29),
    )

    assert complete["availability"] == "AVAILABLE"
    assert complete["reasonCode"] is None
    assert complete["sourceLabel"] == "platform"
    assert complete["methodology"] == {
        "code": "platform.financial-derivation",
        "version": "1",
    }
    assert complete["dataVersion"] == str(derived.data_version)
    assert partial["availability"] == "PARTIAL"
    assert partial["reasonCode"] == "FINANCIAL_COMPONENT_PARTIAL"
    assert partial["dataVersion"] is None
    assert warned["availability"] == "PARTIAL"
    assert warned["reasonCode"] == "QUALITY_WARNING"
    assert failed["availability"] == "SOURCE_UNAVAILABLE"
    assert failed["reasonCode"] == "QUALITY_FAILED"
    assert failed["retryable"] is True


@pytest.mark.parametrize(
    ("quality_status", "availability", "reason_code", "retryable"),
    [
        ("passed", "AVAILABLE", None, False),
        ("PASSED", "AVAILABLE", None, False),
        ("warning", "PARTIAL", "QUALITY_WARNING", False),
        ("WARNING", "PARTIAL", "QUALITY_WARNING", False),
        ("warned", "PARTIAL", "QUALITY_WARNING", False),
        ("WARNED", "PARTIAL", "QUALITY_WARNING", False),
        ("partial", "PARTIAL", "QUALITY_WARNING", False),
        ("PARTIAL", "PARTIAL", "QUALITY_WARNING", False),
        ("failed", "SOURCE_UNAVAILABLE", "QUALITY_FAILED", True),
        ("FAILED", "SOURCE_UNAVAILABLE", "QUALITY_FAILED", True),
    ],
)
def test_publication_quality_controls_dataset_availability(
    quality_status: str,
    availability: str,
    reason_code: str | None,
    retryable: bool,
) -> None:
    """数据状态必须按冻结质量枚举独立门禁 publication，不得只凭版本存在宣称可用。"""
    publication: Any = SimpleNamespace(
        data_version=uuid4(),
        quality_status=quality_status,
        published_at=datetime(2026, 7, 30, 8, tzinfo=UTC),
        effective_as_of=date(2026, 7, 29),
        knowledge_cutoff=datetime(2026, 7, 30, 7, 30, tzinfo=UTC),
    )

    status = reader._publication_status(
        family="BARS_1D",
        dataset="equity.bar.1d.raw",
        publication=publication,
        as_of=date(2026, 7, 29),
        source_label=None,
        methodology=None,
    )

    assert status["availability"] == availability
    assert status["reasonCode"] == reason_code
    assert status["retryable"] is retryable
