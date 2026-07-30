"""市场概览、东财板块和申万行业的完整 EOD 同步与原子发布。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.market_overview import (
    MarketComponentCandidate,
    MarketOverviewRepository,
)

_REQUIRED_INDEX_IDS = {"sse-composite", "szse-component", "csi-300", "chinext"}
_LIMIT_UP_STATUSES = {2, 3}
_LIMIT_DOWN_STATUSES = {5, 6}
_AMOUNT_QA_BLOCK_RATIO = Decimal("0.01")
_AMOUNT_QA_WARN_RATIO = Decimal("0.002")
_PRICE_TOLERANCE = Decimal("0.01")
_COMPONENT_NAMESPACE = UUID("7852a713-94f4-4e3c-8a79-f352700da577")


@dataclass(frozen=True, slots=True)
class MarketOverviewSyncResult:
    """返回完整包版本、交易日和幂等插入状态，不泄漏来源秘密或物理主键。"""

    data_version: UUID
    trade_date: date
    published_at: datetime
    inserted: bool
    component_count: int


class MarketOverviewSyncService:
    """协调 Tushare 中立批次、跨组件质量门和写时原子完整包。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: MarketOverviewRepository,
    ) -> None:
        """接收冻结来源和 canonical 仓储；应用层不导入任何供应商实现。"""
        self._source = source
        self._repository = repository

    async def preflight(self, *, trade_date: date) -> dict[str, Any]:
        """在任何 canonical 写入前验证 token、权限、schema、行上限和数据新鲜度。"""
        batch = await self._fetch("market.source.preflight", tradeDate=trade_date.isoformat())
        payload = _decode_payload(batch, "quant-v2.market-source-preflight.v1")
        records = _records(payload)
        if not records or any(record.get("status") != "passed" for record in records):
            raise _schema_error("market source preflight did not pass every required endpoint")
        if payload.get("freshness") != "current":
            raise _schema_error("market source preflight is not current")
        return payload

    async def seed_derivation_inputs(
        self,
        *,
        trade_dates: tuple[date, ...],
    ) -> int:
        """轻量回填近期板块与申万日线，避免首次启动重复抓取高成本 membership。"""
        if not trade_dates or tuple(sorted(set(trade_dates))) != trade_dates:
            raise ValueError("market derivation seed dates must be ordered and unique")
        candidates: list[MarketComponentCandidate] = []
        for target in trade_dates:
            catalog_batch, sector_batch, sw_batch = await asyncio.gather(
                self._fetch("sector.catalog.dc", tradeDate=target.isoformat()),
                self._fetch("sector.quote.eod.dc", tradeDate=target.isoformat()),
                self._fetch("sw.market-data", tradeDate=target.isoformat()),
            )
            catalog = _decode_payload(
                catalog_batch,
                "quant-v2.sector-catalog-dc.v1",
            )
            sector = _join_sector_quotes(
                catalog=catalog,
                quotes=_decode_payload(
                    sector_batch,
                    "quant-v2.sector-quote-eod-dc.v1",
                ),
                catalog_data_version=_candidate_data_version(
                    "sector.catalog.dc",
                    f"trade-date:{target.isoformat()}",
                    catalog,
                ),
            )
            sw = _decode_payload(sw_batch, "quant-v2.sw-market-data.v1")
            _require_daily_partition(
                sector,
                target=target,
                date_field="tradeDate",
                key_fields=("scheme", "sectorCode"),
            )
            _require_daily_partition(
                sw,
                target=target,
                date_field="tradeDate",
                key_fields=("code",),
            )
            candidates.extend(
                (
                    _source_candidate(
                        batch=sector_batch,
                        dataset_code="sector.quote.eod.dc",
                        trade_date=target,
                        payload=sector,
                    ),
                    _source_candidate(
                        batch=sw_batch,
                        dataset_code="sw.market-data",
                        trade_date=target,
                        payload=sw,
                    ),
                )
            )
        return self._repository.publish_derivation_inputs(components=tuple(candidates))

    async def sync(
        self,
        *,
        trade_date: date,
        preflight_checked: bool = False,
    ) -> MarketOverviewSyncResult:
        """同步一个确认开市日；任一必需组件失败都不创建 bundle 或推进 current pointer。"""
        if not preflight_checked:
            await self.preflight(trade_date=trade_date)
        calendar_batch = await self._fetch(
            "market.calendar",
            start=(trade_date - timedelta(days=400)).isoformat(),
            end=(trade_date + timedelta(days=550)).isoformat(),
        )
        calendar = _decode_payload(calendar_batch, "quant-v2.market-calendar.v1")
        previous_trade_date = _previous_trade_date(calendar, trade_date)
        history_start = _history_start(calendar, trade_date)
        sector_catalog_batch, sw_taxonomy_batch = await asyncio.gather(
            self._fetch("sector.catalog.dc", tradeDate=trade_date.isoformat()),
            self._fetch("sw.taxonomy"),
        )
        sector_catalog = _decode_payload(
            sector_catalog_batch,
            "quant-v2.sector-catalog-dc.v1",
        )
        sw_taxonomy = _decode_payload(sw_taxonomy_batch, "quant-v2.sw-taxonomy.v1")
        sector_catalog_version = _candidate_data_version(
            "sector.catalog.dc",
            f"trade-date:{trade_date.isoformat()}",
            sector_catalog,
        )
        sw_taxonomy_version = _candidate_data_version(
            "sw.taxonomy",
            f"trade-date:{trade_date.isoformat()}",
            sw_taxonomy,
        )
        frozen_sectors = json.dumps(
            [
                {
                    "scheme": row["scheme"],
                    "sectorCode": row["sectorCode"],
                    "name": row["name"],
                }
                for row in _records(sector_catalog)
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        frozen_level_three_codes = json.dumps(
            sorted(str(row["code"]) for row in _records(sw_taxonomy) if row["level"] == 3),
            separators=(",", ":"),
        )

        (
            index_batch,
            catalog_batch,
            quote_batch,
            previous_quote_batch,
            basic_batch,
            suspension_batch,
            turnover_qa_batch,
            previous_turnover_qa_batch,
            market_flow_batch,
            equity_flow_batch,
            sector_quote_batch,
            sector_membership_batch,
            sector_flow_batch,
            sw_membership_batch,
            sw_market_batch,
        ) = await asyncio.gather(
            self._fetch(
                "index.bar.1d",
                start=(trade_date - timedelta(days=400)).isoformat(),
                end=trade_date.isoformat(),
            ),
            self._fetch("equity.catalog"),
            self._fetch("equity.quote.eod", tradeDate=trade_date.isoformat()),
            self._fetch(
                "equity.quote.eod",
                tradeDate=previous_trade_date.isoformat(),
            ),
            self._fetch("equity.daily-basic.eod", tradeDate=trade_date.isoformat()),
            self._fetch("equity.suspension.eod", tradeDate=trade_date.isoformat()),
            self._fetch("market.turnover.qa.reported", tradeDate=trade_date.isoformat()),
            self._fetch(
                "market.turnover.qa.reported",
                tradeDate=previous_trade_date.isoformat(),
            ),
            self._fetch("money-flow.market.dc.eod", tradeDate=trade_date.isoformat()),
            self._fetch(
                "money-flow.equity.order-size.eod",
                tradeDate=trade_date.isoformat(),
            ),
            self._fetch("sector.quote.eod.dc", tradeDate=trade_date.isoformat()),
            self._fetch(
                "sector.membership.dc",
                tradeDate=trade_date.isoformat(),
                sectors=frozen_sectors,
            ),
            self._fetch("sector.money-flow.dc.eod", tradeDate=trade_date.isoformat()),
            self._fetch(
                "sw.membership",
                tradeDate=trade_date.isoformat(),
                levelThreeCodes=frozen_level_three_codes,
            ),
            self._fetch("sw.market-data", tradeDate=trade_date.isoformat()),
        )

        payloads = _PayloadSet(
            calendar=calendar,
            indices=_decode_payload(index_batch, "quant-v2.index-bar-1d.v1"),
            catalog=_decode_payload(catalog_batch, "quant-v2.equity-catalog.v1"),
            quotes=_decode_payload(quote_batch, "quant-v2.equity-quote-eod.v1"),
            previous_quotes=_decode_payload(previous_quote_batch, "quant-v2.equity-quote-eod.v1"),
            basics=_decode_payload(basic_batch, "quant-v2.equity-daily-basic-eod.v1"),
            suspensions=_decode_payload(suspension_batch, "quant-v2.equity-suspension-eod.v1"),
            turnover_qa=_decode_payload(
                turnover_qa_batch, "quant-v2.market-turnover-qa-reported.v1"
            ),
            previous_turnover_qa=_decode_payload(
                previous_turnover_qa_batch,
                "quant-v2.market-turnover-qa-reported.v1",
            ),
            market_flow=_decode_payload(market_flow_batch, "quant-v2.money-flow-market-dc-eod.v1"),
            equity_flow=_decode_payload(
                equity_flow_batch,
                "quant-v2.money-flow-equity-order-size-eod.v1",
            ),
            sector_catalog=sector_catalog,
            sector_quotes=_join_sector_quotes(
                catalog=sector_catalog,
                quotes=_decode_payload(
                    sector_quote_batch,
                    "quant-v2.sector-quote-eod-dc.v1",
                ),
                catalog_data_version=sector_catalog_version,
            ),
            sector_memberships=_with_input_versions(
                _decode_payload(
                    sector_membership_batch,
                    "quant-v2.sector-membership-dc.v1",
                ),
                (sector_catalog_version,),
            ),
            sector_flow=_decode_payload(sector_flow_batch, "quant-v2.sector-money-flow-dc-eod.v1"),
            sw_taxonomy=sw_taxonomy,
            sw_memberships=_with_input_versions(
                _decode_payload(sw_membership_batch, "quant-v2.sw-membership.v1"),
                (sw_taxonomy_version,),
            ),
            sw_market=_decode_payload(sw_market_batch, "quant-v2.sw-market-data.v1"),
        )
        validation = _validate_cross_section(
            payloads=payloads,
            trade_date=trade_date,
            previous_trade_date=previous_trade_date,
        )
        limit_codes = ",".join(validation.limit_security_codes)
        limit_batch = await self._fetch(
            "equity.limit-price.eod",
            tradeDate=trade_date.isoformat(),
            codes=limit_codes,
        )
        limits = _decode_payload(limit_batch, "quant-v2.equity-limit-price-eod.v1")
        _validate_limit_prices(
            quotes=payloads.quotes,
            basics=payloads.basics,
            limits=limits,
        )
        payloads = payloads.with_limits(limits)

        derived = _derive_components(
            payloads=payloads,
            validation=validation,
            trade_date=trade_date,
            previous_trade_date=previous_trade_date,
            prior_sector_quotes=self._repository.list_derivation_inputs(
                dataset_code="sector.quote.eod.dc",
                start=history_start,
                end=trade_date - timedelta(days=1),
            ),
            prior_sw_market=self._repository.list_derivation_inputs(
                dataset_code="sw.market-data",
                start=history_start,
                end=trade_date - timedelta(days=1),
            ),
        )
        batches = (
            calendar_batch,
            index_batch,
            catalog_batch,
            quote_batch,
            previous_quote_batch,
            basic_batch,
            suspension_batch,
            limit_batch,
            turnover_qa_batch,
            previous_turnover_qa_batch,
            market_flow_batch,
            equity_flow_batch,
            sector_catalog_batch,
            sector_quote_batch,
            sector_membership_batch,
            sector_flow_batch,
            sw_taxonomy_batch,
            sw_membership_batch,
            sw_market_batch,
        )
        source_candidates = _source_candidates(
            batches=batches,
            payloads=payloads,
            trade_date=trade_date,
            previous_trade_date=previous_trade_date,
        )
        all_candidates = source_candidates + derived.candidates
        overview = _overview_payload(
            payloads=payloads,
            derived=derived,
            validation=validation,
            trade_date=trade_date,
            candidates=all_candidates,
        )
        published = self._repository.publish_complete_bundle(
            trade_date=trade_date,
            components=all_candidates,
            overview=overview,
        )
        return MarketOverviewSyncResult(
            data_version=published.data_version,
            trade_date=published.trade_date,
            published_at=published.published_at,
            inserted=published.inserted,
            component_count=len(all_candidates),
        )

    async def _fetch(self, capability: str, **parameters: str) -> ProviderBatch:
        """通过冻结 provider-neutral port 获取一个能力，禁止 capability fallback。"""
        if capability not in self._source.capabilities():
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                f"required market capability is unavailable: {capability}",
                retryable=False,
            )
        return await self._source.fetch(
            SourceRequest(
                capability=capability,
                parameters=tuple(sorted(parameters.items())),
            )
        )


@dataclass(frozen=True, slots=True)
class _PayloadSet:
    """汇集一个交易日全部已解码来源组件，便于跨组件质量门保持显式输入。"""

    calendar: dict[str, Any]
    indices: dict[str, Any]
    catalog: dict[str, Any]
    quotes: dict[str, Any]
    previous_quotes: dict[str, Any]
    basics: dict[str, Any]
    suspensions: dict[str, Any]
    turnover_qa: dict[str, Any]
    previous_turnover_qa: dict[str, Any]
    market_flow: dict[str, Any]
    equity_flow: dict[str, Any]
    sector_catalog: dict[str, Any]
    sector_quotes: dict[str, Any]
    sector_memberships: dict[str, Any]
    sector_flow: dict[str, Any]
    sw_taxonomy: dict[str, Any]
    sw_memberships: dict[str, Any]
    sw_market: dict[str, Any]
    limits: dict[str, Any] | None = None

    def with_limits(self, limits: dict[str, Any]) -> _PayloadSet:
        """返回只替换涨跌停限价组件的不可变副本。"""
        return replace(self, limits=limits)


@dataclass(frozen=True, slots=True)
class _Validation:
    """保存跨横截面验证产物，后续派生不再重复猜测 universe。"""

    names: dict[str, str]
    current_quotes: dict[str, dict[str, Any]]
    basics: dict[str, dict[str, Any]]
    eligible_codes: frozenset[str]
    suspended_codes: frozenset[str]
    limit_security_codes: tuple[str, ...]
    turnover: dict[str, Any]
    breadth: dict[str, int]
    money_flow_coverage: Decimal
    quality_checks: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class _Derived:
    """保存写时派生组件和首页所需的稳定投影。"""

    candidates: tuple[MarketComponentCandidate, ...]
    equity_rankings: dict[str, list[dict[str, Any]]]
    equity_money_flow_rankings: dict[str, Any]
    sector_rankings: dict[str, Any]
    attention_signals: list[dict[str, Any]]


def _decode_payload(batch: ProviderBatch, expected_schema: str) -> dict[str, Any]:
    """解码 adapter 中立 JSON 并拒绝 schema、来源或能力错配。"""
    try:
        value = json.loads(batch.payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _schema_error("market provider payload is not JSON") from error
    if not isinstance(value, dict) or value.get("schema") != expected_schema:
        raise _schema_error("market provider payload schema mismatch")
    source = value.get("source")
    if not isinstance(source, dict) or source.get("provider") != batch.provider_id:
        raise _schema_error("market provider payload source mismatch")
    _records(value)
    return value


def _records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """读取对象记录数组，拒绝混入标量或嵌套数组。"""
    values = payload.get("records")
    if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
        raise _schema_error("market provider payload records are invalid")
    return values


def _join_sector_quotes(
    *,
    catalog: dict[str, Any],
    quotes: dict[str, Any],
    catalog_data_version: UUID,
) -> dict[str, Any]:
    """把一次冻结 dc_index publication 与一次 dc_daily 横截面按代码 100% 关联。"""
    catalog_by_code = {str(row["sectorCode"]): row for row in _records(catalog)}
    quote_by_code = {
        str(row["sectorCode"]): row
        for row in _records(quotes)
        if str(row["sectorCode"]) in catalog_by_code
    }
    if set(quote_by_code) != set(catalog_by_code):
        raise _schema_error("dc_daily cannot join frozen same-day dc_index at 100 percent")
    joined = [
        {
            **quote_by_code[code],
            "scheme": catalog_by_code[code]["scheme"],
            "name": catalog_by_code[code]["name"],
            "totalMarketValueCny": catalog_by_code[code]["totalMarketValueCny"],
            "advancing": catalog_by_code[code]["advancing"],
            "declining": catalog_by_code[code]["declining"],
        }
        for code in sorted(catalog_by_code)
    ]
    return {
        **quotes,
        "records": joined,
        "catalogSource": _public_source(catalog),
        "inputDataVersions": [str(catalog_data_version)],
        "joinCoverage": "1",
    }


def _with_input_versions(
    payload: dict[str, Any],
    versions: tuple[UUID, ...],
) -> dict[str, Any]:
    """把冻结上游 publication UUID 附到依赖组件，不改变来源记录内容。"""
    return {**payload, "inputDataVersions": [str(value) for value in versions]}


def _previous_trade_date(calendar: dict[str, Any], target: date) -> date:
    """从 SSE/SZSE 来源日历选择目标日前共同最近开市日，不用工作日猜测。"""
    open_by_venue: dict[str, set[date]] = {"SSE": set(), "SZSE": set()}
    for row in _records(calendar):
        venue = str(row.get("venue"))
        if venue in open_by_venue and row.get("isTradingDay") is True:
            open_by_venue[venue].add(date.fromisoformat(str(row["tradeDate"])))
    if target not in open_by_venue["SSE"] or target not in open_by_venue["SZSE"]:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "market overview target is not a common SSE/SZSE trading day",
            retryable=False,
        )
    common_previous = sorted(
        (open_by_venue["SSE"] & open_by_venue["SZSE"]) - {target},
        reverse=True,
    )
    if not common_previous or common_previous[0] >= target:
        common_previous = [value for value in common_previous if value < target]
    if not common_previous:
        raise _schema_error("market calendar has no previous common trading day")
    return common_previous[0]


def _history_start(calendar: dict[str, Any], target: date) -> date:
    """由共同交易日和当前周/月起点决定历史读取下界，并限制异常扫描范围。"""
    common = sorted(value for value in _common_trading_dates(calendar) if value <= target)
    if not common or common[-1] != target:
        raise _schema_error("history window calendar does not include target trading day")
    strength_start = common[max(0, len(common) - 20)]
    week_start = date.fromordinal(target.toordinal() - target.weekday())
    month_start = target.replace(day=1)
    start = min(strength_start, week_start, month_start)
    if (target - start).days > 90:
        raise _schema_error("history window exceeds the defensive 90-day bound")
    return start


def _validate_cross_section(
    *,
    payloads: _PayloadSet,
    trade_date: date,
    previous_trade_date: date,
) -> _Validation:
    """执行股票全集、指数、资金流、板块、申万和成交额对账硬门。"""
    _require_daily_partition(
        payloads.quotes,
        target=trade_date,
        date_field="tradeDate",
        key_fields=("tsCode",),
    )
    _require_daily_partition(
        payloads.previous_quotes,
        target=previous_trade_date,
        date_field="tradeDate",
        key_fields=("tsCode",),
    )
    _require_daily_partition(
        payloads.basics,
        target=trade_date,
        date_field="tradeDate",
        key_fields=("tsCode",),
    )
    _require_daily_partition(
        payloads.suspensions,
        target=trade_date,
        date_field="tradeDate",
        key_fields=("tsCode", "suspendType", "suspendTiming"),
        allow_empty=True,
    )
    _require_daily_partition(
        payloads.turnover_qa,
        target=trade_date,
        date_field="tradeDate",
        key_fields=("venue",),
    )
    _require_daily_partition(
        payloads.previous_turnover_qa,
        target=previous_trade_date,
        date_field="tradeDate",
        key_fields=("venue",),
    )
    _require_daily_partition(
        payloads.market_flow,
        target=trade_date,
        date_field="tradeDate",
        key_fields=("tradeDate",),
    )
    _require_daily_partition(
        payloads.equity_flow,
        target=trade_date,
        date_field="tradeDate",
        key_fields=("tsCode",),
    )
    _require_daily_partition(
        payloads.sector_catalog,
        target=trade_date,
        date_field="tradeDate",
        key_fields=("scheme", "sectorCode"),
    )
    _require_daily_partition(
        payloads.sector_quotes,
        target=trade_date,
        date_field="tradeDate",
        key_fields=("scheme", "sectorCode"),
    )
    _require_daily_partition(
        payloads.sector_memberships,
        target=trade_date,
        date_field="observationDate",
        key_fields=("scheme", "sectorCode", "tsCode"),
    )
    _require_daily_partition(
        payloads.sector_flow,
        target=trade_date,
        date_field="tradeDate",
        key_fields=("scheme", "sectorCode"),
    )
    _require_daily_partition(
        payloads.sw_market,
        target=trade_date,
        date_field="tradeDate",
        key_fields=("code",),
    )
    current_index_rows = [
        row for row in _records(payloads.indices) if row.get("tradeDate") == trade_date.isoformat()
    ]
    if {str(row.get("indexId")) for row in current_index_rows} != _REQUIRED_INDEX_IDS:
        raise _schema_error("four required index quotes are not complete")
    for row in current_index_rows:
        _validate_ohlc(row)
        if Decimal(str(row["close"])) - Decimal(str(row["previousClose"])) != Decimal(
            str(row["change"])
        ):
            raise _schema_error("index change is not reproducible from close and previousClose")

    catalog_rows = _records(payloads.catalog)
    names = {str(row["tsCode"]): str(row["name"]) for row in catalog_rows}
    eligible_codes = frozenset(
        str(row["tsCode"])
        for row in catalog_rows
        if row.get("exchange") in {"SSE", "SZSE"} and _eligible_on(row, trade_date)
    )
    suspension_rows = _records(payloads.suspensions)
    suspended_codes, intraday_suspended_codes = _classify_suspensions(
        suspension_rows,
        eligible_codes=eligible_codes,
    )
    current_quotes = {
        str(row["tsCode"]): row
        for row in _records(payloads.quotes)
        if row.get("tradeDate") == trade_date.isoformat() and row.get("exchange") in {"SSE", "SZSE"}
    }
    if len(current_quotes) != len(
        [
            row
            for row in _records(payloads.quotes)
            if row.get("tradeDate") == trade_date.isoformat()
            and row.get("exchange") in {"SSE", "SZSE"}
        ]
    ):
        raise _schema_error("equity quote cross-section has duplicate identities")
    expected_quotes = eligible_codes - suspended_codes
    if set(current_quotes) != set(expected_quotes):
        raise _schema_error("equity quote universe differs from catalog minus suspensions")
    if not intraday_suspended_codes.issubset(current_quotes):
        raise _schema_error("intraday-suspended equities must remain in daily cross-section")
    for row in current_quotes.values():
        _validate_ohlc(row)

    basics = {
        str(row["tsCode"]): row
        for row in _records(payloads.basics)
        if str(row["tsCode"]) in current_quotes
    }
    if set(basics) != set(current_quotes):
        raise _schema_error("daily_basic join coverage is not 100 percent")
    if any(row.get("limitStatus") is None for row in basics.values()):
        raise _schema_error("daily_basic limit_status is incomplete")
    limit_codes = tuple(
        sorted(
            code
            for code, row in basics.items()
            if int(row["limitStatus"]) in _LIMIT_UP_STATUSES | _LIMIT_DOWN_STATUSES
        )
    )

    previous_quotes = {
        str(row["tsCode"]): row
        for row in _records(payloads.previous_quotes)
        if row.get("tradeDate") == previous_trade_date.isoformat()
        and row.get("exchange") in {"SSE", "SZSE"}
    }
    if len(previous_quotes) < 1000:
        raise _schema_error("previous trading-day equity cross-section is incomplete")
    turnover, turnover_checks = _turnover(
        current_quotes=current_quotes,
        previous_quotes=previous_quotes,
        current_qa=payloads.turnover_qa,
        previous_qa=payloads.previous_turnover_qa,
    )

    breadth = {
        "eligible": len(eligible_codes),
        "advancing": sum(Decimal(str(row["change"])) > 0 for row in current_quotes.values()),
        "flat": sum(Decimal(str(row["change"])) == 0 for row in current_quotes.values()),
        "declining": sum(Decimal(str(row["change"])) < 0 for row in current_quotes.values()),
        "suspended": len(suspended_codes & eligible_codes),
        "unknown": 0,
    }
    if (
        breadth["advancing"] + breadth["flat"] + breadth["declining"] + breadth["suspended"]
        != breadth["eligible"]
    ):
        raise _schema_error("market breadth does not reconcile to eligible universe")

    traded_sh_sz = {
        code for code, row in current_quotes.items() if row["exchange"] in {"SSE", "SZSE"}
    }
    flow_codes = {str(row["tsCode"]) for row in _records(payloads.equity_flow)}
    if not traded_sh_sz:
        raise _schema_error("money-flow universe has no traded SSE/SZSE equities")
    money_flow_coverage = Decimal(len(flow_codes & traded_sh_sz)) / Decimal(len(traded_sh_sz))
    if flow_codes != traded_sh_sz:
        raise _schema_error("equity money-flow coverage is not 100 percent")
    market_flow_rows = _records(payloads.market_flow)
    if (
        len(market_flow_rows) != 1
        or market_flow_rows[0].get("tradeDate") != trade_date.isoformat()
        or market_flow_rows[0].get("netAmountCny") is None
    ):
        raise _schema_error("market money-flow required daily net amount is incomplete")
    _ = Decimal(str(market_flow_rows[0]["netAmountCny"]))

    sector_catalog = {
        (str(row["scheme"]), str(row["sectorCode"])) for row in _records(payloads.sector_catalog)
    }
    sector_quotes = {
        (str(row["scheme"]), str(row["sectorCode"])) for row in _records(payloads.sector_quotes)
    }
    if sector_catalog != sector_quotes:
        raise _schema_error("DC sector EOD does not cover the active catalog")
    sector_flow = {
        (str(row["scheme"]), str(row["sectorCode"])) for row in _records(payloads.sector_flow)
    }
    if sector_flow != sector_catalog:
        raise _schema_error("DC sector money flow does not cover the active catalog")
    member_codes = {str(row["tsCode"]) for row in _records(payloads.sector_memberships)}
    if not member_codes.issubset(names):
        raise _schema_error("DC sector membership contains unresolved equity identities")

    taxonomy_codes = {str(row["code"]) for row in _records(payloads.sw_taxonomy)}
    sw_market_codes = {str(row["code"]) for row in _records(payloads.sw_market)}
    if not taxonomy_codes.issubset(sw_market_codes):
        raise _schema_error("SW market data does not cover the complete taxonomy")
    sw_member_codes = {
        str(row["tsCode"])
        for row in _records(payloads.sw_memberships)
        if _membership_effective(row, trade_date)
    }
    if not sw_member_codes.issubset(names):
        raise _schema_error("SW membership contains unresolved equity identities")

    checks = (
        {
            "code": "index-required-identities",
            "status": "passed",
            "actual": "4",
            "expected": "4",
        },
        {
            "code": "equity-universe-coverage",
            "status": "passed",
            "actual": str(len(current_quotes)),
            "expected": str(len(expected_quotes)),
        },
        {
            "code": "intraday-suspension-quote-coverage",
            "status": "passed",
            "actual": str(len(intraday_suspended_codes & set(current_quotes))),
            "expected": str(len(intraday_suspended_codes)),
        },
        {
            "code": "market-breadth-reconciliation",
            "status": "passed",
            "actual": str(
                breadth["advancing"] + breadth["flat"] + breadth["declining"] + breadth["suspended"]
            ),
            "expected": str(breadth["eligible"]),
        },
        {
            "code": "money-flow-universe-coverage",
            "status": "passed",
            "actual": format(money_flow_coverage, "f"),
            "expected": "1",
        },
        {
            "code": "sector-catalog-coverage",
            "status": "passed",
            "actual": str(len(sector_quotes)),
            "expected": str(len(sector_catalog)),
        },
        {
            "code": "sector-money-flow-coverage",
            "status": "passed",
            "actual": str(len(sector_flow)),
            "expected": str(len(sector_catalog)),
        },
        {
            "code": "sw-taxonomy-market-coverage",
            "status": "passed",
            "actual": str(len(taxonomy_codes)),
            "expected": str(len(taxonomy_codes)),
        },
        *turnover_checks,
    )
    return _Validation(
        names=names,
        current_quotes=current_quotes,
        basics=basics,
        eligible_codes=eligible_codes,
        suspended_codes=suspended_codes,
        limit_security_codes=limit_codes,
        turnover=turnover,
        breadth=breadth,
        money_flow_coverage=money_flow_coverage,
        quality_checks=checks,
    )


def _require_daily_partition(
    payload: dict[str, Any],
    *,
    target: date,
    date_field: str,
    key_fields: tuple[str, ...],
    allow_empty: bool = False,
) -> None:
    """要求日频组件只有目标日且业务键唯一，防止供应商忽略筛选混入旧行。"""
    rows = _records(payload)
    if not rows and not allow_empty:
        raise _schema_error("required daily component is empty")
    target_text = target.isoformat()
    if any(str(row.get(date_field)) != target_text for row in rows):
        raise _schema_error("daily component contains a row outside the target date")
    identities = [tuple(str(row.get(field)) for field in key_fields) for row in rows]
    if len(identities) != len(set(identities)):
        raise _schema_error("daily component contains duplicate business identities")


def _classify_suspensions(
    rows: list[dict[str, Any]],
    *,
    eligible_codes: frozenset[str],
) -> tuple[frozenset[str], frozenset[str]]:
    """区分全天与日内停牌；未知 timing 不得被猜成任一宽度口径。"""
    full_day: set[str] = set()
    intraday: set[str] = set()
    for row in rows:
        code = str(row["tsCode"])
        if code not in eligible_codes:
            continue
        timing = row.get("suspendTiming")
        normalized = None if timing is None else str(timing).strip()
        if normalized is None or normalized in {"全天", "全日"}:
            full_day.add(code)
            continue
        if re.fullmatch(r"\d{2}:\d{2}-\d{2}:\d{2}", normalized):
            intraday.add(code)
            continue
        raise _schema_error("suspend timing semantics are unknown")
    if full_day & intraday:
        raise _schema_error("equity cannot be both full-day and intraday suspended")
    return frozenset(full_day), frozenset(intraday)


def _turnover(
    *,
    current_quotes: dict[str, dict[str, Any]],
    previous_quotes: dict[str, dict[str, Any]],
    current_qa: dict[str, Any],
    previous_qa: dict[str, Any],
) -> tuple[dict[str, Any], tuple[dict[str, str], ...]]:
    """聚合沪深 A 股 daily 成交额，并用 daily_info SH_A/SZ_A 独立阻断对账。"""
    current = _amounts_by_venue(current_quotes.values())
    previous = _amounts_by_venue(previous_quotes.values())
    current_checks = _reconcile_daily_info(current, current_qa)
    previous_checks = _reconcile_daily_info(previous, previous_qa)
    total = current["SSE"] + current["SZSE"]
    previous_total = previous["SSE"] + previous["SZSE"]
    change = total - previous_total
    change_percent = Decimal("0") if previous_total == 0 else change / previous_total * 100
    return (
        {
            "sseAmountCny": format(current["SSE"], "f"),
            "szseAmountCny": format(current["SZSE"], "f"),
            "totalAmountCny": format(total, "f"),
            "previousTotalAmountCny": format(previous_total, "f"),
            "changeAmountCny": format(change, "f"),
            "changePercent": format(change_percent, "f"),
            "label": "沪深 A 股成交额",
            "universe": "CN-A-SSE-SZSE",
            "methodologyId": "sum-tushare-daily-a-share-amount-cny-v1",
        },
        current_checks + previous_checks,
    )


def _amounts_by_venue(rows: Any) -> dict[str, Decimal]:
    """只聚合 SSE/SZSE A 股日线 amountCny，BSE 与指数 amount 不进入该口径。"""
    amounts = {"SSE": Decimal("0"), "SZSE": Decimal("0")}
    for row in rows:
        venue = str(row["exchange"])
        if venue in amounts:
            amount = row.get("amountCny")
            if amount is None or Decimal(str(amount)) < 0:
                raise _schema_error("equity amount is missing or negative")
            amounts[venue] += Decimal(str(amount))
    return amounts


def _reconcile_daily_info(
    aggregated: dict[str, Decimal], qa_payload: dict[str, Any]
) -> tuple[dict[str, str], ...]:
    """对比交易所 A 股统计；偏差超过 1% 阻断，0.2% 以上仍保存通过阈值证据。"""
    reported = {str(row["venue"]): Decimal(str(row["amountCny"])) for row in _records(qa_payload)}
    if set(reported) != {"SSE", "SZSE"}:
        raise _schema_error("daily_info SH_A/SZ_A QA partitions are incomplete")
    checks: list[dict[str, str]] = []
    for venue in ("SSE", "SZSE"):
        denominator = reported[venue]
        if denominator <= 0:
            raise _schema_error("daily_info reported amount is not positive")
        ratio = abs(aggregated[venue] - denominator) / denominator
        if ratio > _AMOUNT_QA_BLOCK_RATIO:
            raise _schema_error("daily amount differs from daily_info by more than one percent")
        checks.append(
            {
                "code": f"turnover-daily-info-reconciliation-{venue.lower()}",
                "status": "passed",
                "actual": format(ratio, "f"),
                "expected": f"<={format(_AMOUNT_QA_BLOCK_RATIO, 'f')}",
            }
        )
        if ratio > _AMOUNT_QA_WARN_RATIO:
            checks.append(
                {
                    "code": f"turnover-daily-info-warning-band-{venue.lower()}",
                    "status": "passed",
                    "actual": format(ratio, "f"),
                    "expected": f"<={format(_AMOUNT_QA_BLOCK_RATIO, 'f')}",
                }
            )
    return tuple(checks)


def _validate_limit_prices(
    *,
    quotes: dict[str, Any],
    basics: dict[str, Any],
    limits: dict[str, Any],
) -> None:
    """逐 source-reported 涨跌停证券核对收盘与 stk_limit，绝不全市场接受 5800 截断。"""
    quote_by_code = {str(row["tsCode"]): row for row in _records(quotes)}
    basic_by_code = {str(row["tsCode"]): row for row in _records(basics)}
    limit_by_code = {str(row["tsCode"]): row for row in _records(limits)}
    expected = {
        code
        for code, row in basic_by_code.items()
        if int(row["limitStatus"]) in _LIMIT_UP_STATUSES | _LIMIT_DOWN_STATUSES
    }
    if set(limit_by_code) != expected:
        raise _schema_error("per-security stk_limit verification is incomplete")
    for code, row in limit_by_code.items():
        status = int(basic_by_code[code]["limitStatus"])
        target = row["upLimit"] if status in _LIMIT_UP_STATUSES else row["downLimit"]
        if (
            abs(Decimal(str(quote_by_code[code]["close"])) - Decimal(str(target)))
            > _PRICE_TOLERANCE
        ):
            raise _schema_error("source-reported limit status does not match stk_limit price")


def _derive_components(
    *,
    payloads: _PayloadSet,
    validation: _Validation,
    trade_date: date,
    previous_trade_date: date,
    prior_sector_quotes: tuple[Any, ...],
    prior_sw_market: tuple[Any, ...],
) -> _Derived:
    """从冻结输入在写时派生宽度、排行、持续性和注意信号，保留版本化方法学。"""
    equity_rows = [
        {
            "exchange": row["exchange"],
            "symbol": row["symbol"],
            "tsCode": code,
            "name": validation.names[code],
            "close": row["close"],
            "changePercent": row["changePercent"],
            "amountCny": row["amountCny"],
            "turnoverPercent": validation.basics[code]["turnoverPercent"],
        }
        for code, row in validation.current_quotes.items()
    ]
    equity_rankings = {
        "gainers": _rank_equities(equity_rows, "changePercent", reverse=True),
        "losers": _rank_equities(equity_rows, "changePercent", reverse=False),
        "amount": _rank_equities(equity_rows, "amountCny", reverse=True),
        "turnover": _rank_equities(equity_rows, "turnoverPercent", reverse=True),
    }
    flow_rows = []
    for row in _records(payloads.equity_flow):
        quote = validation.current_quotes[str(row["tsCode"])]
        flow_rows.append(
            {
                "exchange": row["exchange"],
                "symbol": row["symbol"],
                "tsCode": row["tsCode"],
                "name": validation.names[str(row["tsCode"])],
                "netAmountCny": row["netAmountCny"],
                "buyLargeAmountCny": row["buyLargeAmountCny"],
                "sellLargeAmountCny": row["sellLargeAmountCny"],
                "changePercent": quote["changePercent"],
            }
        )
    inflow = _rank_flows(flow_rows, reverse=True)
    outflow = _rank_flows(flow_rows, reverse=False)
    equity_money_flow_rankings = {
        "source": _public_source(payloads.equity_flow),
        "methodologyId": "tushare-order-size-flow",
        "methodologyVersion": "1",
        "universe": "CN-A-SSE-SZSE-TRADED",
        "coverage": format(validation.money_flow_coverage, "f"),
        "inflow": inflow,
        "outflow": outflow,
    }
    sector_rankings, strength_records, strength_input_versions = _sector_strength(
        payloads=payloads,
        validation=validation,
        trade_date=trade_date,
        prior_sector_quotes=prior_sector_quotes,
    )
    limits = {
        "limitUp": sum(
            int(row["limitStatus"]) in _LIMIT_UP_STATUSES for row in validation.basics.values()
        ),
        "limitDown": sum(
            int(row["limitStatus"]) in _LIMIT_DOWN_STATUSES for row in validation.basics.values()
        ),
        "rulesVersion": "tushare-daily-basic-limit-status-v1",
    }
    attention = _attention_signals(
        turnover=validation.turnover,
        breadth=validation.breadth,
        limits=limits,
        sector_rankings=sector_rankings,
        trade_date=trade_date,
    )
    observed_at = datetime.now(UTC)
    period_bar_candidates = _period_bar_candidates(
        payloads=payloads,
        trade_date=trade_date,
        prior_sector_quotes=prior_sector_quotes,
        prior_sw_market=prior_sw_market,
        observed_at=observed_at,
    )
    candidates = (
        _derived_candidate(
            "equity.market-snapshot.eod",
            trade_date,
            {"schema": "quant-v2.equity-market-snapshot-eod.v1", "records": equity_rows},
            observed_at,
            "equity-market-snapshot-join-v1",
        ),
        _derived_candidate(
            "market.turnover.eod",
            trade_date,
            {
                "schema": "quant-v2.market-turnover-eod.v1",
                "tradeDate": trade_date.isoformat(),
                "previousTradeDate": previous_trade_date.isoformat(),
                **validation.turnover,
            },
            observed_at,
            "sum-tushare-daily-a-share-amount-cny-v1",
        ),
        _derived_candidate(
            "market.breadth.eod",
            trade_date,
            {
                "schema": "quant-v2.market-breadth-eod.v1",
                "tradeDate": trade_date.isoformat(),
                **validation.breadth,
            },
            observed_at,
            "cn-a-daily-cross-section-breadth-v1",
        ),
        _derived_candidate(
            "market.limit-breadth.eod",
            trade_date,
            {
                "schema": "quant-v2.market-limit-breadth-eod.v1",
                "tradeDate": trade_date.isoformat(),
                **limits,
            },
            observed_at,
            "tushare-daily-basic-limit-status-v1",
        ),
        _derived_candidate(
            "equity.market-ranking.eod",
            trade_date,
            {
                "schema": "quant-v2.equity-market-ranking-eod.v1",
                "tradeDate": trade_date.isoformat(),
                "source": _public_source(payloads.quotes),
                "universe": "CN-A-SSE-SZSE-ELIGIBLE",
                "coverage": "1",
                "finality": "final",
                "quality": {
                    "status": "passed",
                    "universeVersion": _universe_version(
                        validation.eligible_codes,
                        trade_date,
                    ),
                    "checks": [
                        "equity-universe-coverage",
                        "daily-basic-join-coverage",
                        "market-breadth-reconciliation",
                    ],
                },
                **equity_rankings,
            },
            observed_at,
            "stable-metric-rank-security-code-tie-v1",
        ),
        _derived_candidate(
            "money-flow.equity-ranking.eod",
            trade_date,
            {
                "schema": "quant-v2.money-flow-equity-ranking-eod.v1",
                "tradeDate": trade_date.isoformat(),
                **equity_money_flow_rankings,
                "finality": "final",
                "quality": {
                    "status": "passed",
                    "checks": [
                        {
                            "code": "money-flow-universe-coverage",
                            "status": "passed",
                            "actual": format(validation.money_flow_coverage, "f"),
                            "expected": "1",
                        }
                    ],
                },
            },
            observed_at,
            "tushare-order-size-flow-rank-v1",
        ),
        _derived_candidate(
            "sector.strength.eod",
            trade_date,
            {
                "schema": "quant-v2.sector-strength-eod.v1",
                "tradeDate": trade_date.isoformat(),
                "methodologyVersion": "sector-strength-v1",
                "source": _public_source(payloads.sector_quotes),
                "quality": {
                    "status": "passed",
                    "validUniverseCount": len(_records(payloads.sector_quotes)),
                    "validUniverseCountByScheme": {
                        scheme: {
                            str(window): sum(
                                row["scheme"] == scheme
                                and row["window"] == window
                                and row["availability"] == "available"
                                for row in strength_records
                            )
                            for window in (1, 5, 20)
                        }
                        for scheme in (
                            "eastmoney.industry",
                            "eastmoney.concept",
                        )
                    },
                    "checks": [
                        "same-day-dc-index-dc-daily-join",
                        "scheme-isolation",
                        "fixed-input-publications",
                    ],
                },
                "inputDataVersionsByWindow": strength_input_versions,
                "records": strength_records,
            },
            observed_at,
            "sector-strength-v1",
        ),
        _derived_candidate(
            "market.attention-signal.eod",
            trade_date,
            {
                "schema": "quant-v2.market-attention-signal-eod.v1",
                "tradeDate": trade_date.isoformat(),
                "records": attention,
            },
            observed_at,
            "market-attention-rules-v1",
        ),
        *period_bar_candidates,
    )
    return _Derived(
        candidates=candidates,
        equity_rankings=equity_rankings,
        equity_money_flow_rankings=equity_money_flow_rankings,
        sector_rankings=sector_rankings,
        attention_signals=attention,
    )


def _source_candidates(
    *,
    batches: tuple[ProviderBatch, ...],
    payloads: _PayloadSet,
    trade_date: date,
    previous_trade_date: date,
) -> tuple[MarketComponentCandidate, ...]:
    """把所有真实来源批次登记为独立 canonical 组件，不抹掉供应商方法学。"""
    dataset_by_capability = {
        "market.calendar": ("market.calendar", None),
        "index.bar.1d": ("index.bar.1d", trade_date),
        "equity.catalog": ("equity.catalog", None),
        "equity.quote.eod": (None, None),
        "equity.daily-basic.eod": ("equity.daily-basic.eod", trade_date),
        "equity.suspension.eod": ("equity.suspension.eod", trade_date),
        "equity.limit-price.eod": ("equity.limit-price.eod", trade_date),
        "market.turnover.qa.reported": (None, None),
        "money-flow.market.dc.eod": ("money-flow.market.dc.eod", trade_date),
        "money-flow.equity.order-size.eod": (
            "money-flow.equity.order-size.eod",
            trade_date,
        ),
        "sector.catalog.dc": ("sector.catalog.dc", trade_date),
        "sector.quote.eod.dc": ("sector.quote.eod.dc", trade_date),
        "sector.membership.dc": ("sector.membership.dc", trade_date),
        "sector.money-flow.dc.eod": ("sector.money-flow.dc.eod", trade_date),
        "sw.taxonomy": ("sw.taxonomy", trade_date),
        "sw.membership": ("sw.membership", trade_date),
        "sw.market-data": ("sw.market-data", trade_date),
    }
    candidates: list[MarketComponentCandidate] = []
    quote_seen = 0
    turnover_qa_seen = 0
    for batch in batches:
        decoded = json.loads(batch.payload)
        if not isinstance(decoded, dict):
            raise _schema_error("source component payload is not an object")
        payload = decoded
        if batch.capability == "sector.quote.eod.dc":
            # dc_daily 不含 scheme/name，必须保存与本 bundle 冻结目录关联后的 canonical 载荷。
            payload = payloads.sector_quotes
        if batch.capability == "sector.membership.dc":
            payload = payloads.sector_memberships
        if batch.capability == "sw.membership":
            payload = payloads.sw_memberships
        dataset_code, component_date = dataset_by_capability[batch.capability]
        if batch.capability == "equity.quote.eod":
            quote_seen += 1
            dataset_code = (
                "equity.quote.eod" if quote_seen == 1 else "equity.quote.previous-input.eod"
            )
            component_date = trade_date if quote_seen == 1 else previous_trade_date
        if batch.capability == "market.turnover.qa.reported":
            turnover_qa_seen += 1
            dataset_code = (
                "market.turnover.qa.reported"
                if turnover_qa_seen == 1
                else "market.turnover.qa.previous-input.reported"
            )
            component_date = trade_date if turnover_qa_seen == 1 else previous_trade_date
        if dataset_code is None:
            raise RuntimeError("source component mapping is incomplete")
        partition_key = (
            "global" if component_date is None else f"trade-date:{component_date.isoformat()}"
        )
        candidates.append(
            MarketComponentCandidate(
                data_version=_candidate_data_version(
                    dataset_code,
                    partition_key,
                    payload,
                ),
                dataset_code=dataset_code,
                partition_key=partition_key,
                trade_date=component_date,
                payload=payload,
                source=_source_from_batch(batch, payload),
                methodology=_methodology(payload),
                quality={
                    "status": "passed",
                    "recordCount": len(_records(payload)),
                    "checks": ["schema", "unique_identity", "source_finality"],
                },
                observed_at=batch.observed_at,
            )
        )
    return tuple(candidates)


def _source_candidate(
    *,
    batch: ProviderBatch,
    dataset_code: str,
    trade_date: date,
    payload: dict[str, Any],
) -> MarketComponentCandidate:
    """构造一个已校验来源日线 candidate，供轻量 bootstrap 与完整同步共用口径。"""
    partition_key = f"trade-date:{trade_date.isoformat()}"
    return MarketComponentCandidate(
        data_version=_candidate_data_version(
            dataset_code,
            partition_key,
            payload,
        ),
        dataset_code=dataset_code,
        partition_key=partition_key,
        trade_date=trade_date,
        payload=payload,
        source=_source_from_batch(batch, payload),
        methodology=_methodology(payload),
        quality={
            "status": "passed",
            "recordCount": len(_records(payload)),
            "checks": ["schema", "unique_identity", "source_finality"],
        },
        observed_at=batch.observed_at,
    )


def _period_bar_candidates(
    *,
    payloads: _PayloadSet,
    trade_date: date,
    prior_sector_quotes: tuple[Any, ...],
    prior_sw_market: tuple[Any, ...],
    observed_at: datetime,
) -> tuple[MarketComponentCandidate, ...]:
    """在同步写路径从已发布完整日线生成东财与申万三周期正式 bar 组件。"""
    sector_rows = _historical_rows(prior_sector_quotes)
    sector_rows.extend(_records(payloads.sector_quotes))
    sw_rows = _historical_rows(prior_sw_market)
    sw_rows.extend(_records(payloads.sw_market))
    candidates: list[MarketComponentCandidate] = []
    common_dates = _common_trading_dates(payloads.calendar)
    for period in ("1d", "1w", "1mo"):
        finality = _period_is_final(payloads.calendar, trade_date, period)
        if not finality:
            # 未结束自然周/月不形成 final bar；下一交易日跨周期时才正式发布。
            continue
        sector_input_versions = _period_input_versions(
            prior_components=prior_sector_quotes,
            current_payload=payloads.sector_quotes,
            current_dataset="sector.quote.eod.dc",
            trade_date=trade_date,
            period=period,
        )
        sw_input_versions = _period_input_versions(
            prior_components=prior_sw_market,
            current_payload=payloads.sw_market,
            current_dataset="sw.market-data",
            trade_date=trade_date,
            period=period,
        )
        expected_dates = frozenset(
            value
            for value in common_dates
            if value <= trade_date
            and _period_bucket(value, period) == _period_bucket(trade_date, period)
        )
        sector_bars = _aggregate_period_rows(
            rows=sector_rows,
            trade_date=trade_date,
            period=period,
            identity_fields=("scheme", "sectorCode"),
            finality=finality,
            expected_dates=expected_dates,
        )
        sw_bars = _aggregate_period_rows(
            rows=sw_rows,
            trade_date=trade_date,
            period=period,
            identity_fields=("code",),
            finality=finality,
            expected_dates=expected_dates,
        )
        candidates.append(
            _derived_candidate(
                f"sector.bar.{period}.dc",
                trade_date,
                {
                    "schema": f"quant-v2.sector-bar-{period}-dc.v1",
                    "period": period,
                    "tradeDate": trade_date.isoformat(),
                    "source": _public_source(payloads.sector_quotes),
                    "methodology": {
                        "id": (
                            "source-reported-daily-bar"
                            if period == "1d"
                            else "calendar-bounded-ohlcv-aggregation"
                        ),
                        "version": "1",
                        "status": ("source_reported" if period == "1d" else "platform_derived"),
                        "inputDataset": "sector.quote.eod.dc",
                    },
                    "inputDataVersions": sector_input_versions,
                    "records": sector_bars,
                },
                observed_at,
                (
                    "source-reported-daily-bar"
                    if period == "1d"
                    else "calendar-bounded-ohlcv-aggregation"
                ),
            )
        )
        candidates.append(
            _derived_candidate(
                f"sw.bar.{period}",
                trade_date,
                {
                    "schema": f"quant-v2.sw-bar-{period}.v1",
                    "period": period,
                    "tradeDate": trade_date.isoformat(),
                    "source": _public_source(payloads.sw_market),
                    "methodology": {
                        "id": (
                            "source-reported-daily-bar"
                            if period == "1d"
                            else "calendar-bounded-ohlcv-aggregation"
                        ),
                        "version": "1",
                        "status": ("source_reported" if period == "1d" else "platform_derived"),
                        "inputDataset": "sw.market-data",
                    },
                    "inputDataVersions": sw_input_versions,
                    "records": sw_bars,
                },
                observed_at,
                (
                    "source-reported-daily-bar"
                    if period == "1d"
                    else "calendar-bounded-ohlcv-aggregation"
                ),
            )
        )
    return tuple(candidates)


def _period_input_versions(
    *,
    prior_components: tuple[Any, ...],
    current_payload: dict[str, Any],
    current_dataset: str,
    trade_date: date,
    period: str,
) -> list[str]:
    """冻结周期 bar 使用的全部日线 publication UUID，顺序按交易日递增。"""
    inputs = [
        component
        for component in prior_components
        if component.trade_date is not None
        and _period_bucket(component.trade_date, period) == _period_bucket(trade_date, period)
    ]
    versions = [str(component.data_version) for component in inputs]
    partition_key = f"trade-date:{trade_date.isoformat()}"
    versions.append(
        str(
            _candidate_data_version(
                current_dataset,
                partition_key,
                current_payload,
            )
        )
    )
    return versions


def _historical_rows(components: tuple[Any, ...]) -> list[dict[str, Any]]:
    """按发布顺序收集历史日线，并以身份和交易日保留最后一次正式修订。"""
    latest: dict[tuple[str, ...], dict[str, Any]] = {}
    for component in components:
        for row in _records(component.payload):
            identity = (
                str(row.get("scheme") or ""),
                str(row.get("sectorCode") or row.get("code") or ""),
                str(row["tradeDate"]),
            )
            latest[identity] = row
    return list(latest.values())


def _aggregate_period_rows(
    *,
    rows: list[dict[str, Any]],
    trade_date: date,
    period: str,
    identity_fields: tuple[str, ...],
    finality: bool,
    expected_dates: frozenset[date],
) -> list[dict[str, Any]]:
    """按同一交易日历周期聚合 OHLCV；只生成目标日可见的一次写时 revision。"""
    target_bucket = _period_bucket(trade_date, period)
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        row_date = date.fromisoformat(str(row["tradeDate"]))
        if row_date > trade_date or _period_bucket(row_date, period) != target_bucket:
            continue
        identity = tuple(str(row[field]) for field in identity_fields)
        grouped.setdefault(identity, []).append(row)
    records: list[dict[str, Any]] = []
    for identity, values in sorted(grouped.items()):
        ordered = sorted(values, key=lambda item: str(item["tradeDate"]))
        if ordered[-1]["tradeDate"] != trade_date.isoformat():
            continue
        if len({str(item["tradeDate"]) for item in ordered}) != len(ordered):
            raise _schema_error("period bar input contains duplicate trade dates")
        actual_dates = {date.fromisoformat(str(item["tradeDate"])) for item in ordered}
        if actual_dates != expected_dates:
            # 新板块或缺日输入不形成部分周期 bar；完整覆盖后才进入正式历史。
            continue
        first = ordered[0]
        last = ordered[-1]
        previous_close = Decimal(str(first["previousClose"]))
        close = Decimal(str(last["close"]))
        change = close - previous_close
        change_percent = (
            Decimal("0") if previous_close == 0 else change / previous_close * Decimal("100")
        )
        volume = _sum_optional_decimal(ordered, "volume")
        amount = _sum_optional_decimal(ordered, "amountCny")
        turnover = _optional_row_decimal(last, "turnoverPercent") if period == "1d" else None
        high = max(Decimal(str(item["high"])) for item in ordered)
        low = min(Decimal(str(item["low"])) for item in ordered)
        amplitude = (
            Decimal("0") if previous_close == 0 else (high - low) / previous_close * Decimal("100")
        )
        record: dict[str, Any] = {
            **dict(zip(identity_fields, identity, strict=True)),
            "name": last["name"],
            "period": period,
            "periodKey": _period_key(trade_date, period),
            "periodStart": str(first["tradeDate"]),
            "periodEnd": trade_date.isoformat(),
            "open": str(first["open"]),
            "high": format(high, "f"),
            "low": format(low, "f"),
            "close": format(close, "f"),
            "previousClose": format(previous_close, "f"),
            "change": format(change, "f"),
            "changePercent": format(change_percent, "f"),
            "volume": None if volume is None else format(volume, "f"),
            "volumeAvailability": ("available" if volume is not None else "source_not_reported"),
            "amountCny": None if amount is None else format(amount, "f"),
            "amountAvailability": ("available" if amount is not None else "source_not_reported"),
            "amplitudePercent": format(amplitude, "f"),
            "turnoverPercent": None if turnover is None else format(turnover, "f"),
            "turnoverAvailability": (
                "available"
                if turnover is not None
                else ("source_not_reported" if period == "1d" else "aggregation_unsupported")
            ),
            "isFinal": finality,
        }
        records.append(record)
    if not records:
        raise _schema_error(f"{period} period bar aggregation has no target-day records")
    return records


def _period_bucket(value: date, period: str) -> tuple[int, int]:
    """把交易日映射到日、ISO 周或自然月键，不使用固定天数窗口。"""
    if period == "1d":
        return value.year, value.toordinal()
    if period == "1w":
        iso_year, iso_week, _ = value.isocalendar()
        return iso_year, iso_week
    if period == "1mo":
        return value.year, value.month
    raise ValueError("unsupported market bar period")


def _period_key(value: date, period: str) -> str:
    """返回可审计周期键，供 reader 选择同周期最后一次写时 revision。"""
    year, ordinal = _period_bucket(value, period)
    return f"{period}:{year}:{ordinal:03d}"


def _period_is_final(calendar: dict[str, Any], target: date, period: str) -> bool:
    """用已发布沪深共同交易日的下一开市日判断周月周期是否结束。"""
    if period == "1d":
        return True
    common = _common_trading_dates(calendar)
    future = sorted(value for value in common if value > target)
    if not future:
        raise _schema_error("calendar has no next common trading day for period finality")
    return _period_bucket(future[0], period) != _period_bucket(target, period)


def _common_trading_dates(calendar: dict[str, Any]) -> set[date]:
    """读取沪深共同开市集合，周期边界不采用周末或节假日猜测。"""
    by_venue: dict[str, set[date]] = {"SSE": set(), "SZSE": set()}
    for row in _records(calendar):
        venue = str(row.get("venue"))
        if venue in by_venue and row.get("isTradingDay") is True:
            by_venue[venue].add(date.fromisoformat(str(row["tradeDate"])))
    return by_venue["SSE"] & by_venue["SZSE"]


def _sum_optional_decimal(rows: list[dict[str, Any]], field: str) -> Decimal | None:
    """仅在周期内每日值均可用时聚合可选指标，避免部分样本伪装完整。"""
    values = [row.get(field) for row in rows]
    if any(value is None for value in values):
        return None
    return sum((Decimal(str(value)) for value in values), Decimal("0"))


def _optional_row_decimal(row: dict[str, Any], field: str) -> Decimal | None:
    """读取单日可空十进制，供不适用跨日聚合的指标保留来源空值。"""
    value = row.get(field)
    return None if value is None else Decimal(str(value))


def _overview_payload(
    *,
    payloads: _PayloadSet,
    derived: _Derived,
    validation: _Validation,
    trade_date: date,
    candidates: tuple[MarketComponentCandidate, ...],
) -> dict[str, Any]:
    """构造必填首页 DTO；dataVersion/publishedAt 由仓储原子写入时注入。"""
    indices = []
    for row in _records(payloads.indices):
        if row.get("tradeDate") != trade_date.isoformat():
            continue
        indices.append(
            {
                "indexId": row["indexId"],
                "name": row["name"],
                "point": row["close"],
                "previousClose": row["previousClose"],
                "change": row["change"],
                "changePercent": row["changePercent"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "volume": row["volume"],
                "volumeUnit": "lot",
                "amountCny": row["amountCny"],
                "source": _public_source(payloads.indices),
            }
        )
    indices.sort(key=lambda row: str(row["indexId"]))
    market_flow_rows = _records(payloads.market_flow)
    if len(market_flow_rows) != 1:
        raise _schema_error("market money-flow component must contain one daily record")
    limit_candidate = next(
        candidate
        for candidate in derived.candidates
        if candidate.dataset_code == "market.limit-breadth.eod"
    )
    source_bindings = [
        {
            "role": (
                "derived" if candidate.source["provider"] == "quant-v2-derivation" else "external"
            ),
            "component": candidate.dataset_code,
            **_public_source_binding(candidate.source),
            **(
                {"methodology": candidate.methodology}
                if candidate.source["provider"] == "quant-v2-derivation"
                else {}
            ),
        }
        for candidate in sorted(candidates, key=lambda item: item.dataset_code)
    ]
    return {
        "tradeDate": trade_date.isoformat(),
        "finality": "final",
        "status": {
            "market": "closed",
            "freshness": "current",
            "quality": "passed",
        },
        "indices": indices,
        "turnover": validation.turnover,
        "breadth": validation.breadth,
        "limits": {
            key: value
            for key, value in limit_candidate.payload.items()
            if key in {"limitUp", "limitDown", "rulesVersion"}
        },
        "marketMoneyFlow": {
            "source": _public_source(payloads.market_flow),
            "methodologyId": "eastmoney-market-flow-dc",
            "methodologyVersion": "unknown",
            "netAmountCny": market_flow_rows[0]["netAmountCny"],
        },
        "equityMoneyFlowRankings": {
            **{
                key: value
                for key, value in derived.equity_money_flow_rankings.items()
                if key not in {"inflow", "outflow"}
            },
            "inflow": derived.equity_money_flow_rankings["inflow"][:10],
            "outflow": derived.equity_money_flow_rankings["outflow"][:10],
        },
        "equityRankings": {key: rows[:10] for key, rows in derived.equity_rankings.items()},
        "sectorRankings": derived.sector_rankings,
        "attentionSignals": derived.attention_signals,
        "quality": {
            "componentCount": len(candidates),
            "passedCount": len(candidates),
            "universeVersion": _universe_version(validation.eligible_codes, trade_date),
            "sourceBindings": source_bindings,
            "checks": list(validation.quality_checks),
        },
    }


def _sector_strength(
    *,
    payloads: _PayloadSet,
    validation: _Validation,
    trade_date: date,
    prior_sector_quotes: tuple[Any, ...],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, list[str]]]:
    """按 scheme 独立计算 1/5/20 日有效样本，不跨东财行业与概念合榜。"""
    history_by_identity: dict[tuple[str, str], dict[date, Decimal]] = {}
    for component in prior_sector_quotes:
        component_date = component.trade_date
        if component_date is None:
            continue
        for row in _records(component.payload):
            identity = (str(row["scheme"]), str(row["sectorCode"]))
            history_by_identity.setdefault(identity, {})[component_date] = Decimal(
                str(row["changePercent"])
            )
    common_dates = sorted(
        value for value in _common_trading_dates(payloads.calendar) if value <= trade_date
    )
    if not common_dates or common_dates[-1] != trade_date:
        raise _schema_error("sector strength calendar does not include target trading day")
    component_versions_by_date = {
        component.trade_date: str(component.data_version)
        for component in prior_sector_quotes
        if component.trade_date is not None
    }
    component_versions_by_date[trade_date] = str(
        _candidate_data_version(
            "sector.quote.eod.dc",
            f"trade-date:{trade_date.isoformat()}",
            payloads.sector_quotes,
        )
    )
    input_versions_by_window = {
        str(window): [
            component_versions_by_date[value]
            for value in common_dates[-window:]
            if value in component_versions_by_date
        ]
        for window in (1, 5, 20)
    }
    membership_by_sector: dict[tuple[str, str], list[str]] = {}
    for row in _records(payloads.sector_memberships):
        identity = (str(row["scheme"]), str(row["sectorCode"]))
        membership_by_sector.setdefault(identity, []).append(str(row["tsCode"]))
    records: list[dict[str, Any]] = []
    for row in _records(payloads.sector_quotes):
        identity = (str(row["scheme"]), str(row["sectorCode"]))
        history = dict(history_by_identity.get(identity, {}))
        history[trade_date] = Decimal(str(row["changePercent"]))
        members = membership_by_sector.get(identity, [])
        leaders = [
            validation.current_quotes[code] for code in members if code in validation.current_quotes
        ]
        leader = (
            max(leaders, key=lambda item: Decimal(str(item["changePercent"]))) if leaders else None
        )
        for window in (1, 5, 20):
            expected_dates = common_dates[-window:]
            available_dates = [value for value in expected_dates if value in history]
            available = len(expected_dates) == window and len(available_dates) == window
            cumulative = (
                _compound_percent([history[value] for value in expected_dates])
                if available
                else None
            )
            records.append(
                {
                    "scheme": identity[0],
                    "sectorCode": identity[1],
                    "name": row["name"],
                    "tradeDate": trade_date.isoformat(),
                    "window": window,
                    "changePercent": row["changePercent"],
                    "turnoverPercent": row.get("turnoverPercent"),
                    "amountCny": row.get("amountCny"),
                    "cumulativeReturn": (None if cumulative is None else format(cumulative, "f")),
                    "upDays": sum(history[value] > 0 for value in available_dates),
                    "medianRank": None,
                    "validSamples": len(available_dates),
                    "coverage": format(
                        Decimal(len(available_dates)) / Decimal(window),
                        "f",
                    ),
                    "availability": ("available" if available else "insufficient_history"),
                    "leadingEquity": (
                        None
                        if leader is None
                        else {
                            "exchange": leader["exchange"],
                            "symbol": leader["symbol"],
                            "name": validation.names[str(leader["tsCode"])],
                            "changePercent": leader["changePercent"],
                        }
                    ),
                }
            )
    ranking: dict[str, Any] = {}
    for scheme, output_key in (
        ("eastmoney.industry", "eastmoneyIndustry"),
        ("eastmoney.concept", "eastmoneyConcept"),
    ):
        one_day = [
            row
            for row in records
            if row["scheme"] == scheme and row["window"] == 1 and row["availability"] == "available"
        ]
        strongest = sorted(
            one_day,
            key=lambda item: (-Decimal(str(item["changePercent"])), str(item["sectorCode"])),
        )[:5]
        weakest = sorted(
            one_day,
            key=lambda item: (Decimal(str(item["changePercent"])), str(item["sectorCode"])),
        )[:5]
        ranking[output_key] = {
            "strongest": [_sector_rank_item(index, row) for index, row in enumerate(strongest, 1)],
            "weakest": [_sector_rank_item(index, row) for index, row in enumerate(weakest, 1)],
        }
    return ranking, records, input_versions_by_window


def _sector_rank_item(rank: int, row: dict[str, Any]) -> dict[str, Any]:
    """投影首页同 scheme 板块排行项，不携带内部 identity。"""
    return {
        "rank": rank,
        "sectorCode": row["sectorCode"],
        "name": row["name"],
        "changePercent": row["changePercent"],
        "turnoverPercent": row["turnoverPercent"],
        "amountCny": row["amountCny"],
        "leadingEquity": row["leadingEquity"],
        "validSamples": row["validSamples"],
    }


def _rank_equities(
    rows: list[dict[str, Any]], field: str, *, reverse: bool
) -> list[dict[str, Any]]:
    """按单一指标稳定排序股票；空换手不进入该榜，成交额定义“活跃”。"""
    eligible = [row for row in rows if row.get(field) is not None]
    ordered = sorted(
        eligible,
        key=lambda row: (
            -Decimal(str(row[field])) if reverse else Decimal(str(row[field])),
            str(row["tsCode"]),
        ),
    )
    return [
        {
            "rank": rank,
            "exchange": row["exchange"],
            "symbol": row["symbol"],
            "name": row["name"],
            "close": row["close"],
            "changePercent": row["changePercent"],
            "amountCny": row["amountCny"],
            "turnoverPercent": row["turnoverPercent"],
        }
        for rank, row in enumerate(ordered, 1)
    ]


def _rank_flows(rows: list[dict[str, Any]], *, reverse: bool) -> list[dict[str, Any]]:
    """先按净额正负分侧再稳定排序，零值不伪装成流入或流出。"""
    eligible = [
        row
        for row in rows
        if (
            Decimal(str(row["netAmountCny"])) > 0
            if reverse
            else Decimal(str(row["netAmountCny"])) < 0
        )
    ]
    ordered = sorted(
        eligible,
        key=lambda row: (
            -Decimal(str(row["netAmountCny"])) if reverse else Decimal(str(row["netAmountCny"])),
            str(row["tsCode"]),
        ),
    )
    return [
        {
            "rank": rank,
            "exchange": row["exchange"],
            "symbol": row["symbol"],
            "name": row["name"],
            "netAmountCny": row["netAmountCny"],
            "buyLargeAmountCny": row["buyLargeAmountCny"],
            "sellLargeAmountCny": row["sellLargeAmountCny"],
            "changePercent": row["changePercent"],
        }
        for rank, row in enumerate(ordered, 1)
    ]


def _attention_signals(
    *,
    turnover: dict[str, Any],
    breadth: dict[str, int],
    limits: dict[str, Any],
    sector_rankings: dict[str, Any],
    trade_date: date,
) -> list[dict[str, Any]]:
    """按 v1 确定性阈值生成可复算注意信号，不推断新闻因果。"""
    del sector_rankings
    signals: list[dict[str, Any]] = []
    turnover_change = Decimal(str(turnover["changePercent"]))
    if abs(turnover_change) >= 20:
        rule = "turnover-expansion-v1" if turnover_change > 0 else "turnover-contraction-v1"
        signals.append(
            _signal(
                trade_date,
                rule,
                "warning",
                "沪深 A 股成交额较上一交易日显著变化",
                "turnoverChangePercent",
                turnover_change,
                Decimal("20"),
                "percent",
            )
        )
    eligible = Decimal(breadth["advancing"] + breadth["flat"] + breadth["declining"])
    advancing_ratio = (
        Decimal("0") if eligible == 0 else Decimal(breadth["advancing"]) / eligible * 100
    )
    if eligible >= 1000 and (advancing_ratio >= 80 or advancing_ratio <= 20):
        signals.append(
            _signal(
                trade_date,
                "breadth-extreme-v1",
                "warning",
                "市场上涨家数占比处于极端区间",
                "advancingRatio",
                advancing_ratio,
                Decimal("80") if advancing_ratio >= 80 else Decimal("20"),
                "percent",
            )
        )
    up = int(limits["limitUp"])
    down = int(limits["limitDown"])
    if up + down >= 20 and (
        (down == 0 and up > 0) or (up == 0 and down > 0) or up >= down * 3 or down >= up * 3
    ):
        signals.append(
            _signal(
                trade_date,
                "limit-imbalance-v1",
                "warning",
                "涨跌停家数明显失衡",
                "limitImbalance",
                Decimal(max(up, down)),
                Decimal(min(up, down) * 3),
                "count",
            )
        )
    return signals


def _signal(
    trade_date: date,
    rule_id: str,
    severity: str,
    title: str,
    metric: str,
    current: Decimal,
    threshold: Decimal,
    unit: str,
) -> dict[str, Any]:
    """构造稳定 signalId 和结构化证据，标题不携带不可验证原因。"""
    signal_id = hashlib.sha256(f"{trade_date}:{rule_id}".encode()).hexdigest()[:24]
    return {
        "signalId": signal_id,
        "ruleId": rule_id,
        "rulesVersion": "1",
        "severity": severity,
        "title": title,
        "evidence": [
            {
                "metric": metric,
                "currentValue": format(current, "f"),
                "threshold": format(threshold, "f"),
                "unit": unit,
            }
        ],
    }


def _derived_candidate(
    dataset_code: str,
    trade_date: date,
    payload: dict[str, Any],
    observed_at: datetime,
    methodology_id: str,
) -> MarketComponentCandidate:
    """构造具有输入 lineage 声明的内部派生组件。"""
    fingerprint = hashlib.sha256(str(payload["schema"]).encode()).hexdigest()
    partition_key = f"trade-date:{trade_date.isoformat()}"
    return MarketComponentCandidate(
        data_version=_candidate_data_version(dataset_code, partition_key, payload),
        dataset_code=dataset_code,
        partition_key=partition_key,
        trade_date=trade_date,
        payload=payload,
        source={
            "provider": "quant-v2-derivation",
            "upstreamSource": "Tushare canonical inputs",
            "sourceDataset": dataset_code,
            "observedAt": observed_at.isoformat().replace("+00:00", "Z"),
            "adapterVersion": "market-overview-derivation-1",
            "schemaFingerprint": fingerprint,
        },
        methodology={
            "id": methodology_id,
            "version": "1",
            "status": "platform_derived",
        },
        quality={
            "status": "passed",
            "recordCount": len(payload.get("records", [payload])),
            "checks": ["input_publications_fixed", "formula_versioned"],
        },
        observed_at=observed_at,
    )


def _candidate_data_version(
    dataset_code: str,
    partition_key: str,
    payload: dict[str, Any],
) -> UUID:
    """从数据集、分区和规范内容确定组件 UUID，使写时 lineage 可提前冻结。"""
    content_hash = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return uuid5(
        _COMPONENT_NAMESPACE,
        f"{dataset_code}:{partition_key}:{content_hash}",
    )


def _source_from_batch(batch: ProviderBatch, payload: dict[str, Any]) -> dict[str, Any]:
    """投影不含 token/URL 的真实来源绑定，保留 adapter 与上游主体区分。"""
    source = payload["source"]
    return {
        "provider": batch.provider_id,
        "upstreamSource": source["upstreamSource"],
        "sourceDataset": source["sourceDataset"],
        "observedAt": batch.observed_at.isoformat().replace("+00:00", "Z"),
        "adapterVersion": batch.adapter_version,
        "schemaFingerprint": batch.schema_fingerprint
        or hashlib.sha256(str(payload["schema"]).encode()).hexdigest(),
        "normalizedSha256": hashlib.sha256(batch.payload).hexdigest(),
        "requestEvidence": list(payload["source"].get("evidence", [])),
        "licenseScope": payload["source"].get("licenseScope"),
        "licenseReferenceFingerprint": payload["source"].get("licenseReferenceFingerprint"),
    }


def _public_source_binding(source: dict[str, Any]) -> dict[str, Any]:
    """从持久化来源证据投影公开六字段，避免 raw 摘要破坏 API 严格合同。"""
    return {
        key: source[key]
        for key in (
            "provider",
            "upstreamSource",
            "sourceDataset",
            "observedAt",
            "adapterVersion",
            "schemaFingerprint",
        )
    }


def _public_source(payload: dict[str, Any]) -> dict[str, Any]:
    """从来源 envelope 构造公开安全 source；观察时间由 bundle binding 另行提供。"""
    source = payload["source"]
    return {
        "provider": source["provider"],
        "upstreamSource": source["upstreamSource"],
        "sourceDataset": source["sourceDataset"],
        "observedAt": source["observedAt"],
        "adapterVersion": source["adapterVersion"],
        "schemaFingerprint": source["schemaFingerprint"],
    }


def _methodology(payload: dict[str, Any]) -> dict[str, Any]:
    """保留来源方法学身份；未知版本保持 unknown，不伪装为统一事实。"""
    return {
        "semanticFamily": payload.get("semanticFamily", "reported_market_data"),
        "id": payload.get("methodologyId", str(payload["schema"])),
        "version": payload.get("methodologyVersion", "1"),
        "status": payload.get("methodologyStatus", "source_reported"),
    }


def _eligible_on(row: dict[str, Any], target: date) -> bool:
    """按来源明确上市/退市区间判断当日 A 股样本池，不用行情缺席推生命周期。"""
    if row.get("listStatus") == "P":
        return False
    list_date = row.get("listDate")
    if list_date is None or date.fromisoformat(str(list_date)) > target:
        return False
    delist_date = row.get("delistDate")
    return delist_date is None or date.fromisoformat(str(delist_date)) >= target


def _membership_effective(row: dict[str, Any], target: date) -> bool:
    """按正式 `[inDate,outDate)` 区间判断申万成分，不使用 `isNew` 猜历史状态。"""
    in_date = row.get("inDate")
    if in_date is None:
        raise _schema_error("SW membership inDate is required")
    start = date.fromisoformat(str(in_date))
    out_date = row.get("outDate")
    end = None if out_date is None else date.fromisoformat(str(out_date))
    return start <= target and (end is None or target < end)


def _validate_ohlc(row: dict[str, Any]) -> None:
    """验证 OHLC 基本关系和非负价格，阻断字段错位。"""
    open_value = Decimal(str(row["open"]))
    high = Decimal(str(row["high"]))
    low = Decimal(str(row["low"]))
    close = Decimal(str(row["close"]))
    if (
        min(open_value, high, low, close) < 0
        or high < max(open_value, close)
        or low > min(open_value, close)
    ):
        raise _schema_error("OHLC relationship is invalid")


def _compound_percent(values: list[Decimal]) -> Decimal:
    """按日收益复合计算窗口累计收益，不直接相加百分比。"""
    product = Decimal("1")
    for value in values:
        product *= Decimal("1") + value / Decimal("100")
    return (product - Decimal("1")) * Decimal("100")


def _universe_version(codes: frozenset[str], trade_date: date) -> str:
    """计算可审计样本池版本，不暴露内部 UUID。"""
    payload = f"{trade_date}:{','.join(sorted(codes))}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _schema_error(message: str) -> ProviderError:
    """把完整性、schema 或方法学阻断转成不可重试 provider-neutral 失败。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)


__all__ = ["MarketOverviewSyncResult", "MarketOverviewSyncService"]
