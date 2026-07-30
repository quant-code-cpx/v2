"""双时态身份解析与十八类数据状态精确性专项测试。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from service_data_sync.infrastructure.database.models.equity.workspace import (
    EquityDiscoveryAvailability,
)
from service_data_sync.interfaces import internal_equity_workspace_api as reader
from service_data_sync.interfaces.internal_sector_api import InternalProblem


class _Rows:
    """提供身份与状态查询需要的结果外观。"""

    def __init__(self, values: list[Any]) -> None:
        """保存查询结果。"""
        self._values = values

    def all(self) -> list[Any]:
        """返回全部测试行。"""
        return list(self._values)


class _IdentitySession:
    """返回预设双时态 identity 行并记录 SQL。"""

    def __init__(self, values: list[Any]) -> None:
        """保存 identity 行并初始化语句记录。"""
        self._values = values
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> _Rows:
        """记录双时态 SQL 并返回预设行。"""
        self.statements.append(statement)
        return _Rows(self._values)


class _NameSession:
    """返回预设名称版本并记录双时态 SQL。"""

    def __init__(self, values: list[Any]) -> None:
        """保存名称版本并初始化语句记录。"""
        self._values = values
        self.statements: list[Any] = []

    def scalars(self, statement: Any) -> _Rows:
        """记录名称查询并返回预设版本。"""
        self.statements.append(statement)
        return _Rows(self._values)


class _EmptyStatusSession:
    """记录状态 SQL，并让所有 publication 查询返回空。"""

    def __init__(self) -> None:
        """初始化语句记录。"""
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> _Rows:
        """记录集合查询并返回空行。"""
        self.statements.append(statement)
        return _Rows([])

    def scalars(self, statement: Any) -> _Rows:
        """记录标量集合查询并返回空行。"""
        self.statements.append(statement)
        return _Rows([])

    def scalar(self, statement: Any) -> None:
        """记录单值查询并返回不存在。"""
        self.statements.append(statement)
        return None


def _identity_pair(
    *,
    security_id: int = 1,
    effective_from: date = date(2000, 1, 1),
    effective_to: date | None = None,
) -> tuple[SimpleNamespace, SimpleNamespace]:
    """构造永久证券与 identifier version 组合。"""
    instrument = SimpleNamespace(
        security_id=security_id,
        name=f"证券{security_id}",
        exchange="SSE",
        symbol="600000",
    )
    identifier = SimpleNamespace(
        version_id=uuid4(),
        security_id=security_id,
        exchange="SSE",
        symbol="600000",
        effective_from=effective_from,
        effective_to=effective_to,
    )
    return instrument, identifier


def test_data_status_identity_uses_as_of_and_known_at() -> None:
    """data-status 身份查询必须同时约束业务时间和知识时间。"""
    pair = _identity_pair()
    session = _IdentitySession([pair])
    identity, identifier = reader._identity(
        session,
        exchange="SSE",
        symbol="600000",
        as_of=date(2020, 1, 2),
        known_at=datetime(2021, 1, 2, tzinfo=UTC),
    )

    assert identity.security_id == identifier.security_id == 1
    sql = str(session.statements[0])
    assert "equity_identifier_version.effective_from" in sql
    assert "equity_identifier_version.effective_to" in sql
    assert "equity_identifier_version.known_from" in sql
    assert "equity_identifier_version.known_to" in sql


def test_historical_name_uses_same_as_of_and_known_at_slice() -> None:
    """历史名称必须与已选 identifier 共用业务日和知识时点，禁止回退当前兼容名称。"""
    expected = SimpleNamespace(version_id=uuid4(), name="历史证券简称")
    session = _NameSession([expected])

    resolved = reader._identity_name(
        session,
        security_id=7,
        as_of=date(2020, 1, 2),
        known_at=datetime(2021, 1, 2, tzinfo=UTC),
    )

    assert resolved is expected
    sql = str(session.statements[0])
    assert "equity_name_version.security_id" in sql
    assert "equity_name_version.effective_from" in sql
    assert "equity_name_version.effective_to" in sql
    assert "equity_name_version.known_from" in sql
    assert "equity_name_version.known_to" in sql


def test_closed_delisted_identifier_is_not_default_current_detail() -> None:
    """没有 asOf 时闭合退市 identifier 必须表现为 404。"""
    with pytest.raises(InternalProblem) as raised:
        reader._identity(
            _IdentitySession([]),
            exchange="SSE",
            symbol="600000",
            as_of=None,
            known_at=None,
        )

    assert raised.value.status == 404


def test_reused_code_returns_identity_conflict() -> None:
    """同一路径解析到两个永久证券时必须返回 409。"""
    with pytest.raises(InternalProblem) as raised:
        reader._identity(
            _IdentitySession([_identity_pair(security_id=1), _identity_pair(security_id=2)]),
            exchange="SSE",
            symbol="600000",
            as_of=date(2020, 1, 2),
            known_at=None,
        )

    assert raised.value.status == 409
    assert raised.value.code == "identity-resolution-conflict"


def test_event_as_of_resolves_old_identity_and_rejects_cross_range() -> None:
    """事件 asOf 可解析旧身份，但 start/end 触碰闭区间右端必须 409。"""
    pair = _identity_pair(
        effective_from=date(2000, 1, 1),
        effective_to=date(2020, 1, 3),
    )
    valid = {
        "asOf": date(2020, 1, 2),
        "start": date(2000, 1, 1),
        "end": date(2020, 1, 2),
        "knownAt": None,
    }
    identity, _identifier = reader._identity_for_event_window(
        _IdentitySession([pair]),
        exchange="SSE",
        symbol="600000",
        request=valid,
    )
    assert identity.security_id == 1

    with pytest.raises(InternalProblem) as raised:
        reader._identity_for_event_window(
            _IdentitySession([pair]),
            exchange="SSE",
            symbol="600000",
            request={**valid, "end": date(2020, 1, 3)},
        )

    assert raised.value.status == 409


def test_event_without_as_of_rejects_window_spanning_code_reuse() -> None:
    """兼容请求未带 asOf 时，跨两个 identifier range 的窗口必须 409。"""
    with pytest.raises(InternalProblem) as raised:
        reader._identity_for_event_window(
            _IdentitySession(
                [
                    _identity_pair(
                        security_id=1,
                        effective_from=date(2000, 1, 1),
                        effective_to=date(2010, 1, 1),
                    ),
                    _identity_pair(
                        security_id=2,
                        effective_from=date(2010, 1, 1),
                        effective_to=None,
                    ),
                ]
            ),
            exchange="SSE",
            symbol="600000",
            request={
                "asOf": None,
                "start": date(2005, 1, 1),
                "end": date(2015, 1, 1),
                "knownAt": None,
            },
        )

    assert raised.value.status == 409


def test_status_contract_contains_eighteen_precise_families() -> None:
    """默认 data-status 必须覆盖十八族且使用真实专用 dataset 名。"""
    normalized = reader._validate_status({})

    assert len(normalized["families"]) == 18
    assert len(set(normalized["families"])) == 18
    assert reader._STATUS_DATASETS["FINANCIAL_INDICATOR"] == "financial.metric"
    assert reader._STATUS_DATASETS["INDUSTRY_MEMBERSHIP"] == "sector.membership.release"
    assert reader._STATUS_DATASETS["CONCEPT_MEMBERSHIP"] == "sector.membership.release"
    assert reader._STATUS_DATASETS["MONEY_FLOW"] == "money_flow.daily"


def test_financial_and_money_flow_status_never_use_generic_security_partition() -> None:
    """财务与资金流缺少精确证券证据时必须 unavailable，不能借任意全局发布。"""
    financial_session = _EmptyStatusSession()
    financial = reader._financial_dataset_status(
        financial_session,
        family="FINANCIAL_REPORT",
        security_id=91,
        as_of=date(2026, 7, 29),
        known_at=None,
    )
    financial_sql = str(financial_session.statements[0])
    assert "financial_publication.security_id" in financial_sql
    assert "financial_methodology" in financial_sql
    assert financial["availability"] == "SOURCE_UNAVAILABLE"

    money_flow_session = _EmptyStatusSession()
    money_flow = reader._money_flow_dataset_status(
        money_flow_session,
        family="MONEY_FLOW",
        security_id=91,
        as_of=date(2026, 7, 29),
        known_at=None,
    )
    money_flow_sql = str(money_flow_session.statements[0])
    assert "money_flow_series.security_id" in money_flow_sql
    assert money_flow["availability"] == "SOURCE_UNAVAILABLE"
    assert money_flow["reasonCode"] == "METHODOLOGY_NOT_FROZEN"
    assert money_flow["retryable"] is False


def test_membership_status_queries_are_security_exact() -> None:
    """申万归属状态必须把 security_id 写入事实查询，空证据时 fail-closed。"""
    session = _EmptyStatusSession()
    availability = cast(
        EquityDiscoveryAvailability,
        SimpleNamespace(
            component_data_version=uuid4(),
            source_label="SW",
            methodology=None,
        ),
    )
    status = reader._sw_membership_dataset_status(
        session,
        family="SW_INDUSTRY_MEMBERSHIP",
        security_id=91,
        as_of=date(2026, 7, 29),
        known_at=None,
        availability_row=availability,
    )

    assert "sw_membership_item.security_id" in str(session.statements[0])
    assert "dataset_publication.data_version" in str(session.statements[0])
    assert status["availability"] == "SOURCE_UNAVAILABLE"


def test_warned_sector_release_cannot_prove_empty_membership() -> None:
    """板块 release 带质量告警时必须保持精确版本，但只能声明 PARTIAL。"""
    release_version = uuid4()
    release = SimpleNamespace(
        release_id=uuid4(),
        data_version=release_version,
        quality_status="warned",
        published_at=datetime(2026, 7, 30, 8, tzinfo=UTC),
        release_as_of=datetime(2026, 7, 29, 15, tzinfo=UTC),
    )

    class _SectorStatusSession:
        """依次返回 warned release 与无归属事实。"""

        def __init__(self) -> None:
            """初始化确定性标量响应。"""
            self._responses = [release, False]

        def scalar(self, _statement: Any) -> Any:
            """返回下一条 release 或存在性结果。"""
            return self._responses.pop(0)

    status = reader._sector_membership_dataset_status(
        _SectorStatusSession(),
        family="INDUSTRY_MEMBERSHIP",
        security_id=91,
        as_of=date(2026, 7, 29),
        known_at=None,
        availability_row=None,
    )

    assert status["dataVersion"] == str(release_version)
    assert status["availability"] == "PARTIAL"
    assert status["reasonCode"] == "QUALITY_WARNING"
    assert status["retryable"] is False


def test_sw_status_marks_discovery_membership_stale_when_leaf_advances() -> None:
    """SW leaf 更新但 discovery 未重建时，旧归属只能返回 `PARTIAL/STALE`。"""
    bound_version = uuid4()
    latest_version = uuid4()
    bound = SimpleNamespace(
        publication_id=uuid4(),
        data_version=bound_version,
        quality_status="passed",
        published_at=datetime(2026, 7, 29, 8, tzinfo=UTC),
        effective_as_of=date(2026, 7, 28),
        knowledge_cutoff=datetime(2026, 7, 29, 7, 30, tzinfo=UTC),
    )
    latest = SimpleNamespace(
        publication_id=uuid4(),
        data_version=latest_version,
        quality_status="passed",
        published_at=datetime(2026, 7, 30, 8, tzinfo=UTC),
        effective_as_of=date(2026, 7, 29),
        knowledge_cutoff=datetime(2026, 7, 30, 7, 30, tzinfo=UTC),
    )

    class _SwStatusSession:
        """依次返回 discovery 绑定发布与最新 leaf 发布。"""

        def __init__(self) -> None:
            """初始化响应队列和 SQL 记录。"""
            self._responses = [bound, latest]
            self.statements: list[Any] = []

        def scalar(self, statement: Any) -> Any:
            """记录 SQL 并返回下一条 publication。"""
            self.statements.append(statement)
            return self._responses.pop(0)

    availability = cast(
        EquityDiscoveryAvailability,
        SimpleNamespace(
            component_data_version=bound_version,
            source_label="SW",
            methodology={"code": "sw2021", "version": "1"},
        ),
    )

    status = reader._sw_membership_dataset_status(
        _SwStatusSession(),
        family="SW_INDUSTRY_MEMBERSHIP",
        security_id=91,
        as_of=None,
        known_at=None,
        availability_row=availability,
    )

    assert status["dataVersion"] == str(bound_version)
    assert status["availability"] == "PARTIAL"
    assert status["freshness"] == "STALE"
    assert status["reasonCode"] == "DISCOVERY_COMPONENT_STALE"
    assert status["retryable"] is True


@pytest.mark.parametrize(
    "family",
    [
        "CORPORATE_ACTION",
        "EARNINGS_FORECAST",
        "DRAGON_TIGER",
        "BLOCK_TRADE",
    ],
)
def test_event_status_requires_point_coverage_at_known_at(family: str) -> None:
    """事件状态必须按身份、业务日和知识时点读取 coverage，不能拿已有事实充数。"""
    session = _EmptyStatusSession()
    identifier_version_id = uuid4()

    status = reader._event_dataset_status(
        session,
        family=family,
        security_id=91,
        identifier_version_id=identifier_version_id,
        as_of=date(2026, 7, 29),
        known_at=datetime(2026, 7, 30, 8, tzinfo=UTC),
    )

    sql = str(session.statements[0])
    assert "equity_event_window_coverage" in sql
    assert "equity_event_window_coverage.identifier_version_id" in sql
    assert "equity_event_window_coverage.created_at" in sql
    assert "equity_event_window_coverage.superseded_at" in sql
    assert identifier_version_id in session.statements[0].compile().params.values()
    assert status["availability"] == "SOURCE_UNAVAILABLE"
    assert status["reasonCode"] == "NO_COVERAGE"


def test_status_composite_version_changes_with_component_set() -> None:
    """十八族任一实际状态变化都必须改变响应复合版本和 ETag 输入。"""
    first = reader._composite_version(
        "equity-data-status",
        {"datasets": [{"family": "BARS_1D", "dataVersion": str(uuid4())}]},
    )
    second = reader._composite_version(
        "equity-data-status",
        {"datasets": [{"family": "BARS_1D", "dataVersion": str(uuid4())}]},
    )

    assert first != second


def test_matching_discovery_metadata_requires_same_component_version() -> None:
    """来源标签只可从同一组件版本复用，不能借当前 discovery 装饰旧数据。"""
    version = uuid4()
    row = cast(
        EquityDiscoveryAvailability,
        SimpleNamespace(
            component_data_version=version,
            source_label="source",
            methodology={"code": "method", "version": "1"},
        ),
    )

    assert reader._matching_discovery_metadata(row, data_version=version) == (
        "source",
        {"code": "method", "version": "1"},
    )
    assert reader._matching_discovery_metadata(row, data_version=uuid4()) == (None, None)


def test_event_request_accepts_independent_as_of() -> None:
    """事件合同必须把 date-only asOf 与业务筛选窗口分开解析。"""
    normalized = reader._validate_events(
        {
            "families": ["CORPORATE_ACTION"],
            "asOf": "2020-01-02",
            "start": "2019-01-01",
            "end": "2020-01-02",
            "knownAt": "2021-01-01T00:00:00Z",
            "limit": 20,
        }
    )

    assert normalized["asOf"] == date(2020, 1, 2)
    assert normalized["knownAt"] == datetime(2021, 1, 1, tzinfo=UTC)
