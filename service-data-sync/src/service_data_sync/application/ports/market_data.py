"""个股行情、参考数据与仅失败留证的应用端口。

端口描述证券身份、原生日线/周期线、因子、公司行动和资料的版本化读写，同时隔离具体数据源与对象存储实现。
成功发布不要求保存供应商大字段；失败排障字节由独立存储端口按私有证据策略处理。
"""

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
    """行情仓储返回的稳定标准证券身份。

    `security_id` 是跨代码复用的内部永久身份，`instrument_id` 是当前交易所代码版本。
    调用方不应把名称或六位代码单独当作主键。
    """

    security_id: int
    instrument_id: UUID
    identifier: EquityIdentifier
    name: str | None
    listing_status: str


@dataclass(frozen=True, slots=True)
class PublishedDailyBars:
    """描述一次已提交日线发布及其写入结果。

    `data_version` 是读取端应固定使用的不可变版本。
    两个计数分别说明新增事实和与既有内容相同的幂等记录。
    """

    data_version: UUID
    inserted_count: int
    unchanged_count: int
    instrument: StoredEquityInstrument


@dataclass(frozen=True, slots=True)
class EquitySourceObservation:
    """描述来源摘要及成功不留存或失败归档标记的标准观察。

    `source_payload_sha256` 用于验证输入内容，`raw_uri` 指向失败证据或成功路径的不可回放标记。
    `observed_at` 是服务观察来源的带时区时间。
    """

    provider_id: str
    capability: str
    source_payload_sha256: str
    raw_uri: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class PublishedEquityDataset:
    """描述一个证券分区的行情或参考数据发布结果。

    `published_at` 表示消费者何时可见该 `data_version`；写入计数用于区分内容变更和安全的重复同步。
    """

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
class EquityAvailabilityObservation:
    """表示不会产生 `canonical` 事实的同步结果，供读取方安全显示为空。

    这表示精确窗口的合法空集或来源不可用，不等于没有执行过同步，也不能作为行情数值或生命周期变化写入数据库。
    """

    availability: str
    reason_code: str
    observed_at: datetime


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
    """表示仅在同步失败时才会持久化的来源或标准化字节。

    对象键、摘要和内容类型共同让排障过程能验证证据；成功同步不应借此保存可回放的大型供应商响应。
    """

    object_key: str
    content_sha256: str
    content_type: str
    payload: bytes


class RawPayloadStore(Protocol):
    """独立于 `adapter` 和标准表管理失败排障所需来源字节的 `Protocol`。

    此端口与业务事实写入分离，保证失败证据可受控保存，同时不把对象存储细节泄漏给同步编排代码。
    """

    def put(self, payload: RawPayload) -> str:
        """暂存失败证据字节，并返回可记录在来源观察中的受控引用。"""
        ...

    def get(self, uri: str) -> bytes:
        """只读取失败时归档的服务私有证据；成功路径标记必须明确拒绝。"""
        ...


class EquityDailyBarRepository(Protocol):
    """负责标准日线写入和可用性观察的最小应用 `Protocol`。

    实现负责版本化、幂等和事实/非事实结果的互斥；应用服务只提交已解码、已校验的领域对象与来源摘要。
    """

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
        """记录来源摘要和留证标记后，对标准日线进行版本化发布。"""
        ...

    def record_daily_bar_availability(
        self,
        *,
        identifier: EquityIdentifier,
        start: date,
        end: date,
        availability: str,
        reason_code: str,
        provider_id: str | None,
        observed_at: datetime,
    ) -> EquityAvailabilityObservation:
        """记录空集或来源不可用，禁止写入缺少业务事实的日线行。"""
        ...

    def clear_daily_bar_availability(
        self,
        *,
        identifier: EquityIdentifier,
        start: date,
        end: date,
        cleared_at: datetime,
    ) -> None:
        """在同一窗口成功发布真实日线后，终结旧的非事实可用性观测。"""
        ...


class EquityMarketDataRepository(EquityDailyBarRepository, Protocol):
    """负责标准行情、因子、公司事件与概况的版本化写入和发布读取。

    所有读取都应受当前 `publication` 约束，所有写入都以永久证券身份分区。
    这阻止代码复用或未发布修订污染消费者视图。
    """

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

    def get_daily_bar_availability(
        self,
        *,
        identifier: EquityIdentifier,
        start: date,
        end: date,
    ) -> EquityAvailabilityObservation | None:
        """返回精确请求窗口最近的空集或来源不可用观测。"""
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
