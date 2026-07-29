"""canonical release 发布门禁、稳定内容摘要与仓储边界的单元测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from service_data_sync.application.canonical.release_publication import (
    CanonicalReleasePublicationService,
    canonical_release_content_hash,
)
from service_data_sync.application.ports.canonical_release import (
    CanonicalLineageRecord,
    CanonicalQualityDecision,
    CanonicalQualityRule,
    CanonicalReleaseCandidate,
    PublishedCanonicalRelease,
)


class FakeRepository:
    """记录应用层最终允许提交的 canonical 候选，不访问数据库。"""

    def __init__(self) -> None:
        """初始化空的候选捕获位置。"""
        self.candidate: CanonicalReleaseCandidate | None = None

    def publish(self, candidate: CanonicalReleaseCandidate) -> PublishedCanonicalRelease:
        """捕获已完成门禁的候选并回传固定发布结果。"""
        self.candidate = candidate
        return PublishedCanonicalRelease(
            release_id=uuid4(),
            data_version=uuid4(),
            reused_release=False,
            reused_publication=False,
            published_at=candidate.created_at,
        )


def _candidate(*, records: tuple[CanonicalLineageRecord, ...]) -> CanonicalReleaseCandidate:
    """构造一份已通过质量门、可由发布服务处理的最小候选。"""
    return CanonicalReleaseCandidate(
        dataset_id=uuid4(),
        dataset_code="derivative.bar.1d.reported",
        partition_key="CFFEX:IF2608:2026-07-28",
        methodology_version_id=uuid4(),
        normalization_run_id=uuid4(),
        records=records,
        quality=CanonicalQualityDecision(
            status="passed",
            policy_code="derivative.bar.1d.quality",
            policy_version=1,
            rules=(CanonicalQualityRule("ohlc", "blocking", True),),
        ),
        fact_min=date(2026, 7, 28),
        fact_max=date(2026, 7, 28),
        checkpoint_kind="published",
        checkpoint_position={"tradeDate": "2026-07-28"},
        expected_fencing_token=0,
        created_at=datetime(2026, 7, 29, tzinfo=UTC),
    )


def test_release_content_hash_is_stable_when_records_arrive_in_different_orders() -> None:
    """adapter 页顺序不能改变相同业务事实集合的 release 身份。"""
    first = CanonicalLineageRecord("a" * 64, "b" * 64, uuid4(), "c" * 64)
    second = CanonicalLineageRecord("d" * 64, "e" * 64, uuid4(), "f" * 64)
    candidate = _candidate(records=(first, second))

    assert canonical_release_content_hash(candidate) == canonical_release_content_hash(
        replace(candidate, records=(second, first))
    )


def test_blocking_quality_rule_fails_before_repository_publication() -> None:
    """任一 blocking 失败必须阻断 release、publication 和 checkpoint 全部写入。"""
    with pytest.raises(ValueError, match="blocking"):
        CanonicalQualityDecision(
            status="warned",
            policy_code="derivative.bar.1d.quality",
            policy_version=1,
            rules=(CanonicalQualityRule("identity", "blocking", False),),
        )


def test_publication_service_delegates_only_a_valid_candidate() -> None:
    """应用层只将已校验的候选交给拥有原子事务的仓储。"""
    repository = FakeRepository()
    candidate = _candidate(records=(CanonicalLineageRecord("a" * 64, "b" * 64, uuid4(), "c" * 64),))

    result = CanonicalReleasePublicationService(repository=repository).publish(candidate)

    assert repository.candidate is candidate
    assert result.published_at == candidate.created_at
