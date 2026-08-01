"""把既有强类型 revision 发布桥接到统一 `DatasetRelease` 生命周期。

早期 equity 与 financial 仓储已经在各自事务内维护了追加 revision。这里不复制事实、
不保留成功 raw，也不伪造 release UUID；它只读取当前真实快照和来源批次，复用统一
`CanonicalReleaseRepository` 创建 immutable release、血缘和带 `release_id` 的 publication。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from datetime import date, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from service_data_sync.application.ports.canonical_release import (
    CanonicalLineageRecord,
    CanonicalQualityDecision,
    CanonicalQualityRule,
    CanonicalReleaseCandidate,
    PublishedCanonicalRelease,
)
from service_data_sync.infrastructure.database.models.canonical import (
    CanonicalCheckpoint,
    CanonicalDataset,
    MethodologyVersion,
    NormalizationRun,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.persistence.canonical_release_repository import (
    SqlAlchemyCanonicalReleaseRepository,
)

_SCHEMA_VERSION = 1
_METHODOLOGY_VERSION = 1


def publish_legacy_snapshot(
    session: Session,
    *,
    release_repository: SqlAlchemyCanonicalReleaseRepository,
    dataset_code: str,
    partition_key: str,
    domain: str,
    grain: str,
    semantic_family: str,
    mapping_version: str,
    source_batch_id: UUID,
    records: Sequence[CanonicalLineageRecord],
    fact_min: date | None,
    fact_max: date | None,
    now: datetime,
    quality_status: str = "passed",
    publication_effective_as_of: date | None = None,
    write_publication: Callable[[Session, UUID, UUID, UUID], None] | None = None,
    write_visibility: Callable[[Session, UUID, UUID, UUID], None] | None = None,
    before_final_publication: Callable[[], None] | None = None,
    record_fenced_progress: bool = True,
) -> PublishedCanonicalRelease:
    """将当前 revision 快照转换为可验证的 canonical release 并在原事务内发布。

    传入的每条记录都来自刚刚查询到的当前事实行，且携带真实内容摘要和 `source_batch_id`。
    因此 release 内容可复验，重放相同快照会复用原 release/publication，不会生成任意 UUID；
    `quality_status` 只接受已通过发布门的规范状态，保留既有 `warned` 可见性语义。
    `publication_effective_as_of` 可保留 legacy 消费者版本的业务生效日期，避免把事实日期误作
    公告或可用日期。需要将聚合组件或依赖 `data_version` 的领域 release 与 publication 原子
    绑定时，调用方可传入两个同事务回调；回调绝不能推进第二个消费者指针。
    `record_fenced_progress` 仅供一个逻辑 run 内的汇总 publication 使用：它不是新的业务分区时
    必须关闭，避免把同一批 child 数据重复记入控制面进度。
    """
    if quality_status not in {"passed", "warned", "partial"}:
        raise ValueError("legacy release bridge quality status is invalid")
    normalized_records = tuple(sorted(records, key=lambda item: item.record_key_hash))
    dataset_id = _ensure_dataset(
        session,
        dataset_code=dataset_code,
        domain=domain,
        grain=grain,
        now=now,
    )
    methodology_id = _ensure_methodology(
        session,
        dataset_code=dataset_code,
        semantic_family=semantic_family,
        mapping_version=mapping_version,
    )
    normalization_run_id = _record_normalization_run(
        session,
        dataset_id=dataset_id,
        partition_key=partition_key,
        source_batch_id=source_batch_id,
        mapping_version=mapping_version,
        records=normalized_records,
        now=now,
    )
    expected_fencing_token = _checkpoint_fencing_token(
        session,
        dataset_id=dataset_id,
        partition_key=partition_key,
    )
    snapshot_hash = _snapshot_hash(normalized_records)
    candidate = CanonicalReleaseCandidate(
        dataset_id=dataset_id,
        dataset_code=dataset_code,
        partition_key=partition_key,
        methodology_version_id=methodology_id,
        normalization_run_id=normalization_run_id,
        records=normalized_records,
        quality=CanonicalQualityDecision(
            status=quality_status,
            policy_code=f"{dataset_code}.legacy-release-bridge",
            policy_version=1,
            rules=(
                CanonicalQualityRule(
                    rule_code="current-snapshot-materialized",
                    severity="blocking",
                    passed=True,
                ),
            ),
        ),
        fact_min=fact_min,
        fact_max=fact_max,
        checkpoint_kind="published",
        checkpoint_position={"snapshotHash": snapshot_hash},
        expected_fencing_token=expected_fencing_token,
        created_at=now,
        publication_effective_as_of=publication_effective_as_of,
    )
    # 既有仓储已在当前事务写入 revision；统一发布器只负责冻结、血缘和消费者指针。
    return release_repository.publish_in_session(
        session=session,
        candidate=candidate,
        write_publication=write_publication,
        write_visibility=write_visibility,
        before_final_publication=before_final_publication,
        record_fenced_progress=record_fenced_progress,
    )


def _ensure_dataset(
    session: Session,
    *,
    dataset_code: str,
    domain: str,
    grain: str,
    now: datetime,
) -> UUID:
    """登记生产 canonical 身份；桥接路径只承接已批准发布，且绝不复活退役数据集。"""
    dataset_id = uuid5(
        NAMESPACE_URL,
        f"quant-v2:canonical-dataset:{dataset_code}:{_SCHEMA_VERSION}",
    )
    session.execute(
        pg_insert(CanonicalDataset)
        .values(
            dataset_id=dataset_id,
            code=dataset_code,
            schema_version=_SCHEMA_VERSION,
            domain=domain,
            grain=grain,
            status="production",
            owner_service="service-data-sync",
            created_at=now,
        )
        .on_conflict_do_update(
            index_elements=("code", "schema_version"),
            set_={"status": "production"},
            # 已进入发布桥的数据可从早期 candidate 登记晋级，但退役状态必须由治理流程显式恢复。
            where=CanonicalDataset.status.in_(("research", "candidate")),
        )
    )
    return UUID(
        str(
            session.execute(
                select(CanonicalDataset.dataset_id).where(
                    CanonicalDataset.code == dataset_code,
                    CanonicalDataset.schema_version == _SCHEMA_VERSION,
                )
            ).scalar_one()
        )
    )


def _ensure_methodology(
    session: Session,
    *,
    dataset_code: str,
    semantic_family: str,
    mapping_version: str,
) -> UUID:
    """登记发布桥接所解释的固定映射版本，规则变化必须以新版本表达。"""
    code = f"{dataset_code}.release-bridge"
    methodology_id = uuid5(
        NAMESPACE_URL,
        f"quant-v2:methodology:{code}:{_METHODOLOGY_VERSION}",
    )
    session.execute(
        pg_insert(MethodologyVersion)
        .values(
            methodology_version_id=methodology_id,
            code=code,
            version=_METHODOLOGY_VERSION,
            semantic_family=semantic_family,
            kind="reported",
            formula_hash=hashlib.sha256(mapping_version.encode()).hexdigest(),
            effective_from=None,
            effective_to=None,
            status="validated",
            documentation_ref="docs/service-data-sync/0031-data-operations-control-plane/index.html",
        )
        .on_conflict_do_nothing(index_elements=("code", "version"))
    )
    return UUID(
        str(
            session.execute(
                select(MethodologyVersion.methodology_version_id).where(
                    MethodologyVersion.code == code,
                    MethodologyVersion.version == _METHODOLOGY_VERSION,
                )
            ).scalar_one()
        )
    )


def _record_normalization_run(
    session: Session,
    *,
    dataset_id: UUID,
    partition_key: str,
    source_batch_id: UUID,
    mapping_version: str,
    records: Sequence[CanonicalLineageRecord],
    now: datetime,
) -> UUID:
    """为当前快照登记确定性运行，只使用来源摘要而不读取或复制成功 raw。"""
    source = (
        session.execute(
            select(
                SourceBatch.run_id,
                SourceBatch.adapter_version,
                SourceBatch.schema_fingerprint,
                SourceBatch.payload_sha256,
            ).where(SourceBatch.source_batch_id == source_batch_id)
        )
        .mappings()
        .one()
    )
    input_set_hash = hashlib.sha256(
        ":".join(
            (
                str(source["payload_sha256"]),
                str(source_batch_id),
                _snapshot_hash(records),
            )
        ).encode()
    ).hexdigest()
    inserted = session.execute(
        pg_insert(NormalizationRun)
        .values(
            normalization_run_id=uuid4(),
            dataset_id=dataset_id,
            partition_key=partition_key,
            run_id=UUID(str(source["run_id"])),
            adapter_version=str(source["adapter_version"]),
            schema_fingerprint=str(source["schema_fingerprint"]),
            mapping_version=mapping_version,
            input_set_hash=input_set_hash,
            status="passed",
            started_at=now,
            finished_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=("dataset_id", "partition_key", "input_set_hash", "mapping_version")
        )
        .returning(NormalizationRun.normalization_run_id)
    ).scalar_one_or_none()
    if inserted is not None:
        return UUID(str(inserted))
    return UUID(
        str(
            session.execute(
                select(NormalizationRun.normalization_run_id).where(
                    NormalizationRun.dataset_id == dataset_id,
                    NormalizationRun.partition_key == partition_key,
                    NormalizationRun.input_set_hash == input_set_hash,
                    NormalizationRun.mapping_version == mapping_version,
                )
            ).scalar_one()
        )
    )


def _checkpoint_fencing_token(
    session: Session,
    *,
    dataset_id: UUID,
    partition_key: str,
) -> int:
    """读取桥接 release 的当前 CAS 水位，过期 worker 会在统一发布器中被拒绝。"""
    value = session.execute(
        select(CanonicalCheckpoint.fencing_token).where(
            CanonicalCheckpoint.dataset_id == dataset_id,
            CanonicalCheckpoint.partition_key == partition_key,
            CanonicalCheckpoint.checkpoint_kind == "published",
        )
    ).scalar_one_or_none()
    return 0 if value is None else int(value)


def _snapshot_hash(records: Sequence[CanonicalLineageRecord]) -> str:
    """按真实记录键、内容与来源批次计算运行输入摘要，顺序不会影响结果。"""
    payload = "\n".join(
        f"{record.record_key_hash}:{record.content_hash}:{record.source_batch_id}"
        for record in sorted(records, key=lambda item: item.record_key_hash)
    )
    return hashlib.sha256(payload.encode()).hexdigest()
