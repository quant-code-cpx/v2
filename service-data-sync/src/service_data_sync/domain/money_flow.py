"""日频资金流的方法学、序列观察和供应商排行领域对象。

资金流数值只有与来源、算法、范围、窗口、分桶和单位一同保存时才可解释；不同方法学版本不能直接比较或拼接。
本模块区分逐日来源序列和供应商窗口快照，并保留终态、观察时间和完整性证据，避免把推测结果发布为市场事实。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from service_data_sync.domain.equity import Exchange


class MoneyFlowSemanticFamily(StrEnum):
    """区分交易方向与订单规模两类不可互换的供应商派生语义。"""

    TRADE_DIRECTION = "trade_direction_flow"
    ORDER_SIZE = "order_size_flow"


class MoneyFlowScopeType(StrEnum):
    """声明资金流序列的 canonical 观察范围。"""

    EQUITY = "equity"
    SECTOR = "sector"
    MARKET = "market"


class MoneyFlowWindowType(StrEnum):
    """区分源端逐日序列与供应商快照窗口。"""

    DAILY_SOURCE = "daily_source"
    SUPPLIER_DAY = "supplier_day"
    SUPPLIER_ROLLING = "supplier_rolling"


class MoneyFlowFinality(StrEnum):
    """描述来源对观察截点的声明，不把供应商快照伪装成交易所终值。"""

    SOURCE_REPORTED_DAILY = "source_reported_daily"
    POST_CLOSE_OBSERVATION = "post_close_observation"
    UNKNOWN = "unknown"


class MoneyFlowMeasure(StrEnum):
    """列出 canonical 固定列支持的四类资金流度量。"""

    GROSS_INFLOW = "gross_inflow"
    GROSS_OUTFLOW = "gross_outflow"
    NET_AMOUNT = "net_amount"
    NET_RATIO = "net_ratio"


@dataclass(frozen=True, slots=True)
class MoneyFlowBucketDefinition:
    """定义一个方法学版本内不可跨版本复用的资金分桶。"""

    code: str
    label: str
    definition_status: str = "unknown"
    threshold_min: Decimal | None = None
    threshold_max: Decimal | None = None
    threshold_unit: str | None = None

    def __post_init__(self) -> None:
        """拒绝空分桶、反向阈值和伪造的未知阈值。"""
        if not self.code or not self.label:
            raise ValueError("money-flow bucket code and label are required")
        if (
            self.threshold_min is not None
            and self.threshold_max is not None
            and self.threshold_min > self.threshold_max
        ):
            raise ValueError("money-flow bucket threshold range is invalid")
        if self.definition_status == "unknown" and (
            self.threshold_min is not None or self.threshold_max is not None
        ):
            raise ValueError("unknown bucket definition must not invent thresholds")


@dataclass(frozen=True, slots=True)
class MoneyFlowMethodology:
    """冻结一个来源、算法、范围、单位与窗口均明确的方法学版本。"""

    public_key: str
    version: str
    status: str
    production_enabled: bool
    adapter_provider: str
    upstream_source: str
    source_dataset: str
    semantic_family: MoneyFlowSemanticFamily
    scope_types: tuple[MoneyFlowScopeType, ...]
    universe_ids: tuple[str, ...]
    windows: tuple[tuple[MoneyFlowWindowType, int, str], ...]
    buckets: tuple[MoneyFlowBucketDefinition, ...]
    supported_measures: frozenset[MoneyFlowMeasure]
    ratio_denominator: str
    direction_definition: str
    finality: MoneyFlowFinality
    currency: str | None
    raw_amount_unit: str
    standard_amount_unit: str | None
    conversion_version: str | None

    def __post_init__(self) -> None:
        """校验强身份字段，防止窗口、分桶或生产状态形成隐式语义。"""
        required_text = (
            self.public_key,
            self.version,
            self.status,
            self.adapter_provider,
            self.upstream_source,
            self.source_dataset,
            self.ratio_denominator,
            self.direction_definition,
            self.raw_amount_unit,
        )
        if any(not value.strip() for value in required_text):
            raise ValueError("money-flow methodology identity must not be blank")
        if self.status not in {"unknown", "research", "validated", "retired"}:
            raise ValueError("money-flow methodology status is invalid")
        if self.production_enabled and self.status != "validated":
            raise ValueError("only validated money-flow methodology may be production enabled")
        if not self.scope_types or len(set(self.scope_types)) != len(self.scope_types):
            raise ValueError("money-flow methodology scopes must be unique and non-empty")
        if not self.universe_ids or len(set(self.universe_ids)) != len(self.universe_ids):
            raise ValueError("money-flow methodology universes must be unique and non-empty")
        if not self.windows or not self.buckets or not self.supported_measures:
            raise ValueError("money-flow methodology requires windows, buckets and measures")
        if len({bucket.code for bucket in self.buckets}) != len(self.buckets):
            raise ValueError("money-flow methodology bucket codes must be unique")
        for window_type, window_size, source_label in self.windows:
            if window_size < 1 or not source_label:
                raise ValueError("money-flow methodology window is invalid")
            if window_type is MoneyFlowWindowType.DAILY_SOURCE and window_size != 1:
                raise ValueError("daily-source money-flow window size must equal one")
            if window_type is MoneyFlowWindowType.SUPPLIER_DAY and window_size != 1:
                raise ValueError("supplier-day money-flow window size must equal one")
            if window_type is MoneyFlowWindowType.SUPPLIER_ROLLING and window_size <= 1:
                raise ValueError("supplier-rolling money-flow window size must exceed one")


@dataclass(frozen=True, slots=True)
class MoneyFlowScope:
    """携带一条标准 observation 的来源范围，持久化前再解析 canonical 身份。"""

    scope_type: MoneyFlowScopeType
    exchange: Exchange | None = None
    symbol: str | None = None
    sector_scheme: str | None = None
    sector_code: str | None = None
    market_code: str | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        """要求 scope 类型与唯一身份字段严格匹配。"""
        equity_identity = self.exchange is not None or self.symbol is not None
        sector_identity = self.sector_scheme is not None or self.sector_code is not None
        market_identity = self.market_code is not None
        if sum((equity_identity, sector_identity, market_identity)) != 1:
            raise ValueError("money-flow scope must contain exactly one identity")
        if self.scope_type is MoneyFlowScopeType.EQUITY:
            if self.exchange is None or self.symbol is None:
                raise ValueError("equity money-flow scope requires exchange and symbol")
            if len(self.symbol) != 6 or not self.symbol.isdigit():
                raise ValueError("equity money-flow symbol must contain six digits")
        elif self.scope_type is MoneyFlowScopeType.SECTOR:
            if not self.sector_scheme or not self.sector_code:
                raise ValueError("sector money-flow scope requires scheme and code")
        elif not self.market_code:
            raise ValueError("market money-flow scope requires market code")


@dataclass(frozen=True, slots=True)
class MoneyFlowDailyObservation:
    """表达同一来源日序列中一个分桶的 point-in-time 观察。"""

    scope: MoneyFlowScope
    bucket: str
    trade_date: date
    observed_at: datetime
    gross_inflow: Decimal | None
    gross_outflow: Decimal | None
    net_amount: Decimal | None
    net_ratio: Decimal | None

    def __post_init__(self) -> None:
        """校验时区、分桶、gross 符号和至少一个受支持数值。"""
        if not self.bucket:
            raise ValueError("money-flow observation bucket is required")
        if self.observed_at.tzinfo is None:
            raise ValueError("money-flow observation time must include a timezone")
        if self.gross_inflow is not None and self.gross_inflow < 0:
            raise ValueError("money-flow gross inflow must not be negative")
        if self.gross_outflow is not None and self.gross_outflow < 0:
            raise ValueError("money-flow gross outflow must not be negative")
        if all(
            value is None
            for value in (self.gross_inflow, self.gross_outflow, self.net_amount, self.net_ratio)
        ):
            raise ValueError("money-flow observation requires at least one measure")


@dataclass(frozen=True, slots=True)
class MoneyFlowRankingItem:
    """表达供应商排行中的原始位置、scope 和固定四度量。"""

    supplier_position: int
    scope: MoneyFlowScope
    bucket: str
    gross_inflow: Decimal | None
    gross_outflow: Decimal | None
    net_amount: Decimal | None
    net_ratio: Decimal | None

    def __post_init__(self) -> None:
        """拒绝无效位置和没有任何度量的供应商排行项。"""
        if self.supplier_position < 1 or not self.bucket:
            raise ValueError("money-flow ranking position and bucket are required")
        if all(
            value is None
            for value in (self.gross_inflow, self.gross_outflow, self.net_amount, self.net_ratio)
        ):
            raise ValueError("money-flow ranking item requires at least one measure")


@dataclass(frozen=True, slots=True)
class MoneyFlowRankingSnapshot:
    """保存一次不可变供应商排行观测及其可证明完整性。"""

    target_trade_date: date
    observed_at: datetime
    source_cutoff_at: datetime
    scope_type: MoneyFlowScopeType
    universe_id: str
    window_type: MoneyFlowWindowType
    window_size: int
    ranking_bucket: str
    ranking_basis: str
    completeness_basis: str
    is_complete: bool
    items: tuple[MoneyFlowRankingItem, ...]

    def __post_init__(self) -> None:
        """要求位置与分桶组合唯一；SDK 合并结果不得冒充已验证完整分页。"""
        if self.observed_at.tzinfo is None or self.source_cutoff_at.tzinfo is None:
            raise ValueError("money-flow ranking timestamps must include a timezone")
        if not self.universe_id or not self.ranking_bucket or not self.items:
            raise ValueError("money-flow ranking requires universe and items")
        metric_keys = {(item.supplier_position, item.bucket) for item in self.items}
        if len(metric_keys) != len(self.items):
            raise ValueError("money-flow ranking position and bucket keys must be unique")
        scopes_by_position: dict[int, MoneyFlowScope] = {}
        for item in self.items:
            existing_scope = scopes_by_position.setdefault(item.supplier_position, item.scope)
            if existing_scope != item.scope:
                raise ValueError("money-flow ranking position must identify one scope")
        if self.is_complete and self.completeness_basis != "upstream_total_verified":
            raise ValueError("money-flow ranking publication requires verified upstream total")
