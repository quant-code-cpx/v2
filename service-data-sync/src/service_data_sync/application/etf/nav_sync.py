"""`ETF` `P0` 单位净值和累计净值同步。

净值类型由来源明确标注，不能用 `IOPV`、收盘价或另一种净值补齐；最终性和币种随事实一同发布。
成功路径不归档原始字节，只有解码、质量或发布失败时才保存排障证据。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.dataset_availability import DatasetAvailabilityRepository
from service_data_sync.application.ports.etf_market import (
    EtfNavRepository,
    EtfSourceObservation,
)
from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.domain.etf import EtfIdentifier, EtfNav

_CAPABILITY = "fund.etf.nav.1d.reported"
_DATASET = "fund.etf.nav.1d.reported"
_SCHEMA = "quant-v2.etf-nav.v1"


@dataclass(frozen=True, slots=True)
class EtfNavSyncResult:
    """返回 ETF NAV 发布版本和写入计数，不将来源发布时间伪装为精确值。"""

    etf: EtfIdentifier
    data_version: UUID | None
    inserted_count: int
    unchanged_count: int
    availability: str = "available"


class EtfNavSyncService:
    """同步单位和累计 NAV，来源无可信公告时间时由持久化层采用 observed-only PIT。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: EtfNavRepository,
        raw_payload_store: RawPayloadStore,
        availability_repository: DatasetAvailabilityRepository | None = None,
    ) -> None:
        """接收 adapter、NAV 发布仓储与双证据归档端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store
        self._availability_repository = availability_repository

    async def sync(self, *, etf: EtfIdentifier, start: date, end: date) -> EtfNavSyncResult:
        """同步包含边界的 NAV 窗口；来源不可用或合法空集按成功空结果返回。"""
        if start > end:
            raise ValueError("start must not be after end")
        if _CAPABILITY not in self._source.capabilities():
            return self._availability_result(
                etf=etf,
                start=start,
                end=end,
                availability="source_unavailable",
                reason_code="capability_not_configured",
                provider_id=self._source.provider_id,
                observed_at=datetime.now(UTC),
            )
        try:
            batch = await self._source.fetch(
                SourceRequest(
                    capability=_CAPABILITY,
                    parameters=(
                        ("etf", etf.qualified_key),
                        ("start", start.isoformat()),
                        ("end", end.isoformat()),
                    ),
                )
            )
        except ProviderError as error:
            if error.code not in {
                ProviderErrorCode.UNAVAILABLE,
                ProviderErrorCode.RATE_LIMITED,
                ProviderErrorCode.AUTHENTICATION,
                ProviderErrorCode.INVALID_REQUEST,
            }:
                raise
            return self._availability_result(
                etf=etf,
                start=start,
                end=end,
                availability="source_unavailable",
                reason_code=error.code.value,
                provider_id=self._source.provider_id,
                observed_at=datetime.now(UTC),
            )
        navs = decode_etf_nav_batch(batch.payload, etf=etf)
        if not navs:
            return self._availability_result(
                etf=etf,
                start=start,
                end=end,
                availability="empty",
                reason_code="no_matching_facts",
                provider_id=batch.provider_id,
                observed_at=batch.observed_at,
            )
        published = self._repository.publish_navs(
            etf=etf,
            navs=navs,
            source=_archive_batch(batch=batch, payload_store=self._raw_payload_store),
        )
        if self._availability_repository is not None:
            self._availability_repository.clear(
                dataset=_DATASET,
                partition_key=_partition_key(etf=etf, start=start, end=end),
                cleared_at=datetime.now(UTC),
            )
        return EtfNavSyncResult(
            etf=published.etf,
            data_version=published.data_version,
            inserted_count=published.inserted_count,
            unchanged_count=published.unchanged_count,
        )

    def _availability_result(
        self,
        *,
        etf: EtfIdentifier,
        start: date,
        end: date,
        availability: str,
        reason_code: str,
        provider_id: str | None,
        observed_at: datetime,
    ) -> EtfNavSyncResult:
        """记录非事实 NAV 结果，并将其投影为消费者可显示的成功空 DTO。"""
        if self._availability_repository is not None:
            self._availability_repository.record(
                dataset=_DATASET,
                partition_key=_partition_key(etf=etf, start=start, end=end),
                availability=availability,
                reason_code=reason_code,
                provider_id=provider_id,
                observed_at=observed_at,
            )
        return EtfNavSyncResult(
            etf=etf,
            data_version=None,
            inserted_count=0,
            unchanged_count=0,
            availability=availability,
        )


def decode_etf_nav_batch(payload: bytes, *, etf: EtfIdentifier) -> tuple[EtfNav, ...]:
    """解析一个 ETF 的单位/累计净值，拒绝 IOPV、重复键和身份不一致。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _schema_error("ETF NAV payload is not JSON") from error
    if not isinstance(decoded, dict) or decoded.get("schema") != _SCHEMA:
        raise _schema_error("unexpected ETF NAV schema")
    if decoded.get("etf") != etf.qualified_key:
        raise _schema_error("ETF NAV identity mismatch")
    records = decoded.get("navs")
    if not isinstance(records, list):
        raise _schema_error("ETF NAV payload has no records")
    try:
        navs = tuple(_nav(record) for record in records)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise _schema_error("ETF NAV value is invalid") from error
    if len({(item.nav_date, item.nav_kind) for item in navs}) != len(navs):
        raise _schema_error("ETF NAV payload has duplicate date and kind")
    return tuple(sorted(navs, key=lambda item: (item.nav_date, item.nav_kind)))


def _partition_key(*, etf: EtfIdentifier, start: date, end: date) -> str:
    """用上市工具与精确请求窗口隔离 NAV 空观测，禁止跨日期窗口复用。"""
    return f"{etf.qualified_key}:{start.isoformat()}:{end.isoformat()}"


def _archive_batch(*, batch: ProviderBatch, payload_store: RawPayloadStore) -> EtfSourceObservation:
    """分别保存来源 raw 和标准 JSON，确保净值修订可追溯并能在 adapter 升级后重放。"""
    raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
    raw_digest = hashlib.sha256(raw_payload).hexdigest()
    normalized_digest = hashlib.sha256(batch.payload).hexdigest()
    prefix = f"etf/{batch.capability}/{batch.provider_id}/{batch.observed_at:%Y/%m/%d}"
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
    return EtfSourceObservation(
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


def _nav(value: object) -> EtfNav:
    """将标准 JSON 行转换为来源直报 NAV，不以交易价格或 IOPV 补齐字段。"""
    if not isinstance(value, dict):
        raise ValueError("ETF NAV row is not an object")
    return EtfNav(
        nav_date=date.fromisoformat(_required(value, "navDate")),
        nav_kind=_required(value, "navKind"),
        nav_value=Decimal(_required(value, "nav")),
        currency=_required(value, "currency"),
        finality=_required(value, "finality"),
    )


def _required(value: dict[str, object], key: str) -> str:
    """读取必填文本，拒绝 `null`、空白与 pandas 缺失字面量。"""
    normalized = _optional_text(value.get(key))
    if normalized is None:
        raise ValueError(f"{key} is required")
    return normalized


def _optional_text(value: object) -> str | None:
    """将真实空值保留为空，避免净值类型或终态字段出现伪字符串。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return None if not normalized or normalized.lower() in {"nan", "none", "nat"} else normalized


def _schema_error(message: str) -> ProviderError:
    """统一返回不可重试 schema 错误，避免错误数据因网络重试被反复归档。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)
