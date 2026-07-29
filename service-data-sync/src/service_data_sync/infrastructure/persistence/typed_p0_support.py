"""强类型 P0 dataset 共用的来源、方法学、标准化运行和 manifest 持久化辅助。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from service_data_sync.infrastructure.database.models.canonical import (
    CanonicalDataset,
    DataSource,
    MethodologyVersion,
    NormalizationRun,
    NormalizedRecordManifest,
    RawPayloadManifest,
    SourceDataset,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.persistence.source_batch import record_source_observation


class TypedP0SourceObservation(Protocol):
    """定义不同领域 raw-first 同步结果共享的来源观察字段。"""

    @property
    def provider_id(self) -> str:
        """返回提供方稳定标识。"""

        ...

    @property
    def capability(self) -> str:
        """返回本次观察对应能力标识。"""

        ...

    @property
    def raw_payload_sha256(self) -> str:
        """返回失败排障所需 raw 载荷摘要。"""

        ...

    @property
    def raw_uri(self) -> str:
        """返回 raw 载荷对象地址。"""

        ...

    @property
    def raw_content_type(self) -> str:
        """返回 raw 载荷内容类型。"""

        ...

    @property
    def raw_byte_size(self) -> int:
        """返回 raw 载荷字节数。"""

        ...

    @property
    def normalized_payload_sha256(self) -> str:
        """返回标准载荷摘要。"""

        ...

    @property
    def normalized_uri(self) -> str:
        """返回标准载荷对象地址。"""

        ...

    @property
    def normalized_content_type(self) -> str:
        """返回标准载荷内容类型。"""

        ...

    @property
    def normalized_byte_size(self) -> int:
        """返回标准载荷字节数。"""

        ...

    @property
    def observed_at(self) -> datetime:
        """返回来源观察时间。"""

        ...

    @property
    def upstream_source(self) -> str:
        """返回上游原始来源说明。"""

        ...

    @property
    def adapter_version(self) -> str:
        """返回 adapter 版本。"""

        ...

    @property
    def schema_fingerprint(self) -> str:
        """返回标准载荷 schema 指纹。"""

        ...


@dataclass(frozen=True, slots=True)
class TypedP0SourceApproval:
    """记录能够发布的来源权利边界；没有批准项时保持 fail-closed。"""

    provider_id: str
    source_code: str
    legal_name: str
    source_kind: str
    rights_status: str
    license_scope: str

    def __post_init__(self) -> None:
        """拒绝任何缺失的权利归属或许可范围，技术 adapter 不能替代数据源准入。"""
        if not all(
            value.strip()
            for value in (
                self.provider_id,
                self.source_code,
                self.legal_name,
                self.source_kind,
                self.rights_status,
                self.license_scope,
            )
        ):
            raise ValueError("typed P0 source approval is incomplete")


def ensure_dataset(session: Session, *, code: str, domain: str, grain: str, now: datetime) -> UUID:
    """幂等登记一个 schema v1 canonical dataset，状态保持 candidate 直到独立门禁提升。"""
    dataset_id = uuid5(NAMESPACE_URL, f"quant-v2:canonical-dataset:{code}:1")
    session.execute(
        pg_insert(CanonicalDataset)
        .values(
            dataset_id=dataset_id,
            code=code,
            schema_version=1,
            domain=domain,
            grain=grain,
            status="candidate",
            owner_service="service-data-sync",
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=("code", "schema_version"))
    )
    return UUID(
        str(
            session.execute(
                select(CanonicalDataset.dataset_id).where(CanonicalDataset.code == code)
            ).scalar_one()
        )
    )


def ensure_methodology(
    session: Session,
    *,
    code: str,
    semantic_family: str,
    mapping_version: str,
    documentation_ref: str,
) -> UUID:
    """登记冻结映射方法学 v1，输入语义变化只能追加方法学版本而非修改旧 release。"""
    methodology_id = uuid5(NAMESPACE_URL, f"quant-v2:methodology:{code}:1")
    session.execute(
        pg_insert(MethodologyVersion)
        .values(
            methodology_version_id=methodology_id,
            code=code,
            version=1,
            semantic_family=semantic_family,
            kind="reported",
            formula_hash=hashlib.sha256(mapping_version.encode()).hexdigest(),
            effective_from=None,
            effective_to=None,
            status="validated",
            documentation_ref=documentation_ref,
        )
        .on_conflict_do_nothing(index_elements=("code", "version"))
    )
    return UUID(
        str(
            session.execute(
                select(MethodologyVersion.methodology_version_id).where(
                    MethodologyVersion.code == code, MethodologyVersion.version == 1
                )
            ).scalar_one()
        )
    )


def ensure_source_dataset(
    session: Session,
    *,
    approval: TypedP0SourceApproval,
    capability: str,
    native_grain: str,
) -> UUID:
    """登记已经批准的真实来源与 capability 产品，adapter 名称不作为业务来源身份。"""
    source_id = uuid5(NAMESPACE_URL, f"quant-v2:data-source:{approval.source_code}")
    code = f"{approval.source_code}:{capability}"
    source_dataset_id = uuid5(NAMESPACE_URL, f"quant-v2:source-dataset:{code}")
    session.execute(
        pg_insert(DataSource)
        .values(
            source_id=source_id,
            code=approval.source_code,
            legal_name=approval.legal_name,
            source_kind=approval.source_kind,
            timezone="Asia/Shanghai",
            rights_status=approval.rights_status,
            rights_evidence_ref=None,
        )
        .on_conflict_do_nothing(index_elements=("code",))
    )
    session.execute(
        pg_insert(SourceDataset)
        .values(
            source_dataset_id=source_dataset_id,
            source_id=source_id,
            code=code,
            capability=capability,
            native_grain=native_grain,
            native_unit_json={},
            history_from=None,
            history_to=None,
            license_scope=approval.license_scope,
            active=True,
        )
        .on_conflict_do_nothing(index_elements=("source_id", "code"))
    )
    return UUID(
        str(
            session.execute(
                select(SourceDataset.source_dataset_id).where(
                    SourceDataset.source_id == source_id, SourceDataset.code == code
                )
            ).scalar_one()
        )
    )


def record_source_batch(
    session: Session,
    *,
    source: TypedP0SourceObservation,
    source_dataset_id: UUID,
    now: datetime,
) -> UUID:
    """保存一次来源观察以及 raw/normalized 两份对象 manifest，二者都可用于审计和重放。"""
    source_batch_id = record_source_observation(
        session,
        provider_id=source.provider_id,
        capability=source.capability,
        source_payload_sha256=source.raw_payload_sha256,
        raw_uri=source.raw_uri,
        observed_at=source.observed_at,
        created_at=now,
        upstream_source=source.upstream_source,
        adapter_version=source.adapter_version,
        schema_fingerprint=source.schema_fingerprint,
        source_dataset_id=source_dataset_id,
    )
    session.execute(
        insert(RawPayloadManifest).values(
            [
                {
                    "raw_payload_id": uuid4(),
                    "source_batch_id": source_batch_id,
                    "sequence_no": 1,
                    "role": "raw",
                    "object_uri": source.raw_uri,
                    "sha256": source.raw_payload_sha256,
                    "content_type": source.raw_content_type,
                    "byte_size": source.raw_byte_size,
                    "fetched_at": source.observed_at,
                },
                {
                    "raw_payload_id": uuid4(),
                    "source_batch_id": source_batch_id,
                    "sequence_no": 1,
                    "role": "normalized",
                    "object_uri": source.normalized_uri,
                    "sha256": source.normalized_payload_sha256,
                    "content_type": source.normalized_content_type,
                    "byte_size": source.normalized_byte_size,
                    "fetched_at": source.observed_at,
                },
            ]
        )
    )
    return source_batch_id


def record_normalization_run(
    session: Session,
    *,
    dataset_id: UUID,
    partition_key: str,
    source: TypedP0SourceObservation,
    source_batch_id: UUID,
    mapping_version: str,
    now: datetime,
) -> UUID:
    """建立或复用输入摘要相同的标准化运行，确保幂等重放获得同一审计身份。"""
    run_id = UUID(
        str(
            session.execute(
                select(SourceBatch.run_id).where(SourceBatch.source_batch_id == source_batch_id)
            ).scalar_one()
        )
    )
    input_set_hash = hashlib.sha256(
        f"{source.raw_payload_sha256}:{source.normalized_payload_sha256}".encode()
    ).hexdigest()
    inserted = session.execute(
        pg_insert(NormalizationRun)
        .values(
            normalization_run_id=uuid4(),
            dataset_id=dataset_id,
            partition_key=partition_key,
            run_id=run_id,
            adapter_version=source.adapter_version,
            schema_fingerprint=source.schema_fingerprint,
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


def record_manifest(
    session: Session,
    *,
    normalization_run_id: UUID,
    record_key_hash: str,
    canonical_table: str,
    canonical_pk: dict[str, str],
    content_hash: str,
) -> None:
    """追加已接受强类型 revision 的 manifest，连接标准化输入与 canonical 主键。"""
    session.execute(
        insert(NormalizedRecordManifest).values(
            normalization_run_id=normalization_run_id,
            record_key_hash=record_key_hash,
            canonical_table=canonical_table,
            canonical_pk=canonical_pk,
            content_hash=content_hash,
            disposition="accepted",
        )
    )
