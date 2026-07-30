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
    """描述一次已提交日线窗口及其 publication、覆盖和精确来源结果。

    `data_version` 是读取端应固定使用的不可变版本。
    两个计数分别说明新增事实和与既有内容相同的幂等记录。
    合法空窗返回零记录 publication；来源不可用不会构造本结果。
    """

    data_version: UUID
    inserted_count: int
    unchanged_count: int
    instrument: StoredEquityInstrument
    coverage_version: UUID
    source_batch_id: UUID
    publication_kind: str


@dataclass(frozen=True, slots=True)
class EquitySourceObservation:
    """描述原始与标准化对象、来源版本和 schema 的完整观察。

    两份真实对象各自保留摘要、定位、内容类型和字节数；覆盖 manifest 因而可复验从上游
    响应到 canonical revision 的映射，而不需要伪造标准化来源。`observed_at` 是服务实际
    观察上游的带时区知识时间。
    """

    provider_id: str
    capability: str
    raw_payload_sha256: str
    raw_uri: str
    raw_content_type: str
    raw_byte_size: int
    normalized_payload_sha256: str
    normalized_uri: str
    normalized_content_type: str
    normalized_byte_size: int
    observed_at: datetime
    upstream_source: str
    adapter_version: str
    schema_fingerprint: str

    def __post_init__(self) -> None:
        """拒绝缺失、伪摘要或无时区来源观察，coverage 只能绑定可复验真实对象。"""
        if self.observed_at.tzinfo is None:
            raise ValueError("equity source observed_at must include a timezone")
        if any(
            not value.strip()
            for value in (
                self.provider_id,
                self.capability,
                self.raw_uri,
                self.raw_content_type,
                self.normalized_uri,
                self.normalized_content_type,
                self.upstream_source,
                self.adapter_version,
            )
        ):
            raise ValueError("equity source observation metadata is incomplete")
        for digest in (
            self.raw_payload_sha256,
            self.normalized_payload_sha256,
            self.schema_fingerprint,
        ):
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError("equity source observation digest is invalid")
        if self.raw_byte_size < 0 or self.normalized_byte_size < 0:
            raise ValueError("equity source observation byte size is invalid")

    @property
    def source_payload_sha256(self) -> str:
        """兼容既有来源账本字段名，并明确其值就是真实 raw 对象摘要。"""
        return self.raw_payload_sha256


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
    coverage_version: UUID | None = None
    source_batch_id: UUID | None = None
    publication_kind: str | None = None


@dataclass(frozen=True, slots=True)
class EquityDatasetPublication:
    """保存 API 读取所需的当前发布版本与发布时间。"""

    data_version: UUID
    published_at: datetime


@dataclass(frozen=True, slots=True)
class EquityAvailabilityObservation:
    """保留旧任务或运维探针的非成功诊断观察。

    新的合法空行情窗口必须使用零记录 publication 与 immutable coverage；本结构不构成成功
    证据，不能被回填 finalizer、行情读取或生命周期逻辑消费。
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
    """负责标准日线窗口事实或真实零记录 coverage 的最小应用 `Protocol`。

    实现负责版本化、幂等及 DATA/零记录 publication 互斥；来源失败必须在调用前抛出。
    """

    def publish_daily_bars(
        self,
        *,
        identifier: EquityIdentifier,
        bars: Sequence[EquityDailyBar],
        source: EquitySourceObservation,
        start: date,
        end: date,
    ) -> PublishedDailyBars:
        """原子发布标准日线 DATA 或通过质量门的零记录 coverage，并冻结精确来源身份。"""
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
        """记录旧任务或运维探针诊断，不能替代成功 publication/coverage。"""
        ...

    def clear_daily_bar_availability(
        self,
        *,
        identifier: EquityIdentifier,
        start: date,
        end: date,
        cleared_at: datetime,
    ) -> None:
        """在同一窗口形成 DATA 或零记录 coverage 后，终结旧诊断观测。"""
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
        start: date,
        end: date,
    ) -> PublishedEquityDataset:
        """发布上游原生周/月 DATA 或通过质量门的零记录 coverage，禁止从日线聚合。"""
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
        start: date,
        end: date,
    ) -> PublishedEquityDataset:
        """发布精确闭区间内的分红送转修订与真实零记录 coverage。"""
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
        known_at: datetime | None = None,
    ) -> Sequence[StoredEquityBar]:
        """按可选 publication 知识截止点读取日、周或月独立 canonical 表。"""
        ...

    def list_adjustment_factors(
        self,
        *,
        security_id: int,
        end: date,
        known_at: datetime | None = None,
    ) -> Sequence[StoredAdjustmentFactor]:
        """按可选 publication 知识截止点读取完整累计因子序列。"""
        ...

    def list_corporate_actions(
        self,
        *,
        security_id: int,
        start: date | None,
        end: date | None,
        known_at: datetime | None = None,
    ) -> Sequence[StoredCorporateAction]:
        """按报告期和可选 publication 知识截止点读取公司行动版本。"""
        ...

    def get_company_profile(
        self,
        *,
        security_id: int,
        known_at: datetime | None = None,
    ) -> StoredCompanyProfile | None:
        """按可选 publication 知识截止点读取一只证券公司概况。"""
        ...
