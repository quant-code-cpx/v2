"""个股行情的持久化与原始证据端口，均不依赖具体数据源。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from service_data_sync.domain.equity import EquityDailyBar, EquityIdentifier


@dataclass(frozen=True, slots=True)
class StoredEquityInstrument:
    """行情仓储返回的稳定标准证券身份。"""

    security_id: int
    instrument_id: UUID
    identifier: EquityIdentifier
    name: str | None
    listing_status: str


@dataclass(frozen=True, slots=True)
class PublishedDailyBars:
    """描述一次已提交日线发布及其写入结果。"""

    data_version: UUID
    inserted_count: int
    unchanged_count: int
    instrument: StoredEquityInstrument


@dataclass(frozen=True, slots=True)
class RawPayload:
    """记录标准化数据持久化前必须保存的不可变原始证据。"""

    object_key: str
    content_sha256: str
    content_type: str
    payload: bytes


class RawPayloadStore(Protocol):
    """独立于适配器和标准表保存数据源证据。"""

    def put(self, payload: RawPayload) -> str:
        """持久化一个不可变对象并返回其标准存储 URI。"""
        ...

    def get(self, uri: str) -> bytes:
        """读取服务自有 raw evidence，供受控 replay 恢复标准载荷。"""
        ...


class EquityMarketDataRepository(Protocol):
    """负责标准日线写入及已发布数据读取。"""

    def publish_daily_bars(
        self,
        *,
        identifier: EquityIdentifier,
        bars: Sequence[EquityDailyBar],
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        observed_at: datetime,
    ) -> PublishedDailyBars:
        """原始证据可靠保存后，对标准日线进行版本化并发布。"""
        ...

    def get_instrument(self, instrument_id: UUID) -> StoredEquityInstrument | None:
        """返回一个可供内部查询的标准证券。"""
        ...

    def list_instruments(
        self, *, query: str | None, limit: int
    ) -> Sequence[StoredEquityInstrument]:
        """返回排序稳定的已发布证券，用于有上限的前缀查询。"""
        ...

    def list_daily_bars(
        self,
        *,
        instrument_id: UUID,
        start: date,
        end: date,
    ) -> Sequence[tuple[EquityDailyBar, int, bool]]:
        """读取日期窗口内的当前日线及其修订号、终态标记。"""
        ...
