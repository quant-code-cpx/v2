"""板块目录的原始归档、标准解码和版本化发布用例。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.application.ports.sector_market_data import SectorMarketDataRepository
from service_data_sync.domain.sector import SectorCatalogEntry, SectorIdentifier, SectorScheme

_SECTOR_CATALOG_SCHEMA = "quant-v2.sector-catalog.v1"


@dataclass(frozen=True, slots=True)
class SectorCatalogSyncResult:
    """向任务和 CLI 返回不含供应商字段的目录发布摘要。"""

    scheme: SectorScheme
    data_version: UUID
    inserted_count: int
    unchanged_count: int


class SectorCatalogSyncService:
    """同步一个分类体系的完整目录；成员关系不在此用例范围内。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: SectorMarketDataRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """从组合根接收中立数据源、仓储和原始证据端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(self, *, scheme: SectorScheme) -> SectorCatalogSyncResult:
        """归档上游完整目录，再原子激活可公开读取的板块身份。"""
        capability = scheme.catalog_capability
        if capability not in self._source.capabilities():
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST, "unsupported capability", retryable=False
            )
        batch = await self._source.fetch(
            SourceRequest(capability=capability, parameters=(("sectorScheme", scheme.value),))
        )
        entries = decode_sector_catalog_batch(batch.payload, scheme=scheme)
        raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
        raw_content_type = batch.raw_content_type or batch.content_type
        raw_digest = hashlib.sha256(raw_payload).hexdigest()
        # 目录名称会决定公开可见性，canonical 变更必须能回链至原始快照。
        raw_uri = self._raw_payload_store.put(
            RawPayload(
                object_key=(
                    f"raw/{capability}/{batch.provider_id}/{batch.observed_at:%Y/%m/%d}/"
                    f"{raw_digest}.json"
                ),
                content_sha256=raw_digest,
                content_type=raw_content_type,
                payload=raw_payload,
            )
        )
        publication = self._repository.publish_catalog(
            scheme=scheme,
            entries=entries,
            provider_id=batch.provider_id,
            source_payload_sha256=raw_digest,
            raw_uri=raw_uri,
            observed_at=batch.observed_at,
        )
        return SectorCatalogSyncResult(
            scheme=scheme,
            data_version=publication.data_version,
            inserted_count=publication.inserted_count,
            unchanged_count=publication.unchanged_count,
        )


def decode_sector_catalog_batch(
    payload: bytes, *, scheme: SectorScheme
) -> tuple[SectorCatalogEntry, ...]:
    """解析 adapter 标准 JSON，并拒绝分类体系、重复代码或名称结构漂移。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "sector-catalog payload is not JSON", retryable=False
        ) from error
    if (
        not isinstance(decoded, dict)
        or decoded.get("schema") != _SECTOR_CATALOG_SCHEMA
        or decoded.get("sectorScheme") != scheme.value
        or not isinstance(decoded.get("sectors"), list)
    ):
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "unexpected sector-catalog schema", retryable=False
        )
    entries = tuple(_decode_entry(record, scheme=scheme) for record in decoded["sectors"])
    if not entries or len({entry.identifier.code for entry in entries}) != len(entries):
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "sector-catalog payload has empty or duplicate codes",
            retryable=False,
        )
    # 供应商顺序不属于契约；排序令发布、游标和测试均保持稳定。
    return tuple(sorted(entries, key=lambda entry: entry.identifier.code))


def _decode_entry(record: object, *, scheme: SectorScheme) -> SectorCatalogEntry:
    """将一条中立目录 JSON 行映射为稳定身份和受控显示名称。"""
    if not isinstance(record, dict):
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "sector-catalog record is not an object", retryable=False
        )
    code = record.get("code")
    name = record.get("name")
    if not isinstance(code, str) or not isinstance(name, str):
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "sector-catalog record has invalid fields", retryable=False
        )
    try:
        return SectorCatalogEntry(SectorIdentifier(scheme=scheme, code=code), name=name)
    except ValueError as error:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "sector-catalog record has invalid values", retryable=False
        ) from error
