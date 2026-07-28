"""申万三级 taxonomy、估值的归档、解码、发布与 raw replay 编排。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.application.ports.sw_sector import (
    SwPublishResult,
    SwSectorRepository,
    SwSourceObservation,
)
from service_data_sync.domain.sw_sector import (
    SwIndustryLevel,
    SwIndustryNode,
    SwIndustrySnapshot,
    SwIndustryValuation,
    SwMethodology,
)

_CAPABILITY = "sector.sw.snapshot.raw"
_SCHEMA = "quant-v2.sw-industry-snapshot.v1"
_SCHEME = "sw.industry"
_PERCENT_DIVISOR = Decimal("100")


@dataclass(frozen=True, slots=True)
class SwSnapshotSyncResult:
    """向 CLI 与任务返回 taxonomy 和估值的独立发布摘要。"""

    publications: SwPublishResult
    replayed: bool


class SwSnapshotSyncService:
    """协调当日上游抓取或指定日期标准载荷重放。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: SwSectorRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收中立来源、canonical 仓储与私有对象存储端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(self, *, snapshot_date: date) -> SwSnapshotSyncResult:
        """抓取一个当天完整快照，先归档 raw 和标准载荷再原子发布。"""
        if _CAPABILITY not in self._source.capabilities():
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "provider does not support SW snapshot",
                retryable=False,
            )
        batch = await self._source.fetch(
            SourceRequest(
                capability=_CAPABILITY,
                parameters=(("snapshotDate", snapshot_date.isoformat()),),
            )
        )
        snapshot = decode_sw_snapshot(batch.payload, expected_date=snapshot_date)
        source = _archive_batch(batch=batch, payload_store=self._raw_payload_store)
        return SwSnapshotSyncResult(
            publications=self._repository.publish_snapshot(snapshot=snapshot, source=source),
            replayed=False,
        )

    def replay(self, *, snapshot_date: date) -> SwSnapshotSyncResult:
        """从指定日期 checkpoint 的标准载荷恢复，不再次访问上游。"""
        checkpoint = self._repository.get_checkpoint(snapshot_date=snapshot_date)
        if checkpoint is None:
            raise ValueError("SW replay checkpoint is not available")
        raw_payload = self._raw_payload_store.get(checkpoint.raw_uri)
        raw_digest = hashlib.sha256(raw_payload).hexdigest()
        if raw_digest != checkpoint.raw_sha256:
            raise ValueError("SW replay raw payload digest does not match checkpoint")
        normalized_payload = self._raw_payload_store.get(checkpoint.normalized_uri)
        normalized_digest = hashlib.sha256(normalized_payload).hexdigest()
        if normalized_digest != checkpoint.summary_sha256:
            raise ValueError("SW replay payload digest does not match checkpoint")
        snapshot = decode_sw_snapshot(normalized_payload, expected_date=snapshot_date)
        source = SwSourceObservation(
            provider_id=checkpoint.provider_id,
            capability=_CAPABILITY,
            source_payload_sha256=checkpoint.raw_sha256,
            raw_uri=checkpoint.raw_uri,
            normalized_payload_sha256=normalized_digest,
            normalized_uri=checkpoint.normalized_uri,
            observed_at=checkpoint.observed_at,
            upstream_source=checkpoint.upstream_source,
            adapter_version=checkpoint.adapter_version,
            schema_fingerprint=checkpoint.schema_fingerprint,
        )
        return SwSnapshotSyncResult(
            publications=self._repository.publish_snapshot(snapshot=snapshot, source=source),
            replayed=True,
        )


def decode_sw_snapshot(payload: bytes, *, expected_date: date) -> SwIndustrySnapshot:
    """解析 adapter 中立 JSON，解析父级名称并验证完整三级闭包。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _schema_error("SW snapshot payload is not JSON") from error
    if (
        not isinstance(decoded, dict)
        or decoded.get("schema") != _SCHEMA
        or decoded.get("scheme") != _SCHEME
        or decoded.get("snapshotDate") != expected_date.isoformat()
    ):
        raise _schema_error("unexpected SW snapshot schema or identity")
    methodology = _decode_methodology(decoded.get("methodology"))
    levels = decoded.get("levels")
    if not isinstance(levels, list) or len(levels) != 3:
        raise _schema_error("SW snapshot must contain three levels")
    raw_by_level: dict[SwIndustryLevel, list[dict[str, object]]] = {}
    for level_entry in levels:
        if not isinstance(level_entry, dict) or not isinstance(level_entry.get("items"), list):
            raise _schema_error("SW level entry is invalid")
        try:
            level = SwIndustryLevel(int(level_entry["level"]))
        except (KeyError, TypeError, ValueError) as error:
            raise _schema_error("SW level value is invalid") from error
        if level in raw_by_level or not level_entry["items"]:
            raise _schema_error("SW level is duplicate or empty")
        items: list[dict[str, object]] = []
        for item in level_entry["items"]:
            if not isinstance(item, dict):
                raise _schema_error("SW industry entry is not an object")
            items.append(item)
        raw_by_level[level] = items
    if set(raw_by_level) != set(SwIndustryLevel):
        raise _schema_error("SW snapshot levels are incomplete")

    # 上游只给父级名称；同层名称必须唯一，才能无歧义解析为稳定父级代码。
    name_to_code: dict[SwIndustryLevel, dict[str, str]] = {}
    for level, items in raw_by_level.items():
        mapping: dict[str, str] = {}
        for item in items:
            name = _required_text(item, "name")
            code = _required_text(item, "code")
            if name in mapping:
                raise _schema_error("SW parent names are ambiguous within a level")
            mapping[name] = code
        name_to_code[level] = mapping

    nodes: list[SwIndustryNode] = []
    valuations: list[SwIndustryValuation] = []
    try:
        for level in SwIndustryLevel:
            for item in raw_by_level[level]:
                parent_name = _optional_text(item.get("parentName"))
                parent_code = None
                if level is not SwIndustryLevel.LEVEL_1:
                    if parent_name is None:
                        raise ValueError("SW child industry has no parent name")
                    parent_code = name_to_code[SwIndustryLevel(level.value - 1)].get(parent_name)
                    if parent_code is None:
                        raise ValueError("SW child industry parent is not present")
                code = _required_text(item, "code")
                nodes.append(
                    SwIndustryNode(
                        code=code,
                        name=_required_text(item, "name"),
                        level=level,
                        parent_code=parent_code,
                        component_count=_required_int(item, "componentCount"),
                    )
                )
                valuations.append(
                    SwIndustryValuation(
                        code=code,
                        snapshot_date=expected_date,
                        static_pe=_optional_decimal(item.get("staticPe")),
                        ttm_pe=_optional_decimal(item.get("ttmPe")),
                        pb=_optional_decimal(item.get("pb")),
                        dividend_yield_ratio=_divide_percent(
                            _optional_decimal(item.get("dividendYieldPercent"))
                        ),
                    )
                )
        snapshot = SwIndustrySnapshot(
            snapshot_date=expected_date,
            nodes=tuple(sorted(nodes, key=lambda value: (value.level, value.code))),
            valuations=tuple(sorted(valuations, key=lambda value: value.code)),
            methodology=methodology,
        )
        # 在 raw 归档前即计算闭包，任何孤儿或环都会阻断整个快照。
        snapshot.closure()
        return snapshot
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise _schema_error("SW snapshot values or hierarchy are invalid") from error


def _decode_methodology(value: object) -> SwMethodology:
    """解析并验证不可缺失的方法论来源、版本、状态和语义摘要。"""
    if not isinstance(value, dict):
        raise _schema_error("SW methodology is missing")
    try:
        return SwMethodology(
            code=_required_text(value, "code"),
            version=_required_int(value, "version"),
            status=_required_text(value, "status"),
            upstream_source=_required_text(value, "upstreamSource"),
            semantic_spec_sha256=_required_text(value, "semanticSpecSha256"),
        )
    except (TypeError, ValueError) as error:
        raise _schema_error("SW methodology is invalid") from error


def _archive_batch(*, batch: object, payload_store: RawPayloadStore) -> SwSourceObservation:
    """分别归档供应商原始响应和可重放中立载荷，并返回完整来源观察。"""
    from service_data_sync.application.ports.data_source import ProviderBatch

    if not isinstance(batch, ProviderBatch):
        raise TypeError("batch must be ProviderBatch")
    raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
    raw_digest = hashlib.sha256(raw_payload).hexdigest()
    normalized_digest = hashlib.sha256(batch.payload).hexdigest()
    raw_uri = payload_store.put(
        RawPayload(
            object_key=(
                f"raw/{batch.capability}/{batch.provider_id}/{batch.observed_at:%Y/%m/%d}/"
                f"{raw_digest}.json"
            ),
            content_sha256=raw_digest,
            content_type=batch.raw_content_type or batch.content_type,
            payload=raw_payload,
        )
    )
    normalized_uri = payload_store.put(
        RawPayload(
            object_key=(
                f"normalized/{batch.capability}/{batch.provider_id}/"
                f"{batch.observed_at:%Y/%m/%d}/{normalized_digest}.json"
            ),
            content_sha256=normalized_digest,
            content_type=batch.content_type,
            payload=batch.payload,
        )
    )
    return SwSourceObservation(
        provider_id=batch.provider_id,
        capability=batch.capability,
        source_payload_sha256=raw_digest,
        raw_uri=raw_uri,
        normalized_payload_sha256=normalized_digest,
        normalized_uri=normalized_uri,
        observed_at=batch.observed_at,
        upstream_source=batch.upstream_source or batch.provider_id,
        adapter_version=batch.adapter_version,
        schema_fingerprint=batch.schema_fingerprint or hashlib.sha256(_SCHEMA.encode()).hexdigest(),
    )


def _required_text(value: dict[str, object], key: str) -> str:
    """读取中立载荷中的非空文本。"""
    raw = value.get(key)
    if raw is None or not str(raw).strip():
        raise ValueError(f"{key} is required")
    return str(raw).strip()


def _optional_text(value: object) -> str | None:
    """把空白中立文本统一为真实空值。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _required_int(value: dict[str, object], key: str) -> int:
    """读取非布尔整数，拒绝浮点截断。"""
    raw = value.get(key)
    if isinstance(raw, bool):
        raise ValueError(f"{key} must be an integer")
    parsed = int(raw)  # type: ignore[arg-type]
    if str(parsed) != str(raw):
        raise ValueError(f"{key} must be an integer")
    return parsed


def _optional_decimal(value: object) -> Decimal | None:
    """读取可空精确小数并保留缺失语义。"""
    return None if value is None else Decimal(str(value))


def _divide_percent(value: Decimal | None) -> Decimal | None:
    """把来源百分数转为 canonical 一比一比例。"""
    return None if value is None else value / _PERCENT_DIVISOR


def _schema_error(message: str) -> ProviderError:
    """构造不可重试的申万标准载荷漂移错误。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)
