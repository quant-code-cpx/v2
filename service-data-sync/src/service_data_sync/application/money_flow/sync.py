"""日频资金流的来源获取、标准解码、质量门禁和发布用例。

服务把范围、分桶、窗口、金额单位和终态与数值一同校验；这些口径不完整时，数值本身没有可安全发布的业务含义。
逐日序列与供应商排行采用不同完整性条件，研究观察不能越过生产 `fail-closed` 门禁。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.application.ports.money_flow import (
    MoneyFlowRepository,
    MoneyFlowSourceObservation,
    PublishedMoneyFlow,
)
from service_data_sync.domain.equity import Exchange
from service_data_sync.domain.money_flow import (
    MoneyFlowBucketDefinition,
    MoneyFlowDailyObservation,
    MoneyFlowFinality,
    MoneyFlowMeasure,
    MoneyFlowMethodology,
    MoneyFlowRankingItem,
    MoneyFlowRankingSnapshot,
    MoneyFlowScope,
    MoneyFlowScopeType,
    MoneyFlowSemanticFamily,
    MoneyFlowWindowType,
)

_DAILY_SCHEMA = "quant-v2.money-flow-daily.v1"
_RANKING_SCHEMA = "quant-v2.money-flow-ranking.v1"
_ORDER_SIZE_KEY = "eastmoney-order-size"
_TRADE_DIRECTION_KEY = "10jqka-trade-direction"


@dataclass(frozen=True, slots=True)
class MoneyFlowSyncResult:
    """向 CLI 和任务返回同步能力、raw 摘要及 publication 结果。"""

    capability: str
    source_payload_sha256: str
    raw_uri: str
    publication: PublishedMoneyFlow


class MoneyFlowSyncService:
    """协调一个明确资金流 capability，禁止在来源失败时跨方法学 fallback。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: MoneyFlowRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收中立来源、canonical 仓储和不可变 raw 存储。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(
        self,
        *,
        capability: str,
        parameters: tuple[tuple[str, str], ...],
        run_id: UUID | None = None,
        partition_key: str | None = None,
    ) -> MoneyFlowSyncResult:
        """先归档一次来源响应，再按载荷类型发布日序列或 supplier ranking。"""
        if capability not in self._source.capabilities():
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "money-flow capability is not supported by selected source",
                retryable=False,
            )
        batch = await self._source.fetch(
            SourceRequest(capability=capability, parameters=parameters)
        )
        source = self._archive(batch)
        payload = _payload(batch.payload)
        methodology = _methodology(payload)
        if payload["schema"] == _DAILY_SCHEMA:
            publication = self._repository.publish_daily(
                methodology=methodology,
                observations=_daily_observations(payload, batch.observed_at),
                source=source,
                run_id=run_id,
                partition_key=partition_key,
            )
        else:
            publication = self._repository.publish_ranking(
                methodology=methodology,
                snapshot=_ranking_snapshot(payload, batch.observed_at),
                source=source,
                run_id=run_id,
                partition_key=partition_key,
            )
        return MoneyFlowSyncResult(
            capability=capability,
            source_payload_sha256=source.source_payload_sha256,
            raw_uri=source.raw_uri,
            publication=publication,
        )

    def _archive(self, batch: ProviderBatch) -> MoneyFlowSourceObservation:
        """把供应商原始响应先写入对象存储，再构造 canonical 血缘。"""
        raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
        raw_content_type = batch.raw_content_type or batch.content_type
        digest = hashlib.sha256(raw_payload).hexdigest()
        raw_uri = self._raw_payload_store.put(
            RawPayload(
                object_key=(
                    f"raw/{batch.capability}/{batch.provider_id}/"
                    f"{batch.observed_at:%Y/%m/%d}/{digest}.json"
                ),
                content_sha256=digest,
                content_type=raw_content_type,
                payload=raw_payload,
            )
        )
        return MoneyFlowSourceObservation(
            provider_id=batch.provider_id,
            capability=batch.capability,
            source_payload_sha256=digest,
            raw_uri=raw_uri,
            observed_at=batch.observed_at,
            upstream_source=batch.upstream_source or batch.provider_id,
            adapter_version=batch.adapter_version,
            schema_fingerprint=(
                batch.schema_fingerprint or hashlib.sha256(batch.capability.encode()).hexdigest()
            ),
        )


def _payload(payload: bytes) -> dict[str, object]:
    """解析 adapter 标准 JSON，并拒绝未知 schema 或非对象载荷。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _schema_error("money-flow payload is not JSON") from error
    if not isinstance(decoded, dict) or decoded.get("schema") not in {
        _DAILY_SCHEMA,
        _RANKING_SCHEMA,
    }:
        raise _schema_error("unexpected money-flow payload schema")
    return decoded


def _methodology(payload: dict[str, object]) -> MoneyFlowMethodology:
    """按 adapter 冻结的方法学身份返回完整可解释定义。"""
    key = payload.get("methodologyKey")
    version = payload.get("methodologyVersion")
    if key == _ORDER_SIZE_KEY and version == "1":
        buckets = tuple(
            MoneyFlowBucketDefinition(code=code, label=label)
            for code, label in (
                ("main", "主力"),
                ("super_large", "超大单"),
                ("large", "大单"),
                ("medium", "中单"),
                ("small", "小单"),
            )
        )
        return MoneyFlowMethodology(
            public_key=_ORDER_SIZE_KEY,
            version="1",
            status="research",
            production_enabled=False,
            adapter_provider="akshare-eastmoney-money-flow",
            upstream_source="eastmoney.money-flow",
            source_dataset="eastmoney-order-size-money-flow",
            semantic_family=MoneyFlowSemanticFamily.ORDER_SIZE,
            scope_types=(
                MoneyFlowScopeType.EQUITY,
                MoneyFlowScopeType.SECTOR,
                MoneyFlowScopeType.MARKET,
            ),
            universe_ids=("cn-a", "eastmoney-industry"),
            windows=(
                (MoneyFlowWindowType.DAILY_SOURCE, 1, "source daily"),
                (MoneyFlowWindowType.SUPPLIER_DAY, 1, "今日"),
                (MoneyFlowWindowType.SUPPLIER_ROLLING, 3, "3日"),
                (MoneyFlowWindowType.SUPPLIER_ROLLING, 5, "5日"),
                (MoneyFlowWindowType.SUPPLIER_ROLLING, 10, "10日"),
            ),
            buckets=buckets,
            supported_measures=frozenset({MoneyFlowMeasure.NET_AMOUNT, MoneyFlowMeasure.NET_RATIO}),
            ratio_denominator="供应商成交额口径；阈值尚未由连续探针冻结",
            direction_definition="供应商订单规模分类后的净流入；主力层级关系不推断",
            finality=MoneyFlowFinality.UNKNOWN,
            currency=None,
            raw_amount_unit="unknown",
            standard_amount_unit=None,
            conversion_version="percent-to-ratio-v1",
        )
    if key == _TRADE_DIRECTION_KEY and version == "1":
        return MoneyFlowMethodology(
            public_key=_TRADE_DIRECTION_KEY,
            version="1",
            status="research",
            production_enabled=False,
            adapter_provider="akshare-ths-money-flow",
            upstream_source="10jqka.money-flow",
            source_dataset="10jqka-trade-direction-ranking",
            semantic_family=MoneyFlowSemanticFamily.TRADE_DIRECTION,
            scope_types=(MoneyFlowScopeType.EQUITY, MoneyFlowScopeType.SECTOR),
            universe_ids=("provider-page",),
            windows=(
                (MoneyFlowWindowType.SUPPLIER_DAY, 1, "即时"),
                (MoneyFlowWindowType.SUPPLIER_ROLLING, 3, "3日排行"),
                (MoneyFlowWindowType.SUPPLIER_ROLLING, 5, "5日排行"),
                (MoneyFlowWindowType.SUPPLIER_ROLLING, 10, "10日排行"),
                (MoneyFlowWindowType.SUPPLIER_ROLLING, 20, "20日排行"),
            ),
            buckets=(MoneyFlowBucketDefinition(code="all", label="全部成交"),),
            supported_measures=frozenset(
                {
                    MoneyFlowMeasure.GROSS_INFLOW,
                    MoneyFlowMeasure.GROSS_OUTFLOW,
                    MoneyFlowMeasure.NET_AMOUNT,
                }
            ),
            ratio_denominator="not provided",
            direction_definition="同花顺主动交易方向供应商口径；滚动排行仅提供净额",
            finality=MoneyFlowFinality.UNKNOWN,
            currency="CNY",
            raw_amount_unit="vendor suffix",
            standard_amount_unit="CNY",
            conversion_version="chs-wan-yi-to-cny-v1",
        )
    raise _schema_error("unknown money-flow methodology identity")


def _daily_observations(
    payload: dict[str, object], observed_at: datetime
) -> tuple[MoneyFlowDailyObservation, ...]:
    """把标准日序列载荷映射为按交易日解析身份的领域 observation。"""
    scope = _scope(payload.get("scope"))
    records = payload.get("observations")
    if not isinstance(records, list) or not records:
        raise _schema_error("money-flow daily payload has no observations")
    observations = tuple(
        MoneyFlowDailyObservation(
            scope=scope,
            bucket=_required_text(record, "bucket"),
            trade_date=date.fromisoformat(_required_text(record, "tradeDate")),
            observed_at=observed_at,
            gross_inflow=_optional_decimal(record.get("grossInflow")),
            gross_outflow=_optional_decimal(record.get("grossOutflow")),
            net_amount=_optional_decimal(record.get("netAmount")),
            net_ratio=_optional_decimal(record.get("netRatio")),
        )
        for record in records
        if isinstance(record, dict)
    )
    if len(observations) != len(records):
        raise _schema_error("money-flow daily observation is invalid")
    logical_keys = {(item.bucket, item.trade_date) for item in observations}
    if len(logical_keys) != len(observations):
        raise _schema_error("money-flow daily payload contains duplicate keys")
    return tuple(sorted(observations, key=lambda item: (item.trade_date, item.bucket)))


def _ranking_snapshot(
    payload: dict[str, object], observed_at: datetime
) -> MoneyFlowRankingSnapshot:
    """把 supplier ranking 标准载荷映射为不可变快照领域对象。"""
    records = payload.get("items")
    if not isinstance(records, list) or not records:
        raise _schema_error("money-flow ranking payload has no items")
    items: list[MoneyFlowRankingItem] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("metrics"), list):
            raise _schema_error("money-flow ranking item is invalid")
        scope = _ranking_scope(record.get("scope"))
        for metric in record["metrics"]:
            if not isinstance(metric, dict):
                raise _schema_error("money-flow ranking metric is invalid")
            items.append(
                MoneyFlowRankingItem(
                    supplier_position=_required_int(record, "supplierPosition"),
                    scope=scope,
                    bucket=_required_text(metric, "bucket"),
                    gross_inflow=_optional_decimal(metric.get("grossInflow")),
                    gross_outflow=_optional_decimal(metric.get("grossOutflow")),
                    net_amount=_optional_decimal(metric.get("netAmount")),
                    net_ratio=_optional_decimal(metric.get("netRatio")),
                )
            )
    return MoneyFlowRankingSnapshot(
        target_trade_date=date.fromisoformat(_required_text(payload, "targetTradeDate")),
        observed_at=observed_at,
        source_cutoff_at=observed_at,
        scope_type=MoneyFlowScopeType(_required_text(payload, "scopeType")),
        universe_id=_required_text(payload, "universe"),
        window_type=MoneyFlowWindowType(_required_text(payload, "windowType")),
        window_size=_required_int(payload, "windowSize"),
        ranking_bucket=_required_text(payload, "rankingBucket"),
        ranking_basis=_required_text(payload, "rankingBasis"),
        completeness_basis=_required_text(payload, "completenessBasis"),
        is_complete=_required_bool(payload, "isComplete"),
        items=tuple(items),
    )


def _scope(value: object) -> MoneyFlowScope:
    """解析日序列标准 scope，拒绝缺失或混合身份。"""
    if not isinstance(value, dict):
        raise _schema_error("money-flow scope is invalid")
    scope_type = MoneyFlowScopeType(_required_text(value, "scopeType"))
    if scope_type is MoneyFlowScopeType.EQUITY:
        return MoneyFlowScope(
            scope_type=scope_type,
            exchange=Exchange(_required_text(value, "exchange")),
            symbol=_required_text(value, "symbol"),
            name=_optional_text(value.get("name")),
        )
    if scope_type is MoneyFlowScopeType.SECTOR:
        return MoneyFlowScope(
            scope_type=scope_type,
            sector_scheme=_required_text(value, "scheme"),
            sector_code=_required_text(value, "sectorCode"),
            name=_optional_text(value.get("name")),
        )
    return MoneyFlowScope(
        scope_type=scope_type,
        market_code=_required_text(value, "marketCode"),
        name=_optional_text(value.get("name")),
    )


def _ranking_scope(value: object) -> MoneyFlowScope:
    """解析排行来源身份；最终仍由仓储按目标交易日解析 canonical identity。"""
    if not isinstance(value, dict):
        raise _schema_error("money-flow ranking scope is invalid")
    scope_type = MoneyFlowScopeType(_required_text(value, "scopeType"))
    if scope_type is MoneyFlowScopeType.EQUITY:
        symbol = _required_text(value, "sourceSymbol")
        return MoneyFlowScope(
            scope_type=scope_type,
            exchange=_exchange_from_source_symbol(symbol),
            symbol=symbol,
            name=_optional_text(value.get("name")),
        )
    # 当前 SDK 丢弃东财板块代码，因此该名称只能进入 quarantine；
    # 它不能被当成已解析 canonical sector code。
    source_name = _required_text(value, "sourceName")
    return MoneyFlowScope(
        scope_type=scope_type,
        sector_scheme=_required_text(value, "scheme"),
        sector_code=f"unresolved-name:{source_name}",
        name=source_name,
    )


def _exchange_from_source_symbol(symbol: str) -> Exchange:
    """按来源市场编码规则恢复交易所，canonical 身份仍须由日期感知 resolver 确认。"""
    if len(symbol) != 6 or not symbol.isdigit():
        raise _schema_error("money-flow ranking source symbol is invalid")
    if symbol.startswith(("4", "8", "9")):
        return Exchange.BSE
    if symbol.startswith(("5", "6", "7")):
        return Exchange.SSE
    return Exchange.SZSE


def _required_text(record: dict[str, object], key: str) -> str:
    """读取标准载荷中的非空文本字段。"""
    value = record.get(key)
    if value is None or not str(value).strip():
        raise _schema_error(f"money-flow field {key} is required")
    return str(value).strip()


def _required_int(record: dict[str, object], key: str) -> int:
    """读取正整数标准字段，拒绝布尔值和隐式文本转换。"""
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _schema_error(f"money-flow field {key} must be a positive integer")
    return value


def _required_bool(record: dict[str, object], key: str) -> bool:
    """读取布尔标准字段，避免字符串经 `bool` 转换后改变完整性语义。"""
    value = record.get(key)
    if not isinstance(value, bool):
        raise _schema_error(f"money-flow field {key} must be boolean")
    return value


def _optional_text(value: object) -> str | None:
    """把空白或缺失标准字段转换为真实空值。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _optional_decimal(value: object) -> Decimal | None:
    """解析标准十进制字符串，禁止二进制浮点进入 canonical。"""
    normalized = _optional_text(value)
    return None if normalized is None else Decimal(normalized)


def _schema_error(message: str) -> ProviderError:
    """构造不可重试的标准载荷漂移错误。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)


__all__ = ["MoneyFlowSyncResult", "MoneyFlowSyncService"]
