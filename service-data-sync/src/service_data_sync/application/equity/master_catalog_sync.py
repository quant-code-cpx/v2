"""交易所完整证券目录的证据归档与主数据发布编排。

目录发布以一所交易所、一个目标日的完整集合为单位，并由仓储执行完整性基线和双时间身份规则。
目录中没有某代码只能表示本次未观察到，不能在这里推断退市或改写既有生命周期。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.equity_master import (
    EquityMasterRepository,
    PublishedEquityCatalog,
)
from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.domain.equity import EquityIdentifier, Exchange
from service_data_sync.domain.equity_master import EquityCatalogEntry

_CAPABILITY = "equity.master.catalog"
_SCHEMA = "quant-v2.equity-master-catalog.v1"


@dataclass(frozen=True, slots=True)
class EquityCatalogSyncResult:
    """向任务和 CLI 返回一个交易所目录发布的稳定摘要。"""

    exchange: Exchange
    snapshot_id: UUID
    data_version: UUID
    inserted_count: int
    unchanged_count: int


class EquityCatalogSyncService:
    """同步一所交易所完整目录；目录缺席不会被解释成退市。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: EquityMasterRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """从组合根接收中立数据源、主数据仓储与原始证据存储。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(self, *, exchange: Exchange, target_date: date) -> EquityCatalogSyncResult:
        """下载、校验并发布一所交易所目标日完整目录。"""
        if _CAPABILITY not in self._source.capabilities():
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST, "unsupported capability", retryable=False
            )
        batch = await self._source.fetch(
            SourceRequest(
                capability=_CAPABILITY,
                parameters=(("exchange", exchange.value), ("targetDate", target_date.isoformat())),
            )
        )
        entries, schema_fingerprint = decode_equity_catalog_batch(batch.payload, exchange=exchange)
        raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
        raw_content_type = batch.raw_content_type or batch.content_type
        raw_digest = hashlib.sha256(raw_payload).hexdigest()
        raw_uri = self._raw_payload_store.put(
            RawPayload(
                object_key=(
                    f"raw/{_CAPABILITY}/{batch.provider_id}/{batch.observed_at:%Y/%m/%d}/"
                    f"{raw_digest}.json"
                ),
                content_sha256=raw_digest,
                content_type=raw_content_type,
                payload=raw_payload,
            )
        )
        publication = self._repository.publish_catalog(
            exchange=exchange,
            target_date=target_date,
            entries=entries,
            provider_id=batch.provider_id,
            source_payload_sha256=raw_digest,
            raw_uri=raw_uri,
            observed_at=batch.observed_at,
            upstream_source=batch.upstream_source,
            adapter_version=batch.adapter_version,
            schema_fingerprint=batch.schema_fingerprint or schema_fingerprint,
        )
        return _result(exchange, publication)


def decode_equity_catalog_batch(
    payload: bytes, *, exchange: Exchange
) -> tuple[tuple[EquityCatalogEntry, ...], str]:
    """解析 adapter 标准目录 JSON，并拒绝身份漂移、重复代码或空目录。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "equity catalog payload is not JSON", retryable=False
        ) from error
    if not isinstance(decoded, dict) or decoded.get("schema") != _SCHEMA:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "unexpected equity catalog schema", retryable=False
        )
    if decoded.get("exchange") != exchange.value:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "equity catalog exchange mismatch", retryable=False
        )
    rows = decoded.get("entries")
    if not isinstance(rows, list) or not rows:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "equity catalog has no entries", retryable=False
        )
    try:
        entries = tuple(_decode_entry(row, exchange=exchange) for row in rows)
    except (TypeError, ValueError) as error:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "invalid equity catalog entry", retryable=False
        ) from error
    symbols = {entry.identifier.symbol for entry in entries}
    if len(symbols) != len(entries):
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "equity catalog has duplicate symbols", retryable=False
        )
    # 供应商顺序不构成契约；排序使快照哈希、发布与回放结果稳定。
    ordered = tuple(sorted(entries, key=lambda entry: entry.identifier.symbol))
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "schema": _SCHEMA,
                "keys": sorted(str(key) for key in rows[0]) if isinstance(rows[0], dict) else [],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return ordered, fingerprint


def _decode_entry(row: object, *, exchange: Exchange) -> EquityCatalogEntry:
    """将一行中立目录值映射成已确认的标准证券条目。"""
    if not isinstance(row, dict):
        raise ValueError("catalog entry is not an object")
    identifier = EquityIdentifier.parse(f"{exchange.value}.{_required_string(row, 'symbol')}")
    listed_on = row.get("listedOn")
    return EquityCatalogEntry(
        identifier=identifier,
        name=_required_string(row, "name").strip(),
        listed_on=None if listed_on is None else date.fromisoformat(str(listed_on)),
    )


def _required_string(row: dict[str, object], key: str) -> str:
    """读取非空目录字符串字段，阻止 adapter 将缺失值静默标准化。"""
    value = row.get(key)
    if value is None or not str(value).strip():
        raise ValueError(f"{key} is required")
    return str(value)


def _result(exchange: Exchange, publication: PublishedEquityCatalog) -> EquityCatalogSyncResult:
    """将持久化发布投影为不含供应商专有字段的应用结果。"""
    return EquityCatalogSyncResult(
        exchange=exchange,
        snapshot_id=publication.snapshot_id,
        data_version=publication.data_version,
        inserted_count=publication.inserted_count,
        unchanged_count=publication.unchanged_count,
    )
