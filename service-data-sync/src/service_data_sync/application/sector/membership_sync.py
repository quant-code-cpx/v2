"""板块成分当前集合的来源证据、身份质量门和观测 `release` 编排。

完整快照才允许关闭或延续观测区间；`PENDING`、隔离、空响应和不完整集合都不能被解释为真实调入或调出。
发布版本冻结的是“某观察日看到的集合”，不是成分实际生效日期。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.application.ports.sector_market_data import StoredSector
from service_data_sync.application.ports.sector_membership import (
    PublishedSectorMembershipRelease,
    PublishedSectorMembershipSnapshot,
    SectorMembershipRepository,
    SectorMembershipRun,
)
from service_data_sync.domain.sector import (
    SectorIdentifier,
    SectorMembershipCandidate,
    SectorScheme,
)

_CAPABILITY = "sector.membership.snapshot.raw"
_SCHEMA = "quant-v2.sector-membership-snapshot.v1"


@dataclass(frozen=True, slots=True)
class SectorMembershipSyncItem:
    """描述一个板块分区本次是否保存为完整可差分快照。"""

    identifier: SectorIdentifier
    snapshot_id: UUID
    complete: bool
    pending_count: int
    quarantine_count: int


@dataclass(frozen=True, slots=True)
class SectorMembershipSyncFailure:
    """描述一个来源分区失败，供调用方识别 partial run 而不伪装为完整成功。"""

    identifier: SectorIdentifier
    code: ProviderErrorCode


@dataclass(frozen=True, slots=True)
class SectorMembershipSyncResult:
    """描述一个 scheme run 的已提交快照、失败分区和最终 release。"""

    scheme: SectorScheme
    items: tuple[SectorMembershipSyncItem, ...]
    failures: tuple[SectorMembershipSyncFailure, ...]
    release: PublishedSectorMembershipRelease | None


class SectorMembershipSyncService:
    """同步一个分类体系的所有 ACTIVE 板块；成员事实只表示完整快照中的观察结果。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: SectorMembershipRepository,
        raw_payload_store: RawPayloadStore,
        retry_delay_seconds: float = 60.0,
    ) -> None:
        """接收中立来源、canonical 端口和原始证据存储，不依赖供应商实现。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store
        self._retry_delay_seconds = retry_delay_seconds

    async def sync_scheme(
        self,
        *,
        scheme: SectorScheme,
        observation_date: date,
        sector_codes: Sequence[str] | None = None,
        before_final_publication: Callable[[], None] | None = None,
    ) -> SectorMembershipSyncResult:
        """顺序处理冻结分区，并在阈值允许时以同事务回调发布或沿用固定 release。"""
        if _CAPABILITY not in self._source.capabilities():
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "unsupported capability",
                retryable=False,
            )
        active_sectors = tuple(self._repository.list_active_sectors(scheme=scheme))
        if not active_sectors:
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "sector catalog has no active sectors",
                retryable=False,
            )
        sectors = _selected_sectors(active_sectors, sector_codes=sector_codes)
        run = self._repository.start_run(
            scheme=scheme,
            observation_date=observation_date,
            sectors=sectors,
        )
        items: list[SectorMembershipSyncItem] = []
        failures: list[SectorMembershipSyncFailure] = []
        release: PublishedSectorMembershipRelease | None = None
        try:
            for sector in sectors:
                try:
                    publication = await self._sync_sector(
                        run=run,
                        sector=sector,
                        observation_date=observation_date,
                    )
                except ProviderError as error:
                    # 单板块来源失败不关闭区间；reducer 只能按明确阈值沿用旧完整快照。
                    self._repository.mark_partition_failed(
                        run=run,
                        sector=sector,
                        error_code=error.code.value,
                    )
                    failures.append(SectorMembershipSyncFailure(sector.identifier, error.code))
                    continue
                self._repository.mark_partition_completed(
                    run=run,
                    sector=sector,
                    publication=publication,
                )
                items.append(
                    SectorMembershipSyncItem(
                        identifier=sector.identifier,
                        snapshot_id=publication.snapshot_id,
                        complete=publication.complete,
                        pending_count=publication.pending_count,
                        quarantine_count=publication.quarantine_count,
                    )
                )
            release = self._repository.publish_release(
                scheme=scheme,
                observation_date=observation_date,
                before_final_publication=before_final_publication,
            )
        finally:
            status = _run_status(items=items, failures=failures, release=release)
            self._repository.finish_run(run=run, status=status)
        return SectorMembershipSyncResult(
            scheme=scheme,
            items=tuple(items),
            failures=tuple(failures),
            release=release,
        )

    async def _sync_sector(
        self,
        *,
        run: SectorMembershipRun,
        sector: StoredSector,
        observation_date: date,
    ) -> PublishedSectorMembershipSnapshot:
        """归档冻结板块完整响应，解析标准候选后交给仓储执行身份与差分质量门。"""
        identifier = sector.identifier
        batch = await self._fetch_with_retry(
            identifier=identifier, observation_date=observation_date
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
        candidates, schema_fingerprint = decode_sector_membership_batch(
            batch.payload, identifier=identifier
        )
        return self._repository.publish_snapshot(
            sector=sector,
            observation_date=observation_date,
            candidates=candidates,
            provider_id=batch.provider_id,
            source_payload_sha256=raw_digest,
            raw_uri=raw_uri,
            observed_at=batch.observed_at,
            upstream_source=batch.upstream_source,
            adapter_version=batch.adapter_version,
            schema_fingerprint=batch.schema_fingerprint or schema_fingerprint,
            run_id=run.run_id,
            partition_key=_partition_key(identifier, observation_date),
        )

    async def _fetch_with_retry(
        self, *, identifier: SectorIdentifier, observation_date: date
    ) -> ProviderBatch:
        """仅为可重试 UNAVAILABLE 额外重排一次，避免 schema 或身份错误被无意义重复放大。"""
        request = SourceRequest(
            capability=_CAPABILITY,
            parameters=(
                ("sectorScheme", identifier.scheme.value),
                ("sector", identifier.code),
                ("observationDate", observation_date.isoformat()),
            ),
        )
        try:
            return await self._source.fetch(request)
        except ProviderError as error:
            if error.code is not ProviderErrorCode.UNAVAILABLE or not error.retryable:
                raise
        await asyncio.sleep(self._retry_delay_seconds)
        return await self._source.fetch(request)


def _selected_sectors(
    active_sectors: tuple[StoredSector, ...],
    *,
    sector_codes: Sequence[str] | None,
) -> tuple[StoredSector, ...]:
    """从已冻结 ACTIVE 集合精确选择重试分区，并拒绝空集、重复或未知板块代码。"""
    if sector_codes is None:
        return active_sectors
    requested = tuple(sector_codes)
    if not requested or len(set(requested)) != len(requested):
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "sector membership selection is empty or duplicated",
            retryable=False,
        )
    selected = tuple(
        sector for sector in active_sectors if sector.identifier.code in set(requested)
    )
    if len(selected) != len(requested):
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "sector membership selection contains an inactive or unknown sector",
            retryable=False,
        )
    return selected


def decode_sector_membership_batch(
    payload: bytes, *, identifier: SectorIdentifier
) -> tuple[tuple[SectorMembershipCandidate, ...], str]:
    """解析版本化中立 JSON，拒绝跨板块、空集合、重复代码和来源结构漂移。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "sector membership payload is not JSON",
            retryable=False,
        ) from error
    if (
        not isinstance(decoded, dict)
        or decoded.get("schema") != _SCHEMA
        or decoded.get("sectorScheme") != identifier.scheme.value
        or decoded.get("sector") != identifier.code
        or not isinstance(decoded.get("members"), list)
    ):
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "unexpected sector membership schema",
            retryable=False,
        )
    try:
        candidates = tuple(_candidate_from_record(record) for record in decoded["members"])
    except (TypeError, ValueError) as error:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "sector membership record is invalid",
            retryable=False,
        ) from error
    if not candidates or len({candidate.source_symbol for candidate in candidates}) != len(
        candidates
    ):
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "sector membership has empty or duplicate symbols",
            retryable=False,
        )
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "schema": _SCHEMA,
                "keys": sorted(decoded["members"][0])
                if isinstance(decoded["members"][0], dict)
                else [],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return tuple(sorted(candidates, key=lambda candidate: candidate.source_symbol)), fingerprint


def _candidate_from_record(record: object) -> SectorMembershipCandidate:
    """把一条中立成员行转换为受验证领域值，不允许缺失代码或空名称。"""
    if not isinstance(record, dict):
        raise ValueError("membership record must be an object")
    symbol = record.get("sourceSymbol")
    name = record.get("sourceName")
    if not isinstance(symbol, str) or not isinstance(name, str):
        raise ValueError("membership record fields must be strings")
    return SectorMembershipCandidate(source_symbol=symbol, source_name=name)


def _partition_key(identifier: SectorIdentifier, observation_date: date) -> str:
    """生成和持久化运行账本相同的分区键，保证 source batch 可回溯到冻结任务分区。"""
    return f"{identifier.qualified_key}:{observation_date.isoformat()}"


def _run_status(
    *,
    items: list[SectorMembershipSyncItem],
    failures: list[SectorMembershipSyncFailure],
    release: PublishedSectorMembershipRelease | None,
) -> str:
    """按 release、失败和隔离快照生成运行终态，不以部分结果伪装为成功。"""
    if release is not None and not failures and all(item.complete for item in items):
        return "succeeded"
    if items or failures or release is not None:
        return "partial"
    return "failed"
