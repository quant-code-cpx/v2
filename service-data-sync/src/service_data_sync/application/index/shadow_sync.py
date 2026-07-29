"""指数 P0-A raw 归档、标准载荷解码与研究态观察编排。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.index_shadow import (
    IndexCatalogObservationEntry,
    IndexObservedSnapshotItem,
    IndexShadowRepository,
    IndexShadowSourceObservation,
    StoredIndexShadowObservation,
)
from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.domain.index import IndexAdministrator, IndexCapability, IndexIdentifier

_CATALOG_SCHEMA = "quant-v2.index-catalog-snapshot.v1"
_CONSTITUENT_SCHEMA = "quant-v2.index-constituent-observed-snapshot.v1"
_WEIGHT_SCHEMA = "quant-v2.index-weight-close-observed-snapshot.v1"


@dataclass(frozen=True, slots=True)
class IndexShadowSyncResult:
    """向 CLI 或受控任务返回研究态观察结果，不提供任何正式发布版本。"""

    capability: str
    observation: StoredIndexShadowObservation


class IndexShadowSyncService:
    """协调单一指数能力抓取、双载荷归档和仓储写入，默认不创建 PIT 或 publication。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: IndexShadowRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收中立 adapter、研究态观察仓储与服务自有私有对象存储。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync_catalog(self, *, administrator: IndexAdministrator) -> IndexShadowSyncResult:
        """抓取一个管理人目录，先归档 raw 与标准 JSON，再登记不关闭历史身份的观察。"""
        batch = await self._fetch(
            capability=IndexCapability.CATALOG_SNAPSHOT,
            parameters=(("administrator", administrator.value),),
        )
        entries = decode_catalog_payload(batch.payload, administrator=administrator)
        observation = self._repository.record_catalog(
            administrator=administrator.value,
            entries=entries,
            source=_archive_batch(batch=batch, payload_store=self._raw_payload_store),
        )
        return IndexShadowSyncResult(capability=batch.capability, observation=observation)

    async def sync_snapshot(
        self,
        *,
        identifier: IndexIdentifier,
        capability: IndexCapability,
    ) -> IndexShadowSyncResult:
        """抓取一份当前成分或权重快照，拒绝将观察载荷转换为正式有效成分。"""
        if capability not in {
            IndexCapability.CONSTITUENT_SNAPSHOT,
            IndexCapability.WEIGHT_SNAPSHOT,
        }:
            raise ValueError("index shadow snapshot capability is invalid")
        batch = await self._fetch(
            capability=capability,
            parameters=(
                ("administrator", identifier.administrator.value),
                ("indexCode", identifier.code),
            ),
        )
        source_date, items, observation_kind = decode_snapshot_payload(
            batch.payload,
            identifier=identifier,
            capability=capability,
        )
        observation = self._repository.record_snapshot(
            identifier=identifier,
            observation_kind=observation_kind,
            source_as_of_date=source_date,
            items=items,
            source=_archive_batch(batch=batch, payload_store=self._raw_payload_store),
        )
        return IndexShadowSyncResult(capability=batch.capability, observation=observation)

    async def _fetch(
        self,
        *,
        capability: IndexCapability,
        parameters: tuple[tuple[str, str], ...],
    ) -> ProviderBatch:
        """确认 adapter 已声明能力后发起一次请求，不在管理人之间做静默来源回退。"""
        if capability.value not in self._source.capabilities():
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "provider does not support index shadow capability",
                retryable=False,
            )
        return await self._source.fetch(
            SourceRequest(capability=capability.value, parameters=parameters)
        )


def decode_catalog_payload(
    payload: bytes, *, administrator: IndexAdministrator
) -> tuple[IndexCatalogObservationEntry, ...]:
    """验证目录 schema 与管理人边界，并把可选日期、数值保留为明确空值。"""
    decoded = _decoded_object(payload, schema=_CATALOG_SCHEMA)
    if decoded.get("administrator") != administrator.value:
        raise _schema_error("catalog administrator does not match request")
    records = _required_list(decoded, "records")
    entries: list[IndexCatalogObservationEntry] = []
    try:
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("catalog record is invalid")
            entries.append(
                IndexCatalogObservationEntry(
                    identifier=IndexIdentifier(administrator, _six_digit(record.get("indexCode"))),
                    name=_text(record.get("indexName")),
                    full_name=_optional_text(record.get("fullName")),
                    base_date=_optional_date(record.get("baseDate")),
                    base_value=_optional_decimal(record.get("baseValue")),
                    published_date=_optional_date(record.get("publishedDate")),
                    constituent_count=_optional_count(record.get("constituentCount")),
                )
            )
    except (TypeError, ValueError, InvalidOperation) as error:
        raise _schema_error("catalog record is invalid") from error
    if not entries or len({entry.identifier.code for entry in entries}) != len(entries):
        raise _schema_error("catalog must contain unique non-empty index codes")
    return tuple(entries)


def decode_snapshot_payload(
    payload: bytes,
    *,
    identifier: IndexIdentifier,
    capability: IndexCapability,
) -> tuple[date | None, tuple[IndexObservedSnapshotItem, ...], str]:
    """解析当前成分或权重观察；来源未给日期、交易所或单位证据时不补造信息。"""
    schema = (
        _CONSTITUENT_SCHEMA
        if capability is IndexCapability.CONSTITUENT_SNAPSHOT
        else _WEIGHT_SCHEMA
    )
    decoded = _decoded_object(payload, schema=schema)
    if (
        decoded.get("administrator") != identifier.administrator.value
        or decoded.get("indexCode") != identifier.code
    ):
        raise _schema_error("snapshot identity does not match request")
    try:
        if capability is IndexCapability.CONSTITUENT_SNAPSHOT:
            source_date = _optional_date(decoded.get("sourceAsOfDate"))
            raw_items = _required_list(decoded, "constituents")
            items = tuple(_constituent_item(item) for item in raw_items)
            kind = "constituent_current"
        else:
            source_date = _required_date(decoded.get("weightDate"))
            raw_items = _required_list(decoded, "weights")
            weight_kind = _text(decoded.get("weightType")).lower()
            items = tuple(_weight_item(item, weight_kind=weight_kind) for item in raw_items)
            kind = "weight_snapshot"
    except (TypeError, ValueError, InvalidOperation) as error:
        raise _schema_error("snapshot record is invalid") from error
    if not items or len({item.source_symbol for item in items}) != len(items):
        raise _schema_error("snapshot must contain unique non-empty source symbols")
    return source_date, items, kind


def _archive_batch(
    *, batch: ProviderBatch, payload_store: RawPayloadStore
) -> IndexShadowSourceObservation:
    """先分别归档上游 raw 与 adapter 标准 JSON，使后续 schema 失败仍可离线重放。"""
    raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
    raw_digest = hashlib.sha256(raw_payload).hexdigest()
    normalized_digest = hashlib.sha256(batch.payload).hexdigest()
    prefix = f"index-shadow/{batch.capability}/{batch.provider_id}/{batch.observed_at:%Y/%m/%d}"
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
    return IndexShadowSourceObservation(
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


def _constituent_item(value: object) -> IndexObservedSnapshotItem:
    """转换当前成分记录，交易所和行业只在来源明确提供时保存。"""
    if not isinstance(value, dict):
        raise ValueError("constituent is invalid")
    return IndexObservedSnapshotItem(
        source_symbol=_six_digit(value.get("sourceSymbol")),
        source_name=_text(value.get("sourceName")),
        source_exchange=_optional_text(value.get("sourceExchange")),
        source_industry=_optional_text(value.get("sourceIndustry")),
        weight_value=None,
        weight_kind=None,
    )


def _weight_item(value: object, *, weight_kind: str) -> IndexObservedSnapshotItem:
    """把 adapter 已确认的百分比权重转为零到一比例，保留来源权重口径。"""
    item = _constituent_item(value)
    if not isinstance(value, dict):
        raise ValueError("weight is invalid")
    weight = Decimal(_text(value.get("weightValue"))) / Decimal("100")
    if not Decimal("0") <= weight <= Decimal("1"):
        raise ValueError("weight is outside percentage range")
    return IndexObservedSnapshotItem(
        source_symbol=item.source_symbol,
        source_name=item.source_name,
        source_exchange=item.source_exchange,
        source_industry=item.source_industry,
        weight_value=weight,
        weight_kind=weight_kind,
    )


def _decoded_object(payload: bytes, *, schema: str) -> dict[str, object]:
    """读取单个版本化 JSON 对象，拒绝未知 schema 或非对象根节点。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _schema_error("index payload is not JSON") from error
    if not isinstance(decoded, dict) or decoded.get("schema") != schema:
        raise _schema_error("index payload schema is invalid")
    return decoded


def _required_list(value: dict[str, object], key: str) -> list[object]:
    """读取非空来源数组，空目录或空快照必须被上游质量门拒绝。"""
    records = value.get(key)
    if not isinstance(records, list) or not records:
        raise ValueError(f"{key} must be a non-empty list")
    return records


def _text(value: object) -> str:
    """读取非空修剪文本，拒绝 null、空白和 pandas 空值文本。"""
    normalized = _optional_text(value)
    if normalized is None:
        raise ValueError("text is required")
    return normalized


def _optional_text(value: object) -> str | None:
    """统一 null、空白和 pandas 空值，避免把缺失字段写为业务字符串。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return None if not normalized or normalized.lower() in {"nan", "none", "nat"} else normalized


def _six_digit(value: object) -> str:
    """保留来源代码前导零并拒绝无法唯一作为观察键的非六位数字。"""
    normalized = _text(value)
    if normalized.isdigit() and len(normalized) <= 6:
        normalized = normalized.zfill(6)
    if len(normalized) != 6 or not normalized.isdigit():
        raise ValueError("source code must contain six digits")
    return normalized


def _optional_date(value: object) -> date | None:
    """把可空 ISO 日期转换为业务日期，日期未知时不伪造观察日期。"""
    normalized = _optional_text(value)
    return None if normalized is None else date.fromisoformat(normalized[:10])


def _required_date(value: object) -> date:
    """读取权重快照必须提供的来源日期。"""
    resolved = _optional_date(value)
    if resolved is None:
        raise ValueError("source date is required")
    return resolved


def _optional_decimal(value: object) -> Decimal | None:
    """读取可空精确十进制文本，保留来源未提供数值的语义。"""
    normalized = _optional_text(value)
    return None if normalized is None else Decimal(normalized)


def _optional_count(value: object) -> int | None:
    """读取可空非负整数，拒绝布尔值和浮点截断。"""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("count must be an integer")
    if not isinstance(value, int | str):
        raise ValueError("count must be an integer")
    parsed = int(value)
    if parsed < 0 or str(parsed) != str(value).strip():
        raise ValueError("count must be a non-negative integer")
    return parsed


def _schema_error(message: str) -> ProviderError:
    """构造不可重试 schema 漂移错误，阻止错误观察进入研究态仓储。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)
