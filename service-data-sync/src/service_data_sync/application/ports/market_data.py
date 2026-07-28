"""个股行情的持久化与原始证据端口，均不依赖具体数据源。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from service_data_sync.domain.equity import (
    EquityAdjustmentFactor,
    EquityBarPeriod,
    EquityCompanyProfile,
    EquityCorporateAction,
    EquityDailyBar,
    EquityIdentifier,
    EquityPeriodBar,
)


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
class EquitySourceObservation:
    """描述 raw evidence 已可靠归档后的标准来源观察。"""

    provider_id: str
    capability: str
    source_payload_sha256: str
    raw_uri: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class PublishedEquityDataset:
    """描述一个证券分区的行情或参考数据发布结果。"""

    data_version: UUID
    published_at: datetime
    inserted_count: int
    unchanged_count: int
    instrument: StoredEquityInstrument


@dataclass(frozen=True, slots=True)
class EquityDatasetPublication:
    """保存 API 读取所需的当前发布版本与发布时间。"""

    data_version: UUID
    published_at: datetime


@dataclass(frozen=True, slots=True)
class StoredEquityBar:
    """把当前行情值与 revision、终态标记组合为读取记录。"""

    bar: EquityDailyBar | EquityPeriodBar
    revision: int
    is_final: bool


@dataclass(frozen=True, slots=True)
class StoredAdjustmentFactor:
    """把当前累计因子与其发布版本组合为读取记录。"""

    factor: EquityAdjustmentFactor
    revision: int
    factor_version: UUID


@dataclass(frozen=True, slots=True)
class StoredCorporateAction:
    """把当前公司行动与平台稳定身份、revision 组合为读取记录。"""

    action_id: UUID
    action: EquityCorporateAction
    revision: int


@dataclass(frozen=True, slots=True)
class StoredCompanyProfile:
    """把当前公司概况与 revision 组合为读取记录。"""

    profile: EquityCompanyProfile
    revision: int


class EquityMarketDataReadUnavailable(RuntimeError):
    """表示 canonical 行情读取依赖暂时不可用。"""


class EquityIdentityReadConflictError(ValueError):
    """表示读取窗口无法唯一绑定到一个当前知识下的已确认证券。"""


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


class EquityDailyBarRepository(Protocol):
    """负责标准日线写入的最小应用端口。"""

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


class EquityMarketDataRepository(EquityDailyBarRepository, Protocol):
    """负责标准行情、因子、公司事件与概况的版本化写入和发布读取。"""

    def get_instrument(self, instrument_id: UUID) -> StoredEquityInstrument | None:
        """返回一个可供内部查询的标准证券。"""
        ...

    def get_instrument_by_identifier(
        self,
        identifier: EquityIdentifier,
        *,
        fact_start: date | None,
        fact_end: date | None,
    ) -> StoredEquityInstrument | None:
        """按事实日期窗口和当前知识时点返回唯一已确认证券。"""
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

    def publish_period_bars(
        self,
        *,
        identifier: EquityIdentifier,
        period: EquityBarPeriod,
        bars: Sequence[EquityPeriodBar],
        source: EquitySourceObservation,
        window_end: date,
    ) -> PublishedEquityDataset:
        """发布上游直接返回的周线或月线，禁止从日线聚合。"""
        ...

    def publish_adjustment_factors(
        self,
        *,
        identifier: EquityIdentifier,
        factors: Sequence[EquityAdjustmentFactor],
        source: EquitySourceObservation,
        window_end: date,
    ) -> PublishedEquityDataset:
        """发布完整稀疏累计后复权因子序列。"""
        ...

    def publish_corporate_actions(
        self,
        *,
        identifier: EquityIdentifier,
        actions: Sequence[EquityCorporateAction],
        source: EquitySourceObservation,
        window_end: date,
    ) -> PublishedEquityDataset:
        """发布分红送转事件修订。"""
        ...

    def publish_company_profile(
        self,
        *,
        identifier: EquityIdentifier,
        profile: EquityCompanyProfile,
        source: EquitySourceObservation,
    ) -> PublishedEquityDataset:
        """发布公司概况内容修订。"""
        ...

    def get_current_publication(
        self,
        *,
        dataset: str,
        instrument: StoredEquityInstrument,
    ) -> EquityDatasetPublication | None:
        """按永久证券分区返回当前发布，并受控兼容未发生代码复用的旧分区。"""
        ...

    def list_bars(
        self,
        *,
        security_id: int,
        period: EquityBarPeriod,
        start: date,
        end: date,
    ) -> Sequence[StoredEquityBar]:
        """读取日、周或月独立 canonical 表中的当前行情。"""
        ...

    def list_adjustment_factors(
        self,
        *,
        security_id: int,
        end: date,
    ) -> Sequence[StoredAdjustmentFactor]:
        """读取截止日期前的完整当前累计因子序列。"""
        ...

    def list_corporate_actions(
        self,
        *,
        security_id: int,
        start: date | None,
        end: date | None,
    ) -> Sequence[StoredCorporateAction]:
        """读取按报告期过滤的当前公司行动版本。"""
        ...

    def get_company_profile(self, *, security_id: int) -> StoredCompanyProfile | None:
        """读取一只证券当前发布的公司概况。"""
        ...
