"""财务生产发布选择器的边界测试。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy.exc import SQLAlchemyError

from service_data_sync.application.ports.financial_read import (
    FinancialCapability,
    FinancialPublicationSnapshot,
    FinancialReadUnavailable,
    PublishedFinancialReportDetail,
)
from service_data_sync.domain.equity import Exchange
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.financial_read_repository import (
    SqlAlchemyFinancialReadRepository,
)

_DATA_VERSION = UUID("10000000-0000-4000-8000-000000000016")
_METHODOLOGY_ID = UUID("20000000-0000-4000-8000-000000000016")


class FakeResult:
    """模拟发布选择查询使用的 `SQLAlchemy` 映射结果。"""

    def __init__(self, value: object) -> None:
        """保存单行、空值或待抛出的确定性响应。"""
        self._value = value

    def mappings(self) -> FakeResult:
        """测试响应已使用映射形式，直接返回当前替身。"""
        return self

    def one_or_none(self) -> object:
        """返回唯一发布行或空值。"""
        return self._value

    def all(self) -> list[Mapping[str, object]]:
        """返回报表头查询的映射行集合。"""
        assert isinstance(self._value, list)
        return self._value


class FakeConnection:
    """记录只读 `SQL` 并按顺序回放发布选择响应。"""

    def __init__(self, responses: list[object]) -> None:
        """初始化共享响应队列与语句记录。"""
        self._responses = responses
        self.statements: list[str] = []
        self.parameters: list[Mapping[str, object]] = []

    def execute(
        self,
        statement: object,
        parameters: Mapping[str, object] | None = None,
    ) -> FakeResult:
        """记录语句和绑定值，并在指定时模拟基础设施失败。"""
        self.statements.append(str(statement))
        self.parameters.append({} if parameters is None else parameters)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return FakeResult(response)


class FakeEngine:
    """提供仓储短生命周期 `Session` 所需的最小连接形状。"""

    def __init__(self, responses: list[object]) -> None:
        """构造所有 `Session` 复用的无网络替身连接。"""
        self.connection = FakeConnection(responses)

    @contextmanager
    def connect(self) -> Iterator[FakeConnection]:
        """产出当前替身连接，不开启真实数据库事务。"""
        yield self.connection


class FakeDatabase:
    """通过服务的 `Session` 边界暴露替身连接。"""

    def __init__(self, engine: FakeEngine) -> None:
        """保存承载确定性响应的替身引擎。"""
        self._engine = engine

    @contextmanager
    def session(self) -> Iterator[FakeConnection]:
        """提供一次只读查询所需的模拟 `Session`。"""
        with self._engine.connect() as connection:
            yield connection


def test_repository_selects_only_date_aware_current_validated_publication() -> None:
    """选择器按请求双时间锁定证券，再绑定能力、方法学和当前 `data_version`。"""
    engine = FakeEngine([_publication_row()])
    repository = _repository(engine)

    publication = repository.get_current_publication(
        exchange=Exchange.SSE,
        symbol="600519",
        capability="financial.report",
        methodology_code="eastmoney.reported",
        methodology_version=2,
        as_of=date(2026, 7, 27),
        known_at=datetime(2026, 7, 28, 8, tzinfo=UTC),
    )

    assert publication is not None
    assert publication.data_version == _DATA_VERSION
    assert publication.instrument_id == UUID("30000000-0000-4000-8000-000000000016")
    assert publication.methodology_id == _METHODOLOGY_ID
    assert publication.capability == "financial.report"
    assert publication.knowledge_cutoff == datetime(2026, 7, 28, 8, tzinfo=UTC)
    assert publication.source_code == "eastmoney"
    statement = engine.connection.statements[0]
    assert "financial_publication" in statement
    assert "financial_methodology" in statement
    assert "equity_instrument" in statement
    assert "equity_identifier_version" in statement
    assert "effective_range @> financial_publication.effective_as_of" not in statement
    assert "knowledge_range @> financial_publication.knowledge_cutoff" not in statement
    assert "equity_identifier_version.identity_state = :identity_state_1" in statement
    assert "equity_identifier_version.effective_from <= :effective_from_1" in statement
    assert "equity_identifier_version.effective_to > :effective_to_1" in statement
    assert "equity_identifier_version.known_from <= :known_from_1" in statement
    assert "equity_identifier_version.known_to > :known_to_1" in statement
    assert "equity_instrument.exchange =" not in statement
    assert "equity_instrument.symbol =" not in statement
    assert "dataset_publication.superseded_at IS NULL" in statement
    assert "financial_methodology.status = :status_1" in statement
    assert engine.connection.parameters == [{}]


def test_repository_returns_none_when_no_current_publication_exists() -> None:
    """不存在当前生产发布时返回空值，调用方不得降级到 `canonical` 或 `research` 行。"""
    engine = FakeEngine([None])
    repository = _repository(engine)

    publication = repository.get_current_publication(
        exchange=Exchange.SZSE,
        symbol="000001",
        capability="financial.valuation",
        methodology_code="candidate.valuation",
        methodology_version=1,
    )

    assert publication is None


def test_repository_resolves_report_reference_to_its_current_publication() -> None:
    """详情读取只能从公开 `reportRef` 反查其所属生产发布，不能接受调用方猜测方法学。"""
    engine = FakeEngine([_publication_row()])
    repository = _repository(engine)

    publication = repository.get_current_report_publication(
        exchange=Exchange.SSE,
        symbol="600519",
        report_ref=UUID("50000000-0000-4000-8000-000000000016"),
    )

    assert publication is not None
    assert publication.data_version == _DATA_VERSION
    statement = engine.connection.statements[0]
    assert "financial_report.report_ref" in statement
    assert "financial_publication.capability" in statement
    assert "effective_range @> financial_publication.effective_as_of" not in statement
    assert "knowledge_range @> financial_publication.knowledge_cutoff" not in statement


def test_repository_maps_storage_failure_to_fail_closed_port_error() -> None:
    """数据库不可读时返回稳定端口错误，接口层可统一投影为不泄漏细节的 503。"""
    engine = FakeEngine([SQLAlchemyError("database unavailable")])
    repository = _repository(engine)

    with pytest.raises(FinancialReadUnavailable):
        repository.get_current_publication(
            exchange=Exchange.BSE,
            symbol="430047",
            capability="financial.provider-metric",
            methodology_code="candidate.metric",
            methodology_version=1,
        )


def test_repository_lists_only_visible_published_report_headers() -> None:
    """报表页必须绑定已选发布、双时态截点、质量状态和完整复合游标键。"""
    engine = FakeEngine([_publication_row(), [_report_row()]])
    repository = _repository(engine)
    publication = repository.get_current_publication(
        exchange=Exchange.SSE,
        symbol="600519",
        capability="financial.report",
        methodology_code="eastmoney.reported",
        methodology_version=2,
    )
    assert publication is not None

    reports = repository.list_reports(
        publication=publication,
        as_of=date(2026, 7, 27),
        known_at=datetime(2026, 7, 28, 8, tzinfo=UTC),
        statement_types=("INCOME_STATEMENT",),
        period_bases=("YEAR_TO_DATE",),
        statement_scope="CONSOLIDATED",
        report_period_from=date(2025, 1, 1),
        report_period_to=date(2026, 6, 30),
        after_report_period=date(2026, 3, 31),
        after_statement_type="INCOME_STATEMENT",
        after_report_ref=UUID("40000000-0000-4000-8000-000000000016"),
        limit=2,
    )

    assert len(reports) == 1
    assert reports[0].report_ref == UUID("50000000-0000-4000-8000-000000000016")
    assert reports[0].quality_status == "passed"
    statement = engine.connection.statements[1]
    assert "financial_report_revision.quality_status IN" in statement
    assert "financial_report_revision.effective_from <=" in statement
    assert "financial_report_revision.known_from <=" in statement
    assert "financial_report.report_period <" in statement
    assert "ORDER BY financial_report.report_period DESC" in statement


def test_repository_rejects_incomplete_report_cursor_before_database_access() -> None:
    """不完整复合键不能退化成非稳定续页，也不得发起无意义数据库查询。"""
    engine = FakeEngine([])
    repository = _repository(engine)

    with pytest.raises(ValueError, match="cursor keys"):
        repository.list_reports(
            publication=_publication_snapshot(),
            as_of=date(2026, 7, 27),
            known_at=datetime(2026, 7, 28, 8, tzinfo=UTC),
            statement_types=(),
            period_bases=(),
            statement_scope=None,
            report_period_from=None,
            report_period_to=None,
            after_report_period=date(2026, 3, 31),
            after_statement_type=None,
            after_report_ref=None,
            limit=2,
        )

    assert engine.connection.statements == []


def test_repository_reads_visible_detail_and_active_governed_facts() -> None:
    """详情必须选择同一可见 revision，并只返回 active 字典认可的稳定字段顺序。"""
    engine = FakeEngine([_report_detail_row(), [_statement_fact_row()]])
    repository = _repository(engine)

    detail = repository.get_report_detail(
        publication=_publication_snapshot(),
        report_ref=UUID("50000000-0000-4000-8000-000000000016"),
        as_of=date(2026, 7, 27),
        known_at=datetime(2026, 7, 28, 8, tzinfo=UTC),
    )

    assert detail is not None
    facts = repository.list_report_facts(
        detail=detail,
        metric_codes=("assets",),
        after_metric_code="accounts_receivable",
        limit=2,
    )

    assert detail.revision_id == UUID("60000000-0000-4000-8000-000000000016")
    assert facts[0].metric_code == "assets"
    assert str(facts[0].value) == "123.45"
    detail_statement = engine.connection.statements[0]
    fact_statement = engine.connection.statements[1]
    assert "financial_report.report_ref" in detail_statement
    assert "financial_report_revision.quality_status IN" in detail_statement
    assert "financial_metric_definition.status" in fact_statement
    assert "financial_metric_definition.origin" in fact_statement
    assert "financial_metric_definition.code >" in fact_statement
    assert "ORDER BY financial_metric_definition.code" in fact_statement


def test_repository_lists_visible_provider_metrics_in_contract_order() -> None:
    """供应商指标查询必须保留发布、双时态、质量和报告期升序复合游标边界。"""
    engine = FakeEngine([[_provider_metric_row()]])
    repository = _repository(engine)

    metrics = repository.list_provider_metrics(
        publication=_publication_snapshot(),
        as_of=date(2026, 7, 27),
        known_at=datetime(2026, 7, 28, 8, tzinfo=UTC),
        metric_codes=("net_income",),
        period_bases=("YEAR_TO_DATE",),
        report_period_from=date(2026, 1, 1),
        report_period_to=date(2026, 6, 30),
        after_report_period=date(2026, 3, 31),
        after_metric_code="ebit",
        limit=2,
    )

    assert metrics[0].metric_code == "net_income"
    assert metrics[0].origin == "PROVIDER_REPORTED"
    assert str(metrics[0].value) == "12.34"
    statement = engine.connection.statements[0]
    assert "provider_financial_metric_revision.quality_status IN" in statement
    assert "provider_financial_metric_revision.report_period >" in statement
    expected_order = (
        "ORDER BY provider_financial_metric_revision.report_period, "
        "financial_metric_definition.code"
    )
    assert expected_order in statement


def test_repository_rejects_provider_publication_for_derived_metric_read() -> None:
    """供应商 publication 不能读取平台派生表，防止来源边界被调用方误接线。"""
    engine = FakeEngine([])
    repository = _repository(engine)

    with pytest.raises(ValueError, match="derived publication"):
        repository.list_derived_metrics(
            publication=_publication_snapshot(),
            as_of=date(2026, 7, 27),
            known_at=datetime(2026, 7, 28, 8, tzinfo=UTC),
            metric_codes=("platform.operating_revenue.ttm",),
            period_bases=("TTM",),
            report_period_from=None,
            report_period_to=None,
            after_report_period=None,
            after_metric_code=None,
            limit=2,
        )

    assert engine.connection.statements == []


def test_repository_lists_visible_derived_metrics_with_formula_version() -> None:
    """派生查询必须只读平台字典、保留公式版本并按报告期与代码稳定排序。"""
    engine = FakeEngine([[_derived_metric_row()]])
    repository = _repository(engine)

    metrics = repository.list_derived_metrics(
        publication=_derived_publication_snapshot(),
        as_of=date(2026, 7, 27),
        known_at=datetime(2026, 7, 28, 8, tzinfo=UTC),
        metric_codes=("platform.operating_revenue.ttm",),
        period_bases=("TTM",),
        report_period_from=date(2025, 1, 1),
        report_period_to=date(2026, 6, 30),
        after_report_period=date(2026, 3, 31),
        after_metric_code="platform.net_profit_parent.ttm",
        limit=2,
    )

    assert metrics[0].metric_code == "platform.operating_revenue.ttm"
    assert metrics[0].origin == "PLATFORM_DERIVED"
    assert metrics[0].formula_version == 1
    assert str(metrics[0].value) == "600.00"
    statement = engine.connection.statements[0]
    assert "derived_financial_metric_revision.quality_status IN" in statement
    assert "financial_metric_definition.origin =" in statement
    assert "derived_financial_metric_revision.report_period >" in statement
    expected_order = (
        "ORDER BY derived_financial_metric_revision.report_period, financial_metric_definition.code"
    )
    assert expected_order in statement


def test_repository_lists_visible_valuations_in_contract_order() -> None:
    """历史估值查询必须绑定生产发布的日频可见 revision 与日期字段复合游标。"""
    engine = FakeEngine([[_valuation_row()]])
    repository = _repository(engine)

    valuations = repository.list_valuations(
        publication=_publication_snapshot(),
        as_of=date(2026, 7, 27),
        known_at=datetime(2026, 7, 28, 8, tzinfo=UTC),
        metric_codes=("pe_ttm",),
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
        after_observation_date=date(2026, 7, 26),
        after_metric_code="pb",
        limit=2,
    )

    assert valuations[0].metric_code == "pe_ttm"
    assert valuations[0].finality == "PROVIDER_OBSERVATION"
    assert str(valuations[0].value) == "18.25"
    statement = engine.connection.statements[0]
    assert "valuation_observation_revision.quality_status IN" in statement
    assert "valuation_observation_revision.observation_date >" in statement
    assert (
        "ORDER BY valuation_observation_revision.observation_date, financial_metric_definition.code"
        in statement
    )


def test_repository_rejects_invalid_limits_and_partial_composite_cursors() -> None:
    """所有财务列表必须在访问数据库前拒绝越界页长和不完整复合游标。"""
    repository = _repository(FakeEngine([]))
    detail = _detail()
    as_of = date(2026, 7, 27)
    known_at = datetime(2026, 7, 28, 8, tzinfo=UTC)

    with pytest.raises(ValueError, match="1 to 51"):
        repository.list_reports(
            publication=_publication_snapshot(),
            as_of=as_of,
            known_at=known_at,
            statement_types=(),
            period_bases=(),
            statement_scope=None,
            report_period_from=None,
            report_period_to=None,
            after_report_period=None,
            after_statement_type=None,
            after_report_ref=None,
            limit=0,
        )
    with pytest.raises(ValueError, match="1 to 201"):
        repository.list_report_facts(
            detail=detail,
            metric_codes=(),
            after_metric_code=None,
            limit=202,
        )
    with pytest.raises(ValueError, match="1 to 501"):
        repository.list_provider_metrics(
            publication=_publication_snapshot(),
            as_of=as_of,
            known_at=known_at,
            metric_codes=(),
            period_bases=(),
            report_period_from=None,
            report_period_to=None,
            after_report_period=None,
            after_metric_code=None,
            limit=0,
        )
    with pytest.raises(ValueError, match="cursor keys"):
        repository.list_provider_metrics(
            publication=_publication_snapshot(),
            as_of=as_of,
            known_at=known_at,
            metric_codes=(),
            period_bases=(),
            report_period_from=None,
            report_period_to=None,
            after_report_period=as_of,
            after_metric_code=None,
            limit=2,
        )
    with pytest.raises(ValueError, match="1 to 501"):
        repository.list_derived_metrics(
            publication=_derived_publication_snapshot(),
            as_of=as_of,
            known_at=known_at,
            metric_codes=(),
            period_bases=(),
            report_period_from=None,
            report_period_to=None,
            after_report_period=None,
            after_metric_code=None,
            limit=502,
        )
    with pytest.raises(ValueError, match="cursor keys"):
        repository.list_derived_metrics(
            publication=_derived_publication_snapshot(),
            as_of=as_of,
            known_at=known_at,
            metric_codes=(),
            period_bases=(),
            report_period_from=None,
            report_period_to=None,
            after_report_period=None,
            after_metric_code="platform.operating_revenue.ttm",
            limit=2,
        )
    with pytest.raises(ValueError, match="1 to 1001"):
        repository.list_valuations(
            publication=_publication_snapshot(),
            as_of=as_of,
            known_at=known_at,
            metric_codes=(),
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
            after_observation_date=None,
            after_metric_code=None,
            limit=1002,
        )
    with pytest.raises(ValueError, match="cursor keys"):
        repository.list_valuations(
            publication=_publication_snapshot(),
            as_of=as_of,
            known_at=known_at,
            metric_codes=(),
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
            after_observation_date=date(2026, 7, 26),
            after_metric_code=None,
            limit=2,
        )


def test_repository_maps_each_query_failure_to_capability_specific_port_error() -> None:
    """每个读取器都应屏蔽 SQL 异常，并返回可映射为 503 的稳定端口错误。"""
    as_of = date(2026, 7, 27)
    known_at = datetime(2026, 7, 28, 8, tzinfo=UTC)
    failure = SQLAlchemyError("database unavailable")

    with pytest.raises(FinancialReadUnavailable, match="reports are unavailable"):
        _repository(FakeEngine([failure])).list_reports(
            publication=_publication_snapshot(),
            as_of=as_of,
            known_at=known_at,
            statement_types=(),
            period_bases=(),
            statement_scope=None,
            report_period_from=None,
            report_period_to=None,
            after_report_period=None,
            after_statement_type=None,
            after_report_ref=None,
            limit=2,
        )
    with pytest.raises(FinancialReadUnavailable, match="report publication"):
        _repository(FakeEngine([failure])).get_current_report_publication(
            exchange=Exchange.SSE,
            symbol="600519",
            report_ref=UUID("50000000-0000-4000-8000-000000000016"),
        )
    with pytest.raises(FinancialReadUnavailable, match="report detail"):
        _repository(FakeEngine([failure])).get_report_detail(
            publication=_publication_snapshot(),
            report_ref=UUID("50000000-0000-4000-8000-000000000016"),
            as_of=as_of,
            known_at=known_at,
        )
    with pytest.raises(FinancialReadUnavailable, match="statement facts"):
        _repository(FakeEngine([failure])).list_report_facts(
            detail=_detail(),
            metric_codes=(),
            after_metric_code=None,
            limit=2,
        )
    with pytest.raises(FinancialReadUnavailable, match="provider metrics"):
        _repository(FakeEngine([failure])).list_provider_metrics(
            publication=_publication_snapshot(),
            as_of=as_of,
            known_at=known_at,
            metric_codes=(),
            period_bases=(),
            report_period_from=None,
            report_period_to=None,
            after_report_period=None,
            after_metric_code=None,
            limit=2,
        )
    with pytest.raises(FinancialReadUnavailable, match="derived metrics"):
        _repository(FakeEngine([failure])).list_derived_metrics(
            publication=_derived_publication_snapshot(),
            as_of=as_of,
            known_at=known_at,
            metric_codes=(),
            period_bases=(),
            report_period_from=None,
            report_period_to=None,
            after_report_period=None,
            after_metric_code=None,
            limit=2,
        )
    with pytest.raises(FinancialReadUnavailable, match="valuations"):
        _repository(FakeEngine([failure])).list_valuations(
            publication=_publication_snapshot(),
            as_of=as_of,
            known_at=known_at,
            metric_codes=(),
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
            after_observation_date=None,
            after_metric_code=None,
            limit=2,
        )


def test_repository_empty_results_keep_all_optional_filters_unset() -> None:
    """空生产视图应稳定返回空值或空页，且不要求调用方伪造筛选和游标。"""
    engine = FakeEngine(
        [
            None,
            None,
            _report_detail_row(),
            [],
            [],
            [],
            [],
            [],
        ]
    )
    repository = _repository(engine)
    as_of = date(2026, 7, 27)
    known_at = datetime(2026, 7, 28, 8, tzinfo=UTC)

    assert (
        repository.get_current_report_publication(
            exchange=Exchange.SSE,
            symbol="600519",
            report_ref=UUID("50000000-0000-4000-8000-000000000016"),
        )
        is None
    )
    assert (
        repository.get_report_detail(
            publication=_publication_snapshot(),
            report_ref=UUID("50000000-0000-4000-8000-000000000099"),
            as_of=as_of,
            known_at=known_at,
        )
        is None
    )
    detail = repository.get_report_detail(
        publication=_publication_snapshot(),
        report_ref=UUID("50000000-0000-4000-8000-000000000016"),
        as_of=as_of,
        known_at=known_at,
    )
    assert detail is not None
    assert (
        repository.list_reports(
            publication=_publication_snapshot(),
            as_of=as_of,
            known_at=known_at,
            statement_types=(),
            period_bases=(),
            statement_scope=None,
            report_period_from=None,
            report_period_to=None,
            after_report_period=None,
            after_statement_type=None,
            after_report_ref=None,
            limit=2,
        )
        == ()
    )
    assert (
        repository.list_report_facts(
            detail=detail,
            metric_codes=(),
            after_metric_code=None,
            limit=2,
        )
        == ()
    )
    assert (
        repository.list_provider_metrics(
            publication=_publication_snapshot(),
            as_of=as_of,
            known_at=known_at,
            metric_codes=(),
            period_bases=(),
            report_period_from=None,
            report_period_to=None,
            after_report_period=None,
            after_metric_code=None,
            limit=2,
        )
        == ()
    )
    assert (
        repository.list_derived_metrics(
            publication=_derived_publication_snapshot(),
            as_of=as_of,
            known_at=known_at,
            metric_codes=(),
            period_bases=(),
            report_period_from=None,
            report_period_to=None,
            after_report_period=None,
            after_metric_code=None,
            limit=2,
        )
        == ()
    )
    assert (
        repository.list_valuations(
            publication=_publication_snapshot(),
            as_of=as_of,
            known_at=known_at,
            metric_codes=(),
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
            after_observation_date=None,
            after_metric_code=None,
            limit=2,
        )
        == ()
    )


def _repository(engine: FakeEngine) -> SqlAlchemyFinancialReadRepository:
    """围绕带短 `Session` 边界的替身数据库构造财务读取仓储。"""
    return SqlAlchemyFinancialReadRepository(cast(DatabaseClient, FakeDatabase(engine)))


def _detail() -> PublishedFinancialReportDetail:
    """构造可供行项目查询使用的已发布报表详情。"""
    detail = _repository(FakeEngine([_report_detail_row()])).get_report_detail(
        publication=_publication_snapshot(),
        report_ref=UUID("50000000-0000-4000-8000-000000000016"),
        as_of=date(2026, 7, 27),
        known_at=datetime(2026, 7, 28, 8, tzinfo=UTC),
    )
    assert detail is not None
    return detail


def _publication_row() -> dict[str, object]:
    """构造一行已验证且未被替代发布的完整投影。"""
    return {
        "data_version": _DATA_VERSION,
        "security_id": 8,
        "instrument_id": UUID("30000000-0000-4000-8000-000000000016"),
        "methodology_id": _METHODOLOGY_ID,
        "capability": "financial.report",
        "methodology_code": "eastmoney.reported",
        "methodology_version": 2,
        "source_code": "eastmoney",
        "published_at": datetime(2026, 7, 28, 8, 5, tzinfo=UTC),
        "effective_as_of": date(2026, 7, 27),
        "knowledge_cutoff": datetime(2026, 7, 28, 8, tzinfo=UTC),
        "row_count": 12,
        "content_sha256": "a" * 64,
    }


def _publication_snapshot() -> FinancialPublicationSnapshot:
    """从测试发布行构造端口快照，供游标参数校验测试复用。"""
    row = _publication_row()
    return FinancialPublicationSnapshot(
        data_version=cast(UUID, row["data_version"]),
        security_id=cast(int, row["security_id"]),
        instrument_id=cast(UUID, row["instrument_id"]),
        methodology_id=cast(UUID, row["methodology_id"]),
        capability=cast(FinancialCapability, row["capability"]),
        methodology_code=cast(str, row["methodology_code"]),
        methodology_version=cast(int, row["methodology_version"]),
        source_code=cast(str, row["source_code"]),
        published_at=cast(datetime, row["published_at"]),
        effective_as_of=cast(date, row["effective_as_of"]),
        knowledge_cutoff=cast(datetime, row["knowledge_cutoff"]),
        row_count=cast(int, row["row_count"]),
        content_sha256=cast(str, row["content_sha256"]),
    )


def _derived_publication_snapshot() -> FinancialPublicationSnapshot:
    """构造独立平台派生 publication，禁止测试误用供应商指标版本。"""
    return replace(
        _publication_snapshot(),
        capability="financial.derived-metric",
        methodology_code="platform.financial-derivation",
        methodology_version=1,
        source_code="quant-v2.platform",
    )


def _report_row() -> dict[str, object]:
    """构造一条已通过质量检查且位于可见双时态范围的报表头。"""
    return {
        "report_ref": UUID("50000000-0000-4000-8000-000000000016"),
        "statement_type": "INCOME_STATEMENT",
        "report_period": date(2026, 3, 31),
        "period_basis": "YEAR_TO_DATE",
        "statement_scope": "CONSOLIDATED",
        "currency": "CNY",
        "currency_null_reason": None,
        "report_type": "QUARTERLY",
        "audit_status": "UNAUDITED",
        "announcement_date": date(2026, 4, 28),
        "provider_update_at": datetime(2026, 4, 28, 8, tzinfo=UTC),
        "effective_from": date(2026, 4, 28),
        "effective_to": None,
        "known_from": datetime(2026, 4, 28, 8, tzinfo=UTC),
        "known_to": None,
        "knowledge_basis": "ANNOUNCEMENT",
        "knowledge_confidence": "HIGH",
        "observed_at": datetime(2026, 4, 28, 8, tzinfo=UTC),
        "revision": 1,
        "quality_status": "passed",
    }


def _report_detail_row() -> dict[str, object]:
    """构造带内部 revision UUID 的详情头投影，不把该 UUID 暴露给接口响应。"""
    return {
        **_report_row(),
        "revision_id": UUID("60000000-0000-4000-8000-000000000016"),
    }


def _statement_fact_row() -> dict[str, object]:
    """构造一个激活字典认可的精确行项目 SQL 投影。"""
    return {
        "metric_code": "assets",
        "label": "资产合计",
        "value": Decimal("123.45"),
        "null_reason": None,
        "currency": "CNY",
        "currency_null_reason": None,
        "original_unit": "yuan",
        "canonical_unit": "yuan",
        "scale_factor": Decimal("1.0000"),
        "sign_convention": "AS_REPORTED",
    }


def _provider_metric_row() -> dict[str, object]:
    """构造供应商直接指标的映射行，覆盖数值、币种未知和观测血缘投影。"""
    return {
        "metric_code": "net_income",
        "label": "净利润",
        "report_period": date(2026, 3, 31),
        "period_basis": "YEAR_TO_DATE",
        "statement_scope": "UNKNOWN",
        "value": Decimal("12.34"),
        "unit": "source_unknown",
        "currency": None,
        "currency_null_reason": "UNKNOWN_SOURCE",
        "effective_from": date(2026, 3, 31),
        "known_from": datetime(2026, 4, 28, 8, tzinfo=UTC),
        "knowledge_basis": "OBSERVED_AT",
        "knowledge_confidence": "CONSERVATIVE",
        "observed_at": datetime(2026, 4, 28, 8, tzinfo=UTC),
        "revision": 1,
    }


def _derived_metric_row() -> dict[str, object]:
    """构造带公式版本的平台派生指标映射行。"""
    return {
        **_provider_metric_row(),
        "metric_code": "platform.operating_revenue.ttm",
        "label": "营业收入（TTM）",
        "period_basis": "TTM",
        "statement_scope": "CONSOLIDATED",
        "value": Decimal("600.00"),
        "unit": "yuan",
        "currency": "CNY",
        "currency_null_reason": None,
        "formula_version": 1,
    }


def _valuation_row() -> dict[str, object]:
    """构造历史估值映射行，覆盖非最终标记和不适用币种原因。"""
    return {
        "observation_date": date(2026, 7, 27),
        "metric_code": "pe_ttm",
        "value": Decimal("18.25"),
        "unit": "ratio",
        "currency": None,
        "currency_null_reason": "NOT_APPLICABLE",
        "finality": "PROVIDER_OBSERVATION",
        "effective_from": date(2026, 7, 27),
        "known_from": datetime(2026, 7, 28, 8, tzinfo=UTC),
        "knowledge_basis": "OBSERVED_AT",
        "knowledge_confidence": "CONSERVATIVE",
        "observed_at": datetime(2026, 7, 28, 8, tzinfo=UTC),
        "revision": 1,
    }
