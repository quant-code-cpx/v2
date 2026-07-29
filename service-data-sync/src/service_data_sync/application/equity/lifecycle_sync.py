"""显式上市生命周期证据的校验、发布和恢复编排。

只有交易所明确声明的上市、暂停、恢复、退市或官方更正可以改变生命周期；目录缺席和行情缺失没有这种权限。
重放模式仅验证已存 `checkpoint` 与证据摘要，不访问上游，从而支持可复验恢复。
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
from service_data_sync.application.ports.equity_lifecycle import (
    EquityLifecycleRepository,
    PublishedEquityLifecycle,
)
from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.domain.equity import EquityIdentifier, Exchange
from service_data_sync.domain.equity_master import (
    EquityLifecycleEntry,
    EquityLifecycleEvidenceKind,
    EquityLifecycleStatus,
)

_CAPABILITY = "equity.lifecycle.explicit"
_SCHEMA = "quant-v2.equity-lifecycle-explicit.v1"


@dataclass(frozen=True, slots=True)
class EquityLifecycleSyncResult:
    """向任务和 CLI 返回一所交易所生命周期批次的稳定摘要。"""

    exchange: Exchange
    snapshot_id: UUID
    data_version: UUID
    inserted_count: int
    unchanged_count: int


class EquityLifecycleSyncService:
    """同步一所交易所显式生命周期事实；目录缺席永远不能进入本用例。"""

    def __init__(
        self,
        *,
        source: DataSourcePort | None,
        repository: EquityLifecycleRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """从组合根接收中立来源、生命周期仓储与原始证据存储。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(self, *, exchange: Exchange, target_date: date) -> EquityLifecycleSyncResult:
        """下载、归档、校验并发布一所交易所的显式生命周期批次。"""
        if self._source is None or _CAPABILITY not in self._source.capabilities():
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST, "unsupported capability", retryable=False
            )
        batch = await self._source.fetch(
            SourceRequest(
                capability=_CAPABILITY,
                parameters=(
                    ("exchange", exchange.value),
                    ("targetDate", target_date.isoformat()),
                ),
            )
        )
        entries, schema_fingerprint = decode_equity_lifecycle_batch(
            batch.payload, exchange=exchange
        )
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
        normalized_digest = hashlib.sha256(batch.payload).hexdigest()
        normalized_uri = self._raw_payload_store.put(
            RawPayload(
                object_key=(
                    f"normalized/{_CAPABILITY}/{batch.provider_id}/"
                    f"{batch.observed_at:%Y/%m/%d}/{normalized_digest}.json"
                ),
                content_sha256=normalized_digest,
                content_type=batch.content_type,
                payload=batch.payload,
            )
        )
        publication = self._repository.publish_lifecycle(
            exchange=exchange,
            target_date=target_date,
            entries=entries,
            provider_id=batch.provider_id,
            source_payload_sha256=raw_digest,
            raw_uri=raw_uri,
            normalized_uri=normalized_uri,
            observed_at=batch.observed_at,
            upstream_source=batch.upstream_source,
            adapter_version=batch.adapter_version,
            schema_fingerprint=batch.schema_fingerprint or schema_fingerprint,
        )
        return _result(exchange, publication)

    async def replay_last(self, *, exchange: Exchange) -> EquityLifecycleSyncResult:
        """从最后成功检查点重放标准证据，不再次访问供应商或改变原始观测时间。"""
        checkpoint = self._repository.get_replay_checkpoint(exchange=exchange)
        if checkpoint is None:
            raise ValueError("equity lifecycle replay checkpoint is unavailable")
        normalized_payload = self._raw_payload_store.get(checkpoint.normalized_uri)
        entries, _ = decode_equity_lifecycle_batch(normalized_payload, exchange=exchange)
        raw_payload = self._raw_payload_store.get(checkpoint.raw_uri)
        publication = self._repository.publish_lifecycle(
            exchange=exchange,
            target_date=checkpoint.target_date,
            entries=entries,
            provider_id=checkpoint.provider_id,
            source_payload_sha256=hashlib.sha256(raw_payload).hexdigest(),
            raw_uri=checkpoint.raw_uri,
            normalized_uri=checkpoint.normalized_uri,
            observed_at=checkpoint.observed_at,
            upstream_source=checkpoint.upstream_source,
            adapter_version=checkpoint.adapter_version,
            schema_fingerprint=checkpoint.schema_fingerprint,
        )
        return _result(exchange, publication)


def decode_equity_lifecycle_batch(
    payload: bytes, *, exchange: Exchange
) -> tuple[tuple[EquityLifecycleEntry, ...], str]:
    """解析显式证据标准 JSON，拒绝空批次、跨所行和重复状态事实。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "equity lifecycle payload is not JSON", retryable=False
        ) from error
    if not isinstance(decoded, dict) or decoded.get("schema") != _SCHEMA:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "unexpected equity lifecycle schema", retryable=False
        )
    if decoded.get("exchange") != exchange.value:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "equity lifecycle exchange mismatch", retryable=False
        )
    rows = decoded.get("entries")
    if not isinstance(rows, list) or not rows:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "equity lifecycle has no entries", retryable=False
        )
    try:
        entries = tuple(_decode_entry(row, exchange=exchange) for row in rows)
    except (TypeError, ValueError) as error:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "invalid equity lifecycle entry", retryable=False
        ) from error
    keys = {(entry.identifier.symbol, entry.effective_on, entry.status) for entry in entries}
    if len(keys) != len(entries):
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "equity lifecycle has duplicate facts", retryable=False
        )
    ordered = tuple(
        sorted(
            entries, key=lambda entry: (entry.effective_on, entry.identifier.symbol, entry.status)
        )
    )
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


def _decode_entry(row: object, *, exchange: Exchange) -> EquityLifecycleEntry:
    """将中立生命周期行映射为必须携带显式证据的领域事实。"""
    if not isinstance(row, dict):
        raise ValueError("lifecycle entry is not an object")
    identifier = EquityIdentifier.parse(f"{exchange.value}.{_required_string(row, 'symbol')}")
    return EquityLifecycleEntry(
        identifier=identifier,
        status=EquityLifecycleStatus(_required_string(row, "status")),
        effective_on=date.fromisoformat(_required_string(row, "effectiveOn")),
        evidence_kind=EquityLifecycleEvidenceKind(_required_string(row, "evidenceKind")),
        listed_on=_optional_date(row, "listedOn"),
        delisted_on=_optional_date(row, "delistedOn"),
        correction_approval_reference=_optional_string(row, "correctionApprovalReference"),
    )


def _required_string(row: dict[str, object], key: str) -> str:
    """读取非空标准字段，防止来源适配器静默补全必要语义。"""
    value = row.get(key)
    if value is None or not str(value).strip():
        raise ValueError(f"{key} is required")
    return str(value)


def _optional_date(row: dict[str, object], key: str) -> date | None:
    """解析可空日期字段，保留来源明确声明的未知值。"""
    value = row.get(key)
    return None if value is None else date.fromisoformat(str(value))


def _optional_string(row: dict[str, object], key: str) -> str | None:
    """读取可空字符串字段，空白值不能绕过来源更正证据校验。"""
    value = row.get(key)
    return None if value is None else str(value)


def _result(exchange: Exchange, publication: PublishedEquityLifecycle) -> EquityLifecycleSyncResult:
    """将持久化发布投影为不泄露供应商字段的应用结果。"""
    return EquityLifecycleSyncResult(
        exchange=exchange,
        snapshot_id=publication.snapshot_id,
        data_version=publication.data_version,
        inserted_count=publication.inserted_count,
        unchanged_count=publication.unchanged_count,
    )
