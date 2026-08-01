"""既有 revision 到统一 canonical release 发布桥接的回归测试。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import cast
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

import service_data_sync.infrastructure.persistence.canonical_release_repository as release_module
from service_data_sync.application.ports.canonical_release import (
    CanonicalLineageRecord,
    CanonicalReleaseCandidate,
    PublishedCanonicalRelease,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.canonical_release_repository import (
    SqlAlchemyCanonicalReleaseRepository,
)
from service_data_sync.infrastructure.persistence.legacy_canonical_release_bridge import (
    publish_legacy_snapshot,
)

_DATASET_ID = UUID("10000000-0000-4000-8000-000000000001")
_METHODOLOGY_ID = UUID("20000000-0000-4000-8000-000000000001")
_NORMALIZATION_RUN_ID = UUID("30000000-0000-4000-8000-000000000001")
_SOURCE_BATCH_ID = UUID("40000000-0000-4000-8000-000000000001")
_SYNC_RUN_ID = UUID("50000000-0000-4000-8000-000000000001")


class FakeResult:
    """模拟桥接查询所需的标量和映射结果，不连接真实数据库。"""

    def __init__(self, value: object) -> None:
        """保存当前 SQL 语句对应的唯一预置值。"""
        self._value = value

    def scalar_one(self) -> object:
        """返回必需存在的主键或版本标识。"""
        return self._value

    def scalar_one_or_none(self) -> object | None:
        """返回可为空的 upsert 或 checkpoint 查询结果。"""
        return self._value

    def mappings(self) -> FakeResult:
        """声明预置值是按列名访问的映射行。"""
        return self

    def one(self) -> object:
        """返回必需存在的来源批次映射。"""
        return self._value


class BridgeSession:
    """按目标表路由桥接 SQL，验证桥接只消费真实元数据而不读取 raw。"""

    def __init__(self) -> None:
        """初始化语句记录，供断言生命周期登记顺序。"""
        self.statements: list[str] = []

    def execute(self, statement: object) -> FakeResult:
        """为 canonical dataset、方法学、来源、运行和 checkpoint 返回确定性结果。"""
        rendered = str(statement)
        self.statements.append(rendered)
        if "SELECT canonical_dataset.dataset_id" in rendered:
            return FakeResult(_DATASET_ID)
        if "SELECT methodology_version.methodology_version_id" in rendered:
            return FakeResult(_METHODOLOGY_ID)
        if "FROM source_batch" in rendered:
            return FakeResult(
                {
                    "run_id": _SYNC_RUN_ID,
                    "adapter_version": "akshare-test-v1",
                    "schema_fingerprint": "a" * 64,
                    "payload_sha256": "b" * 64,
                }
            )
        if "INSERT INTO normalization_run" in rendered:
            return FakeResult(_NORMALIZATION_RUN_ID)
        if "FROM canonical_checkpoint" in rendered:
            return FakeResult(None)
        return FakeResult(None)


class CapturingReleaseRepository:
    """捕获交给正规发布器的候选，确保桥接不绕过其 release 生成逻辑。"""

    def __init__(self, published: PublishedCanonicalRelease) -> None:
        """保存由正规发布器返回的真实 release/dataVersion 结果。"""
        self._published = published
        self.candidate: CanonicalReleaseCandidate | None = None
        self.write_publication: object = None
        self.write_visibility: object = None
        self.before_final_publication: object = None

    def publish_in_session(
        self,
        *,
        session: Session,
        candidate: CanonicalReleaseCandidate,
        write_facts: object = None,
        write_publication: object = None,
        write_visibility: object = None,
        before_final_publication: object = None,
        record_fenced_progress: bool = True,
    ) -> PublishedCanonicalRelease:
        """记录候选并返回正规发布器已经生成的不可变 release 结果。"""
        del session, write_facts, record_fenced_progress
        self.candidate = candidate
        self.write_publication = write_publication
        self.write_visibility = write_visibility
        self.before_final_publication = before_final_publication
        return self._published


def test_legacy_bridge_uses_current_lineage_and_delegates_real_release() -> None:
    """桥接必须传递真实当前 revision 血缘，不读取成功 raw 或构造 release UUID。"""
    session = BridgeSession()
    published = PublishedCanonicalRelease(
        release_id=UUID("60000000-0000-4000-8000-000000000001"),
        data_version=UUID("70000000-0000-4000-8000-000000000001"),
        reused_release=False,
        reused_publication=False,
        published_at=datetime(2026, 7, 29, tzinfo=UTC),
    )
    repository = CapturingReleaseRepository(published)
    record = CanonicalLineageRecord(
        record_key_hash="c" * 64,
        content_hash="d" * 64,
        source_batch_id=_SOURCE_BATCH_ID,
        transform_hash="e" * 64,
    )

    result = publish_legacy_snapshot(
        cast(Session, session),
        release_repository=cast(SqlAlchemyCanonicalReleaseRepository, repository),
        dataset_code="equity.bar.1d.raw",
        partition_key="security:8",
        domain="equity",
        grain="equity security + trade date",
        semantic_family="reported-equity-market-data",
        mapping_version="equity-market-data-release-bridge-v1",
        source_batch_id=_SOURCE_BATCH_ID,
        records=(record,),
        fact_min=date(2026, 7, 28),
        fact_max=date(2026, 7, 28),
        now=datetime(2026, 7, 29, tzinfo=UTC),
        publication_effective_as_of=date(2026, 7, 29),
    )

    assert result == published
    assert repository.candidate is not None
    assert repository.candidate.dataset_id == _DATASET_ID
    assert repository.candidate.methodology_version_id == _METHODOLOGY_ID
    assert repository.candidate.normalization_run_id == _NORMALIZATION_RUN_ID
    assert repository.candidate.records == (record,)
    assert repository.candidate.expected_fencing_token == 0
    assert repository.candidate.publication_effective_as_of == date(2026, 7, 29)
    assert repository.candidate.checkpoint_position["snapshotHash"]
    assert any(
        "ON CONFLICT" in statement
        and "DO UPDATE SET status" in statement
        and "canonical_dataset.status IN" in statement
        for statement in session.statements
    )
    assert all("raw_payload_manifest" not in statement for statement in session.statements)


def test_legacy_bridge_preserves_warned_publication_quality() -> None:
    """已有发布门允许 warned 时，桥接不得把真实质量状态硬编码为 passed。"""
    session = BridgeSession()
    published = PublishedCanonicalRelease(
        release_id=UUID("61000000-0000-4000-8000-000000000001"),
        data_version=UUID("71000000-0000-4000-8000-000000000001"),
        reused_release=False,
        reused_publication=False,
        published_at=datetime(2026, 7, 29, tzinfo=UTC),
    )
    repository = CapturingReleaseRepository(published)

    publish_legacy_snapshot(
        cast(Session, session),
        release_repository=cast(SqlAlchemyCanonicalReleaseRepository, repository),
        dataset_code="sector.quote.eod.snapshot",
        partition_key="eastmoney.industry:2026-07-29",
        domain="sector",
        grain="sector EOD snapshot",
        semantic_family="reported-sector-eod-snapshot",
        mapping_version="sector-eod-release-bridge-v1",
        source_batch_id=_SOURCE_BATCH_ID,
        records=(_record(),),
        fact_min=date(2026, 7, 29),
        fact_max=date(2026, 7, 29),
        now=datetime(2026, 7, 29, tzinfo=UTC),
        quality_status="warned",
        publication_effective_as_of=date(2026, 7, 29),
    )

    assert repository.candidate is not None
    assert repository.candidate.quality.status == "warned"
    assert repository.candidate.publication_effective_as_of == date(2026, 7, 29)


def test_release_repository_publishes_inside_existing_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既有仓储调用公开 in-session API 时必须复用统一 `_publish_in_session` 实现。"""
    expected = PublishedCanonicalRelease(
        release_id=UUID("80000000-0000-4000-8000-000000000001"),
        data_version=UUID("90000000-0000-4000-8000-000000000001"),
        reused_release=False,
        reused_publication=False,
        published_at=datetime(2026, 7, 29, tzinfo=UTC),
    )
    publisher = Mock(return_value=expected)
    monkeypatch.setattr(release_module, "_publish_in_session", publisher)
    repository = SqlAlchemyCanonicalReleaseRepository(cast(DatabaseClient, Mock()))
    candidate = CanonicalReleaseCandidate(
        dataset_id=_DATASET_ID,
        dataset_code="equity.bar.1d.raw",
        partition_key="security:8",
        methodology_version_id=_METHODOLOGY_ID,
        normalization_run_id=_NORMALIZATION_RUN_ID,
        records=(),
        quality=_quality(),
        fact_min=None,
        fact_max=None,
        checkpoint_kind="published",
        checkpoint_position={"snapshotHash": "a" * 64},
        expected_fencing_token=0,
        created_at=datetime(2026, 7, 29, tzinfo=UTC),
    )

    result = repository.publish_in_session(
        session=cast(Session, Mock()),
        candidate=candidate,
    )

    assert result == expected
    assert publisher.call_args.kwargs["candidate"] == candidate


def _quality():
    """构造满足候选校验的最小通过质量结论，供仓储委托回归使用。"""
    from service_data_sync.application.ports.canonical_release import (
        CanonicalQualityDecision,
        CanonicalQualityRule,
    )

    return CanonicalQualityDecision(
        status="passed",
        policy_code="test.bridge",
        policy_version=1,
        rules=(CanonicalQualityRule("current-snapshot-materialized", "blocking", True),),
    )


def _record() -> CanonicalLineageRecord:
    """构造与 bridge 输入约束一致的单条当前 revision 血缘。"""
    return CanonicalLineageRecord(
        record_key_hash="c" * 64,
        content_hash="d" * 64,
        source_batch_id=_SOURCE_BATCH_ID,
        transform_hash="e" * 64,
    )
