"""公司公告、业绩预告和业绩快报 `P0` 同步。

一个有界日期窗口先从中立来源取得同批公告与指标，再以“每项指标都有同批公告证据”为前提原子发布。
成功同步只保存 `canonical` 事实与来源摘要；抓取后解析、质量或发布失败时才把字节写入私有失败证据区。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from service_data_sync.application.ports.corporate_events import (
    CorporateEventsRepository,
    CorporateSourceObservation,
)
from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.domain.corporate import (
    DisclosureDocument,
    EarningsExpressMetric,
    EarningsGuidanceMetric,
)

_CAPABILITY = "corporate.disclosure.earnings.p0"
_SCHEMA = "quant-v2.corporate-earnings-events.v1"


@dataclass(frozen=True, slots=True)
class CorporateEventsSyncResult:
    """返回公告域发布版本和变更计数；无公告时返回成功空结果。"""

    data_version: UUID | None
    inserted_count: int
    unchanged_count: int
    availability: str = "available"


class CorporateEventsSyncService:
    """同步官方公告目录及 P0 业绩事件，严格要求结构化值引用同批公告证据。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: CorporateEventsRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收中立数据源、事件仓储和失败排障载荷端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(self, *, start: date, end: date) -> CorporateEventsSyncResult:
        """同步有界公告窗口；能力或日期无效时在访问 Provider 前停止。"""
        if start > end:
            raise ValueError("start must not be after end")
        if _CAPABILITY not in self._source.capabilities():
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "unsupported corporate earnings capability",
                retryable=False,
            )
        batch = await self._source.fetch(
            SourceRequest(
                capability=_CAPABILITY,
                parameters=(("start", start.isoformat()), ("end", end.isoformat())),
            )
        )
        documents, guidance_metrics, express_metrics = decode_corporate_events_batch(batch.payload)
        if not documents:
            # 该窗口无公告是可显示的正常结果，不应触发失败留证或空发布。
            return CorporateEventsSyncResult(
                data_version=None,
                inserted_count=0,
                unchanged_count=0,
                availability="empty",
            )
        published = self._repository.publish(
            documents=documents,
            guidance_metrics=guidance_metrics,
            express_metrics=express_metrics,
            source=_archive_batch(batch=batch, payload_store=self._raw_payload_store),
        )
        return CorporateEventsSyncResult(
            data_version=published.data_version,
            inserted_count=published.inserted_count,
            unchanged_count=published.unchanged_count,
        )


def decode_corporate_events_batch(
    payload: bytes,
) -> tuple[
    tuple[DisclosureDocument, ...],
    tuple[EarningsGuidanceMetric, ...],
    tuple[EarningsExpressMetric, ...],
]:
    """解码标准载荷，要求每项业绩事实都能在同批官方公告目录找到证据。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _schema_error("corporate events payload is not JSON") from error
    if not isinstance(decoded, dict) or decoded.get("schema") != _SCHEMA:
        raise _schema_error("unexpected corporate events schema")
    _reject_unknown(decoded, {"schema", "documents", "guidanceMetrics", "expressMetrics"}, "root")
    raw_documents = decoded.get("documents")
    raw_guidance = decoded.get("guidanceMetrics", [])
    raw_express = decoded.get("expressMetrics", [])
    if not isinstance(raw_documents, list):
        raise _schema_error("corporate events payload has no documents")
    if not isinstance(raw_guidance, list) or not isinstance(raw_express, list):
        raise _schema_error("corporate events metrics must be arrays")
    try:
        documents = tuple(_document(item) for item in raw_documents)
        guidance_metrics = tuple(_guidance(item) for item in raw_guidance)
        express_metrics = tuple(_express(item) for item in raw_express)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise _schema_error("corporate events value is invalid") from error
    document_ids = {item.source_document_id for item in documents}
    if len(document_ids) != len(documents):
        raise _schema_error("corporate events payload has duplicate document identities")
    if any(
        item.source_document_id not in document_ids
        for item in (*guidance_metrics, *express_metrics)
    ):
        raise _schema_error("corporate metric lacks a same-batch disclosure document")
    if len({(item.source_document_id, item.metric_code) for item in guidance_metrics}) != len(
        guidance_metrics
    ):
        raise _schema_error("corporate events payload has duplicate guidance metrics")
    if len({(item.source_document_id, item.metric_code) for item in express_metrics}) != len(
        express_metrics
    ):
        raise _schema_error("corporate events payload has duplicate express metrics")
    return documents, guidance_metrics, express_metrics


def _document(value: object) -> DisclosureDocument:
    """将公告目录项转为文档证据，日期精度和安全可用时间由 adapter 显式给出。"""
    if not isinstance(value, dict):
        raise ValueError("document is not an object")
    _reject_unknown(
        value,
        {
            "sourceDocumentId",
            "securityCode",
            "title",
            "category",
            "officialUrl",
            "announcedOn",
            "sourceVisibleAt",
            "visibleTimePrecision",
            "publicUsableAt",
            "contentSha256",
        },
        "document",
    )
    return DisclosureDocument(
        source_document_id=_required(value, "sourceDocumentId"),
        source_security_code=_required(value, "securityCode"),
        title=_required(value, "title"),
        category=_required(value, "category"),
        official_url=_required(value, "officialUrl"),
        announced_on=date.fromisoformat(_required(value, "announcedOn")),
        source_visible_at=_optional_datetime(value.get("sourceVisibleAt")),
        visible_time_precision=_required(value, "visibleTimePrecision"),
        public_usable_at=_datetime(_required(value, "publicUsableAt")),
        content_sha256=_required(value, "contentSha256"),
    )


def _guidance(value: object) -> EarningsGuidanceMetric:
    """将一个预告指标转为领域值，保留上下界而不从单值填充缺失端。"""
    if not isinstance(value, dict):
        raise ValueError("guidance metric is not an object")
    _reject_unknown(
        value,
        {
            "sourceDocumentId",
            "securityCode",
            "reportPeriod",
            "guidanceType",
            "metricCode",
            "amountLow",
            "amountHigh",
            "yoyLow",
            "yoyHigh",
            "priorPeriodValue",
            "currency",
        },
        "guidance metric",
    )
    return EarningsGuidanceMetric(
        source_document_id=_required(value, "sourceDocumentId"),
        source_security_code=_required(value, "securityCode"),
        report_period=date.fromisoformat(_required(value, "reportPeriod")),
        guidance_type=_required(value, "guidanceType"),
        metric_code=_required(value, "metricCode"),
        amount_low=_optional_decimal(value.get("amountLow")),
        amount_high=_optional_decimal(value.get("amountHigh")),
        yoy_low=_optional_decimal(value.get("yoyLow")),
        yoy_high=_optional_decimal(value.get("yoyHigh")),
        prior_period_value=_optional_decimal(value.get("priorPeriodValue")),
        currency=_required(value, "currency"),
    )


def _express(value: object) -> EarningsExpressMetric:
    """将一项快报指标转为初步事实，阻止正式财报或未知量纲穿透 P0 边界。"""
    if not isinstance(value, dict):
        raise ValueError("express metric is not an object")
    _reject_unknown(
        value,
        {
            "sourceDocumentId",
            "securityCode",
            "reportPeriod",
            "metricCode",
            "currentValue",
            "priorValue",
            "unit",
            "currency",
            "preliminaryStatus",
        },
        "express metric",
    )
    return EarningsExpressMetric(
        source_document_id=_required(value, "sourceDocumentId"),
        source_security_code=_required(value, "securityCode"),
        report_period=date.fromisoformat(_required(value, "reportPeriod")),
        metric_code=_required(value, "metricCode"),
        current_value=Decimal(_required(value, "currentValue")),
        prior_value=_optional_decimal(value.get("priorValue")),
        unit=_required(value, "unit"),
        currency=_optional_text(value.get("currency")),
        preliminary_status=_required(value, "preliminaryStatus"),
    )


def _archive_batch(
    *, batch: ProviderBatch, payload_store: RawPayloadStore
) -> CorporateSourceObservation:
    """构造公告来源摘要；载荷仅在失败时归档，成功发布不增加对象存储。"""
    raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
    raw_digest = hashlib.sha256(raw_payload).hexdigest()
    normalized_digest = hashlib.sha256(batch.payload).hexdigest()
    prefix = f"corporate/{batch.capability}/{batch.provider_id}/{batch.observed_at:%Y/%m/%d}"
    raw_uri = payload_store.put(
        RawPayload(
            object_key=f"raw/{prefix}/{raw_digest}.json",
            content_sha256=raw_digest,
            content_type=batch.raw_content_type or batch.content_type,
            payload=raw_payload,
        )
    )
    normalized_uri = payload_store.put(
        RawPayload(
            object_key=f"normalized/{prefix}/{normalized_digest}.json",
            content_sha256=normalized_digest,
            content_type=batch.content_type,
            payload=batch.payload,
        )
    )
    return CorporateSourceObservation(
        provider_id=batch.provider_id,
        capability=batch.capability,
        raw_payload_sha256=raw_digest,
        raw_uri=raw_uri,
        raw_content_type=batch.raw_content_type or batch.content_type,
        raw_byte_size=len(raw_payload),
        normalized_payload_sha256=normalized_digest,
        normalized_uri=normalized_uri,
        normalized_content_type=batch.content_type,
        normalized_byte_size=len(batch.payload),
        observed_at=batch.observed_at,
        upstream_source=batch.upstream_source or batch.provider_id,
        adapter_version=batch.adapter_version,
        schema_fingerprint=batch.schema_fingerprint
        or hashlib.sha256(batch.capability.encode()).hexdigest(),
    )


def _required(value: dict[str, object], key: str) -> str:
    """读取非空字符串，拒绝 pandas 缺失字面量进入文档或事件身份字段。"""
    normalized = _optional_text(value.get(key))
    if normalized is None:
        raise ValueError(f"{key} is required")
    return normalized


def _optional_text(value: object) -> str | None:
    """标准化可选文本，保留真正空值而不把缺失标记变成业务内容。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return None if not normalized or normalized.lower() in {"nan", "none", "nat"} else normalized


def _optional_decimal(value: object) -> Decimal | None:
    """保持未披露字段为空，真实零值仍以精确十进制零保存。"""
    normalized = _optional_text(value)
    return None if normalized is None else Decimal(normalized)


def _datetime(value: str) -> datetime:
    """解析带时区 ISO 时间，阻止无时区字符串在服务器本地时区被悄然解释。"""
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("datetime must include timezone")
    return result


def _optional_datetime(value: object) -> datetime | None:
    """解析可选来源公开时间；日期精度记录由 `None` 与 precision 共同表达。"""
    normalized = _optional_text(value)
    return None if normalized is None else _datetime(normalized)


def _reject_unknown(value: dict[str, object], allowed: set[str], location: str) -> None:
    """拒绝 schema 未声明字段，避免 Provider 新增的事后列被无审计地静默忽略。"""
    unknown = set(value).difference(allowed)
    if unknown:
        raise ValueError(f"unexpected {location} fields: {', '.join(sorted(unknown))}")


def _schema_error(message: str) -> ProviderError:
    """将标准载荷问题归类为不可重试 schema 错误，避免错误数据反复触发任务重试。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)
