"""SQLAlchemy canonical release 发布仓储，统一实现不可变内容、可追溯血缘与 fencing。"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from service_data_sync.application.canonical.release_publication import (
    canonical_release_content_hash,
)
from service_data_sync.application.ports.canonical_release import (
    CanonicalReleaseCandidate,
    CanonicalReleaseRepository,
    PublishedCanonicalRelease,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import (
    CanonicalCheckpoint,
    CanonicalDataset,
    CanonicalRecordLineage,
    DatasetRelease,
    MethodologyVersion,
    NormalizationRun,
    QualityEvaluation,
    QualityResult,
)
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)


class SqlAlchemyCanonicalReleaseRepository(CanonicalReleaseRepository):
    """在一个数据库事务内发布一个已质量合格的 canonical dataset 分区。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保留服务拥有的事务工厂，避免应用层接触 SQLAlchemy session。"""
        self._database = database

    def publish(self, candidate: CanonicalReleaseCandidate) -> PublishedCanonicalRelease:
        """复用相同内容 release，或原子创建版本、血缘、发布指针和新 fencing 水位。"""
        return self.publish_with_facts(candidate=candidate, write_facts=None)

    def publish_with_facts(
        self,
        *,
        candidate: CanonicalReleaseCandidate,
        write_facts: Callable[[Session, UUID], None] | None,
    ) -> PublishedCanonicalRelease:
        """在 release 创建与 publication 切换之间写入强类型事实，消除非空 release 外键循环。"""
        with self._database.transaction() as session:
            return _publish_in_session(session, candidate=candidate, write_facts=write_facts)

    def publish_prepared(
        self,
        *,
        prepare_candidate: Callable[[Session], CanonicalReleaseCandidate],
        write_facts: Callable[[Session, CanonicalReleaseCandidate, UUID], None],
    ) -> PublishedCanonicalRelease:
        """在同一事务内准备来源/运行候选、写 revision 并切换 publication。"""
        with self._database.transaction() as session:
            candidate = prepare_candidate(session)
            return _publish_in_session(
                session,
                candidate=candidate,
                write_facts=lambda current_session, release_id: write_facts(
                    current_session, candidate, release_id
                ),
            )


def _publish_in_session(
    session: Session,
    *,
    candidate: CanonicalReleaseCandidate,
    write_facts: Callable[[Session, UUID], None] | None,
) -> PublishedCanonicalRelease:
    """执行已准备候选的统一发布步骤；调用方必须已开启同一数据库事务。"""
    content_hash = canonical_release_content_hash(candidate)
    _validate_candidate_references(session, candidate)
    _record_quality_decision(session, candidate=candidate)
    release, reused_release = _find_or_create_release(
        session, candidate=candidate, content_hash=content_hash
    )
    # 新 release 必须先拥有稳定 UUID，随后才能让强类型 revision 以非空外键引用它。
    # 回调仍处在同一事务中：任一步失败都会回滚 release、事实、血缘和 publication。
    if not reused_release and write_facts is not None:
        write_facts(session, release.release_id)
    _record_lineage(session, release_id=release.release_id, candidate=candidate)
    current = session.execute(
        select(DatasetPublication)
        .where(
            DatasetPublication.dataset == candidate.dataset_code,
            DatasetPublication.partition_key == candidate.partition_key,
            DatasetPublication.superseded_at.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if current is not None and current.release_id == release.release_id:
        _advance_checkpoint(session, candidate=candidate, release_id=release.release_id)
        return PublishedCanonicalRelease(
            release_id=release.release_id,
            data_version=current.data_version,
            reused_release=True,
            reused_publication=True,
            published_at=current.published_at,
        )
    existing_publication = session.execute(
        select(DatasetPublication).where(DatasetPublication.release_id == release.release_id)
    ).scalar_one_or_none()
    if existing_publication is not None:
        raise ValueError(
            "canonical release has already been superseded; reactivation needs a new design"
        )
    now = candidate.created_at
    if current is not None:
        session.execute(
            update(DatasetPublication)
            .where(DatasetPublication.publication_id == current.publication_id)
            .values(superseded_at=now)
        )
    publication_id = uuid4()
    data_version = uuid4()
    session.execute(
        insert(DatasetPublication).values(
            publication_id=publication_id,
            dataset=candidate.dataset_code,
            partition_key=candidate.partition_key,
            data_version=data_version,
            release_id=release.release_id,
            quality_status=candidate.quality.status,
            published_at=now,
            superseded_at=None,
            effective_as_of=candidate.fact_max,
            knowledge_cutoff=now,
        )
    )
    _advance_checkpoint(session, candidate=candidate, release_id=release.release_id)
    return PublishedCanonicalRelease(
        release_id=release.release_id,
        data_version=data_version,
        reused_release=reused_release,
        reused_publication=False,
        published_at=candidate.created_at,
    )


def _validate_candidate_references(session: Session, candidate: CanonicalReleaseCandidate) -> None:
    """验证 dataset、方法学和运行归属，防止跨域候选在错误分区发布。"""
    dataset = session.execute(
        select(CanonicalDataset)
        .where(CanonicalDataset.dataset_id == candidate.dataset_id)
        .with_for_update()
    ).scalar_one_or_none()
    if dataset is None or dataset.code != candidate.dataset_code:
        raise ValueError("canonical dataset does not match release candidate")
    if dataset.status not in {"candidate", "production"}:
        raise ValueError("canonical dataset is not eligible for publication")
    methodology = session.execute(
        select(MethodologyVersion).where(
            MethodologyVersion.methodology_version_id == candidate.methodology_version_id
        )
    ).scalar_one_or_none()
    if methodology is None or methodology.status != "validated":
        raise ValueError("canonical methodology is not validated")
    run = session.execute(
        select(NormalizationRun).where(
            NormalizationRun.normalization_run_id == candidate.normalization_run_id
        )
    ).scalar_one_or_none()
    if (
        run is None
        or run.dataset_id != candidate.dataset_id
        or run.partition_key != candidate.partition_key
        or run.status != "passed"
    ):
        raise ValueError("canonical normalization run is not a passed candidate for this partition")


def _record_quality_decision(session: Session, *, candidate: CanonicalReleaseCandidate) -> None:
    """固化版本化质量决策；同一 normalization 与策略重放时复用既有审计结论。"""
    existing = session.execute(
        select(QualityEvaluation).where(
            QualityEvaluation.normalization_run_id == candidate.normalization_run_id,
            QualityEvaluation.policy_code == candidate.quality.policy_code,
            QualityEvaluation.policy_version == candidate.quality.policy_version,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status != _evaluation_status(candidate.quality.status):
            raise ValueError("canonical quality policy result conflicts with prior evaluation")
        return
    evaluation_id = uuid4()
    inserted = session.execute(
        pg_insert(QualityEvaluation)
        .values(
            evaluation_id=evaluation_id,
            dataset_id=candidate.dataset_id,
            partition_key=candidate.partition_key,
            normalization_run_id=candidate.normalization_run_id,
            policy_code=candidate.quality.policy_code,
            policy_version=candidate.quality.policy_version,
            status=_evaluation_status(candidate.quality.status),
            score=None,
            evaluated_at=candidate.created_at,
        )
        .on_conflict_do_nothing(
            index_elements=("normalization_run_id", "policy_code", "policy_version")
        )
        .returning(QualityEvaluation.evaluation_id)
    ).scalar_one_or_none()
    if inserted is None:
        existing = session.execute(
            select(QualityEvaluation).where(
                QualityEvaluation.normalization_run_id == candidate.normalization_run_id,
                QualityEvaluation.policy_code == candidate.quality.policy_code,
                QualityEvaluation.policy_version == candidate.quality.policy_version,
            )
        ).scalar_one()
        if existing.status != _evaluation_status(candidate.quality.status):
            raise ValueError("canonical quality policy result conflicts with prior evaluation")
        return
    if candidate.quality.rules:
        session.execute(
            insert(QualityResult).values(
                [
                    {
                        "evaluation_id": evaluation_id,
                        "rule_code": rule.rule_code,
                        "severity": rule.severity,
                        "passed": rule.passed,
                        "actual_value": None,
                        "threshold_value": None,
                        "sample_json": None,
                        "affected_count": 0,
                    }
                    for rule in candidate.quality.rules
                ]
            )
        )


def _evaluation_status(release_status: str) -> str:
    """将 release 的 partial 可见性映射为可审计的 warned 质量评估状态。"""
    return "warned" if release_status == "partial" else release_status


def _find_or_create_release(
    session: Session, *, candidate: CanonicalReleaseCandidate, content_hash: str
) -> tuple[DatasetRelease, bool]:
    """按 immutable 内容唯一键复用 release；新内容才创建新的 release 身份。"""
    release_id = uuid4()
    inserted = session.execute(
        pg_insert(DatasetRelease)
        .values(
            release_id=release_id,
            dataset_id=candidate.dataset_id,
            partition_key=candidate.partition_key,
            methodology_version_id=candidate.methodology_version_id,
            normalization_run_id=candidate.normalization_run_id,
            content_hash=content_hash,
            quality_status=candidate.quality.status,
            record_count=len(candidate.records),
            fact_min=candidate.fact_min,
            fact_max=candidate.fact_max,
            created_at=candidate.created_at,
        )
        .on_conflict_do_nothing(
            index_elements=(
                "dataset_id",
                "partition_key",
                "methodology_version_id",
                "content_hash",
            )
        )
        .returning(DatasetRelease.release_id)
    ).scalar_one_or_none()
    release = session.execute(
        select(DatasetRelease).where(
            DatasetRelease.dataset_id == candidate.dataset_id,
            DatasetRelease.partition_key == candidate.partition_key,
            DatasetRelease.methodology_version_id == candidate.methodology_version_id,
            DatasetRelease.content_hash == content_hash,
        )
    ).scalar_one()
    return release, inserted is None


def _record_lineage(
    session: Session, *, release_id: UUID, candidate: CanonicalReleaseCandidate
) -> None:
    """追加新证据到 release 的多对多血缘；相同角色证据重放不重复插入。"""
    if not candidate.records:
        return
    session.execute(
        pg_insert(CanonicalRecordLineage)
        .values(
            [
                {
                    "release_id": release_id,
                    "record_key_hash": record.record_key_hash,
                    "source_batch_id": record.source_batch_id,
                    "role": record.role,
                    "raw_payload_id": record.raw_payload_id,
                    "transform_hash": record.transform_hash,
                }
                for record in candidate.records
            ]
        )
        .on_conflict_do_nothing(
            index_elements=("release_id", "record_key_hash", "source_batch_id", "role")
        )
    )


def _advance_checkpoint(
    session: Session, *, candidate: CanonicalReleaseCandidate, release_id: UUID
) -> None:
    """在 publication 成功路径内按 CAS 推进水位，拒绝过期 worker 的晚提交。"""
    checkpoint = session.execute(
        select(CanonicalCheckpoint)
        .where(
            CanonicalCheckpoint.dataset_id == candidate.dataset_id,
            CanonicalCheckpoint.partition_key == candidate.partition_key,
            CanonicalCheckpoint.checkpoint_kind == candidate.checkpoint_kind,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if checkpoint is None:
        if candidate.expected_fencing_token != 0:
            raise ValueError("canonical checkpoint has not reached the expected fencing token")
        session.execute(
            insert(CanonicalCheckpoint).values(
                dataset_id=candidate.dataset_id,
                partition_key=candidate.partition_key,
                checkpoint_kind=candidate.checkpoint_kind,
                position_json=candidate.checkpoint_position,
                last_release_id=release_id,
                fencing_token=1,
                updated_at=candidate.created_at,
            )
        )
        return
    if checkpoint.fencing_token != candidate.expected_fencing_token:
        raise ValueError("canonical checkpoint fencing token is stale")
    session.execute(
        update(CanonicalCheckpoint)
        .where(
            CanonicalCheckpoint.dataset_id == candidate.dataset_id,
            CanonicalCheckpoint.partition_key == candidate.partition_key,
            CanonicalCheckpoint.checkpoint_kind == candidate.checkpoint_kind,
            CanonicalCheckpoint.fencing_token == candidate.expected_fencing_token,
        )
        .values(
            position_json=candidate.checkpoint_position,
            last_release_id=release_id,
            fencing_token=candidate.expected_fencing_token + 1,
            updated_at=candidate.created_at,
        )
    )
