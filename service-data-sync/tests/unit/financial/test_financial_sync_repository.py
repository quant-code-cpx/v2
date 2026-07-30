"""财务 canonical 写入仓储的发布摘要与幂等分支测试。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from unittest.mock import Mock
from uuid import NAMESPACE_URL, UUID, uuid5

from pytest import MonkeyPatch
from sqlalchemy.orm import Session

from service_data_sync.application.ports.canonical_release import PublishedCanonicalRelease
from service_data_sync.application.ports.financial_sync import (
    FinancialCapability,
    FinancialFactInput,
    FinancialMetricInput,
    FinancialPublicationResult,
    FinancialReportInput,
    FinancialSourceObservation,
    FinancialValuationInput,
)
from service_data_sync.domain.equity import Exchange
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.financial.financial_report_revision import (
    FinancialReportRevision,
)
from service_data_sync.infrastructure.persistence import financial_sync_repository
from service_data_sync.infrastructure.persistence.financial_sync_repository import (
    SqlAlchemyFinancialSyncRepository,
    _content_hash,
)


class FakeResult:
    """提供仓储私有查询所需的 scalar 与 mapping 结果表面。"""

    def __init__(self, value: object) -> None:
        """保存单值、空值或映射行列表。"""
        self._value = value

    def scalar_one_or_none(self) -> object:
        """返回允许为空的唯一结果。"""
        return self._value

    def scalar_one(self) -> object:
        """返回必须存在的唯一结果。"""
        return self._value

    def mappings(self) -> FakeResult:
        """将当前替身作为映射结果读取器返回。"""
        return self

    def one_or_none(self) -> object:
        """返回允许为空的唯一映射行。"""
        return self._value

    def all(self) -> list[dict[str, object]]:
        """返回预置的映射行列表。"""
        assert isinstance(self._value, list)
        return self._value


class FakeSession:
    """按调用顺序回放 SQLAlchemy 查询结果，并记录执行过的语句。"""

    def __init__(self, responses: list[object]) -> None:
        """初始化响应队列与语句记录。"""
        self._responses = responses
        self.statements: list[str] = []

    def execute(self, statement: object) -> FakeResult:
        """记录仓储语句并返回下一个确定性结果。"""
        self.statements.append(str(statement))
        return FakeResult(self._responses.pop(0))


class FakeTransaction:
    """提供仓储公开写入入口所需的最小事务上下文。"""

    def __init__(self, session: Session) -> None:
        """保存整个公开写入流程共用的伪会话。"""
        self._session = session

    def __enter__(self) -> Session:
        """进入事务时交出预置会话。"""
        return self._session

    def __exit__(self, *_: object) -> bool:
        """模拟正常结束，不吞没仓储写入异常。"""
        return False


class FakeDatabase:
    """将公开仓储入口限定在一个可断言的内存事务内。"""

    def __init__(self, session: Session) -> None:
        """保存每次事务都应返回的会话。"""
        self._session = session

    def transaction(self) -> FakeTransaction:
        """返回不提交真实数据库的事务替身。"""
        return FakeTransaction(self._session)


def test_publication_state_hashes_all_current_rows_not_only_latest_input() -> None:
    """三类发布摘要必须覆盖当前完整视图，增量重跑不能丢失先前 canonical 行。"""
    repository = _repository()
    session = FakeSession(
        [
            [
                {
                    "report_ref": UUID("50000000-0000-4000-8000-000000000001"),
                    "statement_type": "BALANCE_SHEET",
                    "report_period": date(2026, 3, 31),
                    "period_basis": "POINT_IN_TIME",
                    "statement_scope": "UNKNOWN",
                    "effective_from": date(2026, 4, 28),
                    "revision": 1,
                    "content_sha256": "a" * 64,
                },
                {
                    "report_ref": UUID("50000000-0000-4000-8000-000000000002"),
                    "statement_type": "INCOME_STATEMENT",
                    "report_period": date(2026, 3, 31),
                    "period_basis": "YEAR_TO_DATE",
                    "statement_scope": "UNKNOWN",
                    "effective_from": date(2026, 4, 29),
                    "revision": 1,
                    "content_sha256": "b" * 64,
                },
            ],
            [
                {
                    "report_period": date(2026, 3, 31),
                    "metric_id": 11,
                    "period_basis": "YEAR_TO_DATE",
                    "statement_scope": "UNKNOWN",
                    "revision": 1,
                    "content_sha256": "c" * 64,
                },
                {
                    "report_period": date(2026, 6, 30),
                    "metric_id": 11,
                    "period_basis": "YEAR_TO_DATE",
                    "statement_scope": "UNKNOWN",
                    "revision": 1,
                    "content_sha256": "d" * 64,
                },
            ],
            [
                {
                    "observation_date": date(2026, 7, 26),
                    "metric_id": 12,
                    "revision": 1,
                    "content_sha256": "e" * 64,
                },
                {
                    "observation_date": date(2026, 7, 27),
                    "metric_id": 12,
                    "revision": 1,
                    "content_sha256": "f" * 64,
                },
            ],
        ]
    )

    report_state = repository._report_publication_state(
        cast(Session, session),
        security_id=8,
        methodology_id=_METHODOLOGY_ID,
    )
    metric_state = repository._provider_metric_publication_state(
        cast(Session, session),
        security_id=8,
        methodology_id=_METHODOLOGY_ID,
    )
    valuation_state = repository._valuation_publication_state(
        cast(Session, session),
        security_id=8,
        methodology_id=_METHODOLOGY_ID,
    )

    assert report_state[:2] == (date(2026, 4, 29), 2)
    assert metric_state[:2] == (date(2026, 6, 30), 2)
    assert valuation_state[:2] == (date(2026, 7, 27), 2)
    assert all(len(state[2]) == 64 for state in (report_state, metric_state, valuation_state))
    assert len(session.statements) == 3


def test_publish_reuses_current_release_when_content_is_unchanged(
    monkeypatch: MonkeyPatch,
) -> None:
    """无 canonical 变化时复用已绑定 release 的当前 publication 与 dataVersion。"""
    repository = _repository()
    current_version = UUID("10000000-0000-4000-8000-000000000001")
    release_bridge = Mock(
        return_value=PublishedCanonicalRelease(
            release_id=UUID("20000000-0000-4000-8000-000000000001"),
            data_version=current_version,
            reused_release=True,
            reused_publication=True,
            published_at=datetime(2026, 7, 28, 8, tzinfo=UTC),
        )
    )
    monkeypatch.setattr(
        financial_sync_repository,
        "_current_release_records",
        Mock(return_value=((), date(2026, 3, 31), date(2026, 3, 31))),
    )
    monkeypatch.setattr(financial_sync_repository, "publish_legacy_snapshot", release_bridge)
    session = FakeSession([])

    result = repository._publish(
        cast(Session, session),
        capability="financial.report",
        security_id=8,
        methodology_id=_METHODOLOGY_ID,
        effective_as_of=date(2026, 4, 29),
        changed_count=0,
        row_count=2,
        content_sha256="a" * 64,
        source_batch_id=UUID("30000000-0000-4000-8000-000000000001"),
        now=datetime(2026, 7, 28, 8, tzinfo=UTC),
    )

    assert result.data_version == current_version
    assert result.inserted_count == 0
    assert result.unchanged_count == 2
    assert session.statements == []
    assert release_bridge.call_args.kwargs["dataset_code"] == "financial.report"


def test_publish_creates_financial_detail_for_new_canonical_release(
    monkeypatch: MonkeyPatch,
) -> None:
    """新 canonical release 统一替代消费者指针，并追加同一真实版本的财务发布明细。"""
    repository = _repository()
    data_version = UUID("40000000-0000-4000-8000-000000000001")
    release_bridge = Mock(
        return_value=PublishedCanonicalRelease(
            release_id=UUID("50000000-0000-4000-8000-000000000001"),
            data_version=data_version,
            reused_release=False,
            reused_publication=False,
            published_at=datetime(2026, 7, 28, 8, tzinfo=UTC),
        )
    )
    monkeypatch.setattr(
        financial_sync_repository,
        "_current_release_records",
        Mock(return_value=((), date(2026, 7, 27), date(2026, 7, 27))),
    )
    monkeypatch.setattr(financial_sync_repository, "publish_legacy_snapshot", release_bridge)
    session = FakeSession([None])

    result = repository._publish(
        cast(Session, session),
        capability="financial.valuation",
        security_id=8,
        methodology_id=_METHODOLOGY_ID,
        effective_as_of=date(2026, 7, 27),
        changed_count=1,
        row_count=5,
        content_sha256="b" * 64,
        source_batch_id=UUID("60000000-0000-4000-8000-000000000001"),
        now=datetime(2026, 7, 28, 8, tzinfo=UTC),
    )

    assert result.capability == "financial.valuation"
    assert result.data_version == data_version
    assert result.inserted_count == 1
    assert result.unchanged_count == 4
    rendered = "\n".join(session.statements)
    assert "INSERT INTO financial_publication" in rendered
    assert release_bridge.call_args.kwargs["dataset_code"] == "financial.valuation"


def test_repository_uses_deterministic_methodology_and_active_metric_definitions() -> None:
    """方法学和字段定义应可重放复用，并拒绝未知证券的隐式创建。"""
    repository = _repository()
    created_methodology = uuid5(
        NAMESPACE_URL,
        "quant-v2:akshare.eastmoney.financial-report:1",
    )
    create_session = FakeSession([None, created_methodology])
    existing_session = FakeSession([created_methodology])
    metric_create_session = FakeSession([None, 33])
    metric_existing_session = FakeSession([33])
    security_session = FakeSession([[{"security_id": 8, "identity_state": "CONFIRMED"}]])

    methodology_id = repository._methodology_id(
        cast(Session, create_session),
        capability="financial.report",
        now=datetime(2026, 7, 28, 8, tzinfo=UTC),
    )
    existing_methodology_id = repository._methodology_id(
        cast(Session, existing_session),
        capability="financial.report",
        now=datetime(2026, 7, 28, 8, tzinfo=UTC),
    )
    created_metric_id = repository._metric_id(
        cast(Session, metric_create_session),
        code="statement.balance_sheet.assets",
        label="资产总计",
        origin="statement_fact",
        statement_type="BALANCE_SHEET",
        value_domain="monetary",
        canonical_unit="source_unknown",
        currency_required=False,
        sign_convention="provider_as_reported",
    )
    existing_metric_id = repository._metric_id(
        cast(Session, metric_existing_session),
        code="statement.balance_sheet.assets",
        label="资产总计",
        origin="statement_fact",
        statement_type="BALANCE_SHEET",
        value_domain="monetary",
        canonical_unit="source_unknown",
        currency_required=False,
        sign_convention="provider_as_reported",
    )
    security_id = repository._security_id(
        cast(Session, security_session),
        exchange=Exchange.SSE,
        symbol="600519",
        fact_dates=(date(2026, 7, 28),),
        known_at=datetime(2026, 7, 28, 8, tzinfo=UTC),
    )

    assert methodology_id == created_methodology
    assert existing_methodology_id == created_methodology
    assert created_metric_id == 33
    assert existing_metric_id == 33
    assert security_id == 8


def test_financial_identity_uses_current_selector_not_pre_listing_report_period(
    monkeypatch: MonkeyPatch,
) -> None:
    """招股前真实报告期仍归属当前唯一证券身份，不能被误判为当时已经上市。"""
    captured: dict[str, object] = {}

    def resolve_current_identity(
        connection: Session,
        *,
        exchange: Exchange,
        symbol: str,
        fact_dates: tuple[date, ...],
        known_at: datetime,
    ) -> int:
        """捕获仓储交给统一身份解析器的业务日期。"""
        captured.update(
            connection=connection,
            exchange=exchange,
            symbol=symbol,
            fact_dates=fact_dates,
            known_at=known_at,
        )
        return 8

    monkeypatch.setattr(
        financial_sync_repository,
        "require_single_confirmed_identity_on_connection",
        resolve_current_identity,
    )
    repository = _repository()
    known_at = datetime(2026, 7, 30, 16, tzinfo=UTC)

    security_id = repository._security_id(
        cast(Session, object()),
        exchange=Exchange.SSE,
        symbol="600519",
        fact_dates=(date(1998, 12, 31), date(2026, 6, 30)),
        known_at=known_at,
    )

    assert security_id == 8
    assert captured["fact_dates"] == (date(2026, 7, 31),)
    assert captured["known_at"] == known_at


def test_repository_tracks_revision_numbers_checkpoint_and_quality_evidence() -> None:
    """修订号、checkpoint 和质量结果必须在无变化和变化分支中保持可审计。"""
    repository = _repository()
    revision_session = FakeSession([2, None])
    checkpoint_session = FakeSession([None, None, None])
    result = FinancialPublicationResult(
        capability="financial.provider-metric",
        data_version=UUID("10000000-0000-4000-8000-000000000003"),
        inserted_count=0,
        unchanged_count=2,
    )

    next_revision = repository._next_revision(
        cast(Session, revision_session),
        FinancialReportRevision.revision,
        FinancialReportRevision.financial_report_id == 1,
    )
    first_revision = repository._next_revision(
        cast(Session, revision_session),
        FinancialReportRevision.revision,
        FinancialReportRevision.financial_report_id == 1,
    )
    repository._checkpoint(
        cast(Session, checkpoint_session),
        source=_source(),
        result=result,
        now=datetime(2026, 7, 28, 8, tzinfo=UTC),
    )
    repository._quality_result(
        cast(Session, checkpoint_session),
        source_batch_id=UUID("30000000-0000-4000-8000-000000000001"),
        data_version=result.data_version,
        inserted_count=0,
        now=datetime(2026, 7, 28, 8, tzinfo=UTC),
    )

    assert next_revision == 3
    assert first_revision == 1
    rendered = "\n".join(checkpoint_session.statements)
    assert "INSERT INTO financial_change_checkpoint" in rendered
    assert "INSERT INTO financial_quality_result" in rendered


def test_report_revision_skips_identical_content_and_closes_changed_revision() -> None:
    """报表重放相同内容不产生伪修订，变化内容必须闭合旧知识区间并写入全部事实。"""
    repository = _repository()
    report = _report()
    unchanged_session = FakeSession([22, {"revision": 3, "content_sha256": _content_hash(report)}])
    changed_session = FakeSession(
        [
            22,
            {"revision": 3, "content_sha256": "b" * 64},
            3,
            None,
            None,
            45,
            None,
        ]
    )
    now = datetime(2026, 7, 28, 8, tzinfo=UTC)

    unchanged = repository._write_report(
        cast(Session, unchanged_session),
        security_id=8,
        methodology_id=_METHODOLOGY_ID,
        report=report,
        source_batch_id=_SOURCE_BATCH_ID,
        source=_source(),
        now=now,
    )
    changed = repository._write_report(
        cast(Session, changed_session),
        security_id=8,
        methodology_id=_METHODOLOGY_ID,
        report=report,
        source_batch_id=_SOURCE_BATCH_ID,
        source=_source(),
        now=now,
    )

    assert unchanged == 0
    assert len(unchanged_session.statements) == 2
    assert changed == 1
    rendered = "\n".join(changed_session.statements)
    assert "UPDATE financial_report_revision" in rendered
    assert "INSERT INTO financial_report_revision" in rendered
    assert "INSERT INTO financial_statement_fact" in rendered


def test_provider_metric_and_valuation_revision_close_current_versions() -> None:
    """供应商指标和日频估值变化均须追加修订，且不允许沿用已关闭的 current 指针。"""
    repository = _repository()
    metric_session = FakeSession([31, {"revision": 2, "content_sha256": "a" * 64}, 2, None, None])
    valuation_session = FakeSession(
        [32, {"revision": 4, "content_sha256": "a" * 64}, 4, None, None]
    )
    now = datetime(2026, 7, 28, 8, tzinfo=UTC)

    metric_changed = repository._write_provider_metric(
        cast(Session, metric_session),
        security_id=8,
        methodology_id=_METHODOLOGY_ID,
        metric=_provider_metric(),
        source_batch_id=_SOURCE_BATCH_ID,
        source=_source(),
        now=now,
    )
    valuation_changed = repository._write_valuation(
        cast(Session, valuation_session),
        security_id=8,
        methodology_id=_METHODOLOGY_ID,
        valuation=_valuation(),
        source_batch_id=_SOURCE_BATCH_ID,
        source=_source(),
        now=now,
    )

    assert metric_changed == 1
    assert valuation_changed == 1
    assert "UPDATE provider_financial_metric_revision" in "\n".join(metric_session.statements)
    assert "INSERT INTO provider_financial_metric_revision" in "\n".join(metric_session.statements)
    assert "UPDATE valuation_observation_revision" in "\n".join(valuation_session.statements)
    assert "INSERT INTO valuation_observation_revision" in "\n".join(valuation_session.statements)


def test_canonical_writes_reject_empty_inputs_before_opening_transactions() -> None:
    """空批次没有可发布的 canonical 快照，三个入口均必须在开启事务前失败。"""
    repository = _repository()

    for publish in (
        repository.publish_reports,
        repository.publish_provider_metrics,
        repository.publish_valuations,
    ):
        try:
            publish(
                exchange=Exchange.SSE,
                symbol="600519",
                **{
                    _input_argument_name(publish.__name__): (),
                    "source": _source(),
                },
            )
        except ValueError as error:
            assert str(error).endswith("must not be empty")
        else:
            raise AssertionError("空财务批次必须拒绝")


def test_publish_reports_orchestrates_identity_revision_publication_and_quality(
    monkeypatch: MonkeyPatch,
) -> None:
    """报表公开入口必须按身份、方法学、来源、revision、发布、checkpoint、质量的顺序完成。"""
    repository, calls = _orchestration_repository(
        monkeypatch,
        capability="financial.report",
        publication_state="report",
    )

    result = repository.publish_reports(
        exchange=Exchange.SSE,
        symbol="600519",
        reports=(_report(),),
        source=_source(),
    )

    assert result.capability == "financial.report"
    calls["partition"].assert_called_once()
    calls["write"].assert_called_once()
    calls["state"].assert_called_once()
    calls["publish"].assert_called_once()
    calls["checkpoint"].assert_called_once()
    calls["quality"].assert_called_once()


def test_publish_provider_metrics_orchestrates_independent_publication(
    monkeypatch: MonkeyPatch,
) -> None:
    """供应商指标须使用独立能力、完整当前视图摘要及对应质量审计记录。"""
    repository, calls = _orchestration_repository(
        monkeypatch,
        capability="financial.provider-metric",
        publication_state="provider_metric",
    )

    result = repository.publish_provider_metrics(
        exchange=Exchange.SSE,
        symbol="600519",
        metrics=(_provider_metric(),),
        source=_source(),
    )

    assert result.capability == "financial.provider-metric"
    calls["partition"].assert_called_once()
    calls["write"].assert_called_once()
    calls["state"].assert_called_once()
    calls["publish"].assert_called_once()
    calls["checkpoint"].assert_called_once()
    calls["quality"].assert_called_once()


def test_publish_valuations_orchestrates_independent_publication(
    monkeypatch: MonkeyPatch,
) -> None:
    """日频估值须使用独立能力、完整当前视图摘要及对应质量审计记录。"""
    repository, calls = _orchestration_repository(
        monkeypatch,
        capability="financial.valuation",
        publication_state="valuation",
    )

    result = repository.publish_valuations(
        exchange=Exchange.SSE,
        symbol="600519",
        valuations=(_valuation(),),
        source=_source(),
    )

    assert result.capability == "financial.valuation"
    calls["partition"].assert_called_once()
    calls["write"].assert_called_once()
    calls["state"].assert_called_once()
    calls["publish"].assert_called_once()
    calls["checkpoint"].assert_called_once()
    calls["quality"].assert_called_once()


def _repository() -> SqlAlchemyFinancialSyncRepository:
    """构造不访问数据库的仓储实例，仅调用可独立验证的持久化逻辑。"""
    return SqlAlchemyFinancialSyncRepository(cast(DatabaseClient, object()))


def _source() -> FinancialSourceObservation:
    """构造已归档 raw evidence 的最小来源观察，供发布和 checkpoint 测试复用。"""
    return FinancialSourceObservation(
        provider_id="akshare-eastmoney-financial",
        capability="financial.statement.raw",
        source_payload_sha256="a" * 64,
        raw_uri="s3://raw/test.json",
        observed_at=datetime(2026, 7, 28, 8, tzinfo=UTC),
        upstream_source="eastmoney.financial",
        adapter_version="test-v1",
        schema_fingerprint="b" * 64,
    )


def _report() -> FinancialReportInput:
    """构造含单条受控事实的最小利润表输入，覆盖报表 revision 与行项目写入。"""
    return FinancialReportInput(
        statement_type="INCOME_STATEMENT",
        report_period=date(2026, 6, 30),
        period_basis="YEAR_TO_DATE",
        statement_scope="CONSOLIDATED",
        currency="CNY",
        currency_null_reason=None,
        report_type="interim",
        announcement_date=date(2026, 8, 28),
        provider_update_at=datetime(2026, 8, 28, 8, tzinfo=UTC),
        audit_status="unaudited",
        facts=(
            FinancialFactInput(
                code="statement.income_statement.total-operate-income",
                label="营业总收入",
                value=Decimal("12.5"),
                null_reason=None,
                value_domain="monetary",
                original_unit="元",
                canonical_unit="CNY",
                scale_factor=Decimal("1"),
                sign_convention="provider_as_reported",
                currency="CNY",
                currency_null_reason=None,
            ),
        ),
    )


def _provider_metric() -> FinancialMetricInput:
    """构造最小供应商指标，覆盖独立 revision 的闭合与追加逻辑。"""
    return FinancialMetricInput(
        code="provider.financial.roe",
        label="净资产收益率",
        report_period=date(2026, 6, 30),
        period_basis="YEAR_TO_DATE",
        statement_scope="CONSOLIDATED",
        value=Decimal("0.15"),
        value_domain="ratio",
        unit="ratio",
        currency=None,
        currency_null_reason="NOT_APPLICABLE",
    )


def _valuation() -> FinancialValuationInput:
    """构造最小估值观察，覆盖日频 revision 的闭合与追加逻辑。"""
    return FinancialValuationInput(
        code="valuation.pe_ttm",
        label="市盈率 TTM",
        observation_date=date(2026, 7, 28),
        value=Decimal("18.2"),
        value_domain="ratio",
        unit="ratio",
        currency=None,
        currency_null_reason="NOT_APPLICABLE",
    )


def _input_argument_name(method_name: str) -> str:
    """将公开写入方法名映射为其唯一的批次关键字参数。"""
    return {
        "publish_reports": "reports",
        "publish_provider_metrics": "metrics",
        "publish_valuations": "valuations",
    }[method_name]


def _orchestration_repository(
    monkeypatch: MonkeyPatch,
    *,
    capability: str,
    publication_state: str,
) -> tuple[SqlAlchemyFinancialSyncRepository, dict[str, Mock]]:
    """安装公开写入编排的边界替身，保留调用顺序和参数由真实入口负责。"""
    repository = _repository()
    session = cast(Session, FakeSession([]))
    repository._database = cast(DatabaseClient, FakeDatabase(session))
    result = FinancialPublicationResult(
        capability=cast("FinancialCapability", capability),
        data_version=UUID("40000000-0000-4000-8000-000000000001"),
        inserted_count=1,
        unchanged_count=0,
    )
    calls = {
        "partition": Mock(),
        "write": Mock(return_value=1),
        "state": Mock(return_value=(date(2026, 7, 28), 1, "c" * 64)),
        "publish": Mock(return_value=result),
        "checkpoint": Mock(),
        "quality": Mock(),
    }
    monkeypatch.setattr(
        "service_data_sync.infrastructure.persistence.financial_sync_repository.ensure_financial_year_partitions",
        calls["partition"],
    )
    monkeypatch.setattr(repository, "_security_id", Mock(return_value=8))
    monkeypatch.setattr(repository, "_methodology_id", Mock(return_value=_METHODOLOGY_ID))
    monkeypatch.setattr(repository, "_source_batch", Mock(return_value=_SOURCE_BATCH_ID))
    monkeypatch.setattr(repository, "_publish", calls["publish"])
    monkeypatch.setattr(repository, "_checkpoint", calls["checkpoint"])
    monkeypatch.setattr(repository, "_quality_result", calls["quality"])
    if publication_state == "report":
        monkeypatch.setattr(repository, "_write_report", calls["write"])
        monkeypatch.setattr(repository, "_report_publication_state", calls["state"])
    elif publication_state == "provider_metric":
        monkeypatch.setattr(repository, "_write_provider_metric", calls["write"])
        monkeypatch.setattr(repository, "_provider_metric_publication_state", calls["state"])
    else:
        monkeypatch.setattr(repository, "_write_valuation", calls["write"])
        monkeypatch.setattr(repository, "_valuation_publication_state", calls["state"])
    return repository, calls


_METHODOLOGY_ID = UUID("20000000-0000-4000-8000-000000000001")
_SOURCE_BATCH_ID = UUID("30000000-0000-4000-8000-000000000001")
