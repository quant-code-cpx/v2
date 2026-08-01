"""AKShare 沪深港通市场统计的 SQLAlchemy research-only 仓储。

本仓储只保存来源观察、digest-only manifest、规范化、质量和 research 行。它不导入或写入
官方港通 repository、`DatasetRelease`、`DatasetPublication`、PIT revision 或 bundle。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from service_data_sync.application.ports.stock_connect_market_stat_research import (
    StockConnectMarketStatResearchRecord,
    StockConnectMarketStatResearchRepository,
    StockConnectMarketStatResearchSourceObservation,
    StoredStockConnectMarketStatResearchBatch,
)
from service_data_sync.domain.stock_connect import StockConnectChannel
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import (
    CanonicalDataset,
    DataSource,
    NormalizationRun,
    NormalizedRecordManifest,
    QualityEvaluation,
    QualityResult,
    RawPayloadManifest,
    SourceDataset,
)
from service_data_sync.infrastructure.database.models.market import (
    StockConnectMarketStatResearchBatch,
    StockConnectMarketStatResearchObservation,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.persistence.source_batch import record_source_observation

_CAPABILITY = "market.stock_connect.market_stat.reported"
_DATASET_CODE = "market.stock_connect.market_stat.research"
_MAPPING_VERSION = "stock-connect-market-stat-research-v1"
_QUALITY_POLICY = "stock-connect.market-stat.research"
_UPSTREAM_SOURCE = "eastmoney.stock-connect"
_SOURCE_CODE = "eastmoney-stock-connect"
_SOURCE_DATASET_CODE = f"{_UPSTREAM_SOURCE}:{_CAPABILITY}"


class SqlAlchemyStockConnectMarketStatResearchRepository(StockConnectMarketStatResearchRepository):
    """将 AKShare 标准市场统计写入私有 research 表，不形成正式消费者可见版本。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存服务拥有的数据库会话工厂，ORM 与事务细节不穿过应用端口。"""
        self._database = database

    def record_market_statistics(
        self,
        *,
        channel: StockConnectChannel,
        records: Sequence[StockConnectMarketStatResearchRecord],
        source: StockConnectMarketStatResearchSourceObservation,
    ) -> StoredStockConnectMarketStatResearchBatch:
        """原子写入一批来源观察、双摘要 manifest、规范化、质量和单日 research 记录。"""
        records = tuple(records)
        _validate_input(channel=channel, records=records, source=source)
        now = datetime.now(UTC)
        with self._database.transaction() as session:
            dataset_id = _ensure_dataset(session, now=now)
            source_batch_id, run_id = _record_source_evidence(session, source=source, now=now)
            normalization_run_id = _record_normalization(
                session,
                dataset_id=dataset_id,
                run_id=run_id,
                source_batch_id=source_batch_id,
                source=source,
                now=now,
            )
            quality_status, missing_metadata_count = _quality_status(records)
            research_batch_id = uuid4()
            session.execute(
                insert(StockConnectMarketStatResearchBatch).values(
                    research_batch_id=research_batch_id,
                    dataset_id=dataset_id,
                    source_batch_id=source_batch_id,
                    normalization_run_id=normalization_run_id,
                    channel=channel.channel,
                    direction=channel.direction,
                    observed_at=source.observed_at,
                    record_count=len(records),
                    normalized_payload_sha256=source.normalized_payload_sha256,
                    quality_status=quality_status,
                    status="research",
                    created_at=now,
                )
            )
            _record_observations(
                session,
                research_batch_id=research_batch_id,
                channel=channel,
                records=records,
            )
            _record_normalized_manifest(
                session,
                normalization_run_id=normalization_run_id,
                research_batch_id=research_batch_id,
                channel=channel,
                records=records,
            )
            _record_quality(
                session,
                dataset_id=dataset_id,
                normalization_run_id=normalization_run_id,
                source_batch_id=source_batch_id,
                channel=channel,
                record_count=len(records),
                missing_metadata_count=missing_metadata_count,
                quality_status=quality_status,
                now=now,
            )
        return StoredStockConnectMarketStatResearchBatch(
            research_batch_id=research_batch_id,
            source_batch_id=source_batch_id,
            inserted_count=len(records),
            quality_status=quality_status,
        )


def _validate_input(
    *,
    channel: StockConnectChannel,
    records: tuple[StockConnectMarketStatResearchRecord, ...],
    source: StockConnectMarketStatResearchSourceObservation,
) -> None:
    """验证 research 边界、唯一日期与 digest-only URI，阻止成功路径误写 S3 或正式来源。"""
    del channel
    if source.capability != _CAPABILITY:
        raise ValueError("stock-connect market-stat research capability is invalid")
    if source.upstream_source != _UPSTREAM_SOURCE:
        raise ValueError("stock-connect market-stat research upstream source is invalid")
    if (
        source.raw_uri != f"unretained://sha256/{source.raw_payload_sha256}"
        or source.normalized_uri != (f"unretained://sha256/{source.normalized_payload_sha256}")
    ):
        raise ValueError("stock-connect market-stat research requires digest-only source manifests")
    if len({record.trade_date for record in records}) != len(records):
        raise ValueError("stock-connect market-stat research records must have unique trade dates")


def _ensure_dataset(session: Session, *, now: datetime) -> UUID:
    """幂等登记唯一 research 数据集，状态永不提升为 candidate 或 production。"""
    dataset_id = uuid5(NAMESPACE_URL, f"quant-v2:canonical-dataset:{_DATASET_CODE}:1")
    session.execute(
        pg_insert(CanonicalDataset)
        .values(
            dataset_id=dataset_id,
            code=_DATASET_CODE,
            schema_version=1,
            domain="stock_connect",
            grain="AKShare source batch + channel + direction + reported trade date",
            status="research",
            owner_service="service-data-sync",
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=("code", "schema_version"))
    )
    return UUID(
        str(
            session.execute(
                select(CanonicalDataset.dataset_id).where(
                    CanonicalDataset.code == _DATASET_CODE,
                    CanonicalDataset.schema_version == 1,
                )
            ).scalar_one()
        )
    )


def _record_source_evidence(
    session: Session,
    *,
    source: StockConnectMarketStatResearchSourceObservation,
    now: datetime,
) -> tuple[UUID, UUID]:
    """写入 research-only 上游产品、source batch 和 raw/normalized 摘要清单，不写来源字节。"""
    source_dataset_id = _ensure_source_dataset(session)
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
    run_id = session.execute(
        select(SourceBatch.run_id).where(SourceBatch.source_batch_id == source_batch_id)
    ).scalar_one()
    return source_batch_id, UUID(str(run_id))


def _ensure_source_dataset(session: Session) -> UUID:
    """登记 Eastmoney 产品为 research-only 上游，AKShare 仅保留为技术 adapter 身份。"""
    source_id = uuid5(NAMESPACE_URL, f"quant-v2:data-source:{_SOURCE_CODE}")
    source_dataset_id = uuid5(NAMESPACE_URL, f"quant-v2:source-dataset:{_SOURCE_DATASET_CODE}")
    session.execute(
        pg_insert(DataSource)
        .values(
            source_id=source_id,
            code=_SOURCE_CODE,
            legal_name=_UPSTREAM_SOURCE,
            source_kind="platform",
            timezone="Asia/Shanghai",
            rights_status="research",
            rights_evidence_ref=None,
        )
        .on_conflict_do_nothing(index_elements=("code",))
    )
    session.execute(
        pg_insert(SourceDataset)
        .values(
            source_dataset_id=source_dataset_id,
            source_id=source_id,
            code=_SOURCE_DATASET_CODE,
            capability=_CAPABILITY,
            native_grain="channel + direction + reported trade date",
            native_unit_json={
                "amountFields": "adapter-normalized CNY base units when supplied",
                "currency": "source-reported ISO code",
                "turnoverAmount": "not inferred when source omits it",
            },
            history_from=None,
            history_to=None,
            license_scope="research_only",
            active=True,
        )
        .on_conflict_do_nothing(index_elements=("source_id", "code"))
    )
    return UUID(
        str(
            session.execute(
                select(SourceDataset.source_dataset_id).where(
                    SourceDataset.source_id == source_id,
                    SourceDataset.code == _SOURCE_DATASET_CODE,
                )
            ).scalar_one()
        )
    )


def _record_normalization(
    session: Session,
    *,
    dataset_id: UUID,
    run_id: UUID,
    source_batch_id: UUID,
    source: StockConnectMarketStatResearchSourceObservation,
    now: datetime,
) -> UUID:
    """登记一个绑定精确来源批次的规范化运行，重跑不会覆盖前次研究证据。"""
    normalization_run_id = uuid4()
    input_set_hash = hashlib.sha256(
        f"{source.raw_payload_sha256}:{source.normalized_payload_sha256}".encode()
    ).hexdigest()
    session.execute(
        insert(NormalizationRun).values(
            normalization_run_id=normalization_run_id,
            dataset_id=dataset_id,
            partition_key=f"research-source-batch:{source_batch_id}",
            run_id=run_id,
            adapter_version=source.adapter_version,
            schema_fingerprint=source.schema_fingerprint,
            mapping_version=_MAPPING_VERSION,
            input_set_hash=input_set_hash,
            status="passed",
            started_at=now,
            finished_at=now,
        )
    )
    return normalization_run_id


def _quality_status(
    records: tuple[StockConnectMarketStatResearchRecord, ...],
) -> tuple[str, int]:
    """把空批次或缺来源元数据标为 warned，但不把未知字段默认为失败或零。"""
    missing_metadata_count = sum(
        record.currency is None or record.availability_status is None for record in records
    )
    return (
        ("passed" if records and missing_metadata_count == 0 else "warned"),
        missing_metadata_count,
    )


def _record_observations(
    session: Session,
    *,
    research_batch_id: UUID,
    channel: StockConnectChannel,
    records: tuple[StockConnectMarketStatResearchRecord, ...],
) -> None:
    """写入每个交易日的可选来源字段，绝不计算缺失成交额、净买额或额度。"""
    if not records:
        return
    session.execute(
        insert(StockConnectMarketStatResearchObservation).values(
            [
                {
                    "research_batch_id": research_batch_id,
                    "trade_date": record.trade_date,
                    "channel": channel.channel,
                    "direction": channel.direction,
                    "buy_amount": record.buy_amount,
                    "sell_amount": record.sell_amount,
                    "turnover_amount": record.turnover_amount,
                    "net_buy_amount": record.net_buy_amount,
                    "quota_balance": record.quota_balance,
                    "currency": record.currency,
                    "availability_status": record.availability_status,
                    "field_availability": (
                        None
                        if record.field_availability is None
                        else dict(record.field_availability)
                    ),
                    "content_hash": _record_content_hash(record),
                }
                for record in records
            ]
        )
    )


def _record_normalized_manifest(
    session: Session,
    *,
    normalization_run_id: UUID,
    research_batch_id: UUID,
    channel: StockConnectChannel,
    records: tuple[StockConnectMarketStatResearchRecord, ...],
) -> None:
    """把每条标准记录绑定到 research 主键，审计索引不构成消费者查询或发布。"""
    if not records:
        return
    session.execute(
        insert(NormalizedRecordManifest).values(
            [
                {
                    "normalization_run_id": normalization_run_id,
                    "record_key_hash": _record_key_hash(channel, record),
                    "canonical_table": "stock_connect_market_stat_research_observation",
                    "canonical_pk": {
                        "researchBatchId": str(research_batch_id),
                        "tradeDate": record.trade_date.isoformat(),
                    },
                    "content_hash": _record_content_hash(record),
                    "disposition": "accepted",
                }
                for record in records
            ]
        )
    )


def _record_quality(
    session: Session,
    *,
    dataset_id: UUID,
    normalization_run_id: UUID,
    source_batch_id: UUID,
    channel: StockConnectChannel,
    record_count: int,
    missing_metadata_count: int,
    quality_status: str,
    now: datetime,
) -> None:
    """记录研究数据的可见字段与批次非空质量，不把 warned 结果提升为正式发布。"""
    evaluation_id = uuid4()
    partition_key = f"research-source-batch:{source_batch_id}"
    session.execute(
        insert(QualityEvaluation).values(
            evaluation_id=evaluation_id,
            dataset_id=dataset_id,
            partition_key=partition_key,
            normalization_run_id=normalization_run_id,
            policy_code=_QUALITY_POLICY,
            policy_version=1,
            status=quality_status,
            score=None,
            evaluated_at=now,
        )
    )
    session.execute(
        insert(QualityResult).values(
            [
                {
                    "evaluation_id": evaluation_id,
                    "rule_code": "stock-connect-market-stat.research.records-present",
                    "severity": "warn",
                    "passed": record_count > 0,
                    "actual_value": Decimal(record_count),
                    "threshold_value": Decimal(1),
                    "sample_json": {
                        "channel": channel.channel,
                        "direction": channel.direction,
                    },
                    "affected_count": 0 if record_count > 0 else 1,
                },
                {
                    "evaluation_id": evaluation_id,
                    "rule_code": "stock-connect-market-stat.research.source-metadata",
                    "severity": "warn",
                    "passed": missing_metadata_count == 0,
                    "actual_value": Decimal(missing_metadata_count),
                    "threshold_value": Decimal(0),
                    "sample_json": None,
                    "affected_count": missing_metadata_count,
                },
            ]
        )
    )


def _record_key_hash(
    channel: StockConnectChannel,
    record: StockConnectMarketStatResearchRecord,
) -> str:
    """计算通道、方向和交易日业务键摘要，避免审计表携带多余来源正文。"""
    return hashlib.sha256(
        f"{channel.channel}:{channel.direction}:{record.trade_date.isoformat()}".encode()
    ).hexdigest()


def _record_content_hash(record: StockConnectMarketStatResearchRecord) -> str:
    """计算所有可选字段的稳定摘要，空值仍参与哈希而不会被解释为零。"""
    return _hash_json(
        {
            "tradeDate": record.trade_date.isoformat(),
            "buyAmount": _decimal_json(record.buy_amount),
            "sellAmount": _decimal_json(record.sell_amount),
            "turnoverAmount": _decimal_json(record.turnover_amount),
            "netBuyAmount": _decimal_json(record.net_buy_amount),
            "quotaBalance": _decimal_json(record.quota_balance),
            "currency": record.currency,
            "availabilityStatus": record.availability_status,
            "fieldAvailability": (
                None if record.field_availability is None else dict(record.field_availability)
            ),
        }
    )


def _hash_json(value: object) -> str:
    """以稳定 JSON 编码生成 SHA-256，避免字典顺序改变研究证据摘要。"""
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _decimal_json(value: Decimal | None) -> str | None:
    """将精确十进制编码为普通文本，避免科学计数法影响内容摘要。"""
    return None if value is None else format(value, "f")


__all__ = ["SqlAlchemyStockConnectMarketStatResearchRepository"]
