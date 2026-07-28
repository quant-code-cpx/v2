"""显式上市生命周期同步的持久化端口，不依赖具体来源或 SQL。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from service_data_sync.domain.equity import Exchange
from service_data_sync.domain.equity_master import EquityLifecycleEntry


@dataclass(frozen=True, slots=True)
class PublishedEquityLifecycle:
    """描述一所交易所生命周期批次和推进后的目录数据版本。"""

    snapshot_id: UUID
    data_version: UUID
    inserted_count: int
    unchanged_count: int


@dataclass(frozen=True, slots=True)
class EquityLifecycleReplayCheckpoint:
    """描述最后成功生命周期批次的标准证据和来源血缘，供确定性恢复使用。"""

    exchange: Exchange
    target_date: date
    data_version: UUID
    snapshot_id: UUID
    raw_uri: str
    normalized_uri: str
    provider_id: str
    upstream_source: str
    adapter_version: str
    schema_fingerprint: str
    observed_at: datetime


class EquityLifecycleRepository(Protocol):
    """保存显式生命周期证据、双时间状态修订和交易所发布版本。"""

    def publish_lifecycle(
        self,
        *,
        exchange: Exchange,
        target_date: date,
        entries: tuple[EquityLifecycleEntry, ...],
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        observed_at: datetime,
        upstream_source: str | None,
        adapter_version: str,
        schema_fingerprint: str,
        normalized_uri: str | None = None,
    ) -> PublishedEquityLifecycle:
        """原子写入已确认身份的生命周期修订，并仅在事实变化时推进版本。"""
        ...

    def get_replay_checkpoint(
        self, *, exchange: Exchange
    ) -> EquityLifecycleReplayCheckpoint | None:
        """读取一所最后成功检查点；没有发布时返回空而不猜测恢复位置。"""
        ...
