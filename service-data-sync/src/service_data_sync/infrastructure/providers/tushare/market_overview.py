"""Tushare Pro 市场概览与行业板块 provider adapter。

供应商 API 名、HTTP 地址、字段名、单位和权限错误只允许存在于本模块。输出载荷采用
provider-neutral schema；应用层仍会独立执行跨组件完整性和方法学质量门。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from contextvars import ContextVar
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)

_API_URL = "https://api.tushare.pro"
_PROVIDER_ID = "tushare-pro"
_ADAPTER_VERSION = "market-overview-1"
_CONTENT_TYPE = "application/json"
_REQUEST_EVIDENCE: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "tushare_market_request_evidence",
    default=None,
)
_INDEX_IDENTITIES = {
    "000001.SH": ("sse-composite", "上证指数"),
    "399001.SZ": ("szse-component", "深证成指"),
    "000300.SH": ("csi-300", "沪深300"),
    "399006.SZ": ("chinext", "创业板指"),
}
_API_ROW_LIMITS = {
    "daily": 6000,
    "daily_basic": 6000,
    "moneyflow": 6000,
    "moneyflow_ind_dc": 5000,
    "stock_basic": 6000,
    "stk_limit": 5800,
    "dc_daily": 2000,
    "dc_member": 5000,
    "index_member_all": 5000,
    "sw_daily": 4000,
    "daily_info": 4000,
}
_CAPABILITIES = frozenset(
    {
        "market.source.preflight",
        "market.calendar",
        "index.bar.1d",
        "equity.catalog",
        "equity.quote.eod",
        "equity.daily-basic.eod",
        "equity.suspension.eod",
        "equity.limit-price.eod",
        "market.turnover.qa.reported",
        "money-flow.market.dc.eod",
        "money-flow.equity.order-size.eod",
        "sector.catalog.dc",
        "sector.quote.eod.dc",
        "sector.membership.dc",
        "sector.money-flow.dc.eod",
        "sw.taxonomy",
        "sw.membership",
        "sw.market-data",
    }
)


class TushareMarketOverviewAdapter(DataSourcePort):
    """通过 Tushare Pro HTTP 协议提供完整市场概览所需的中立批次。"""

    def __init__(
        self,
        *,
        token: str,
        timeout_seconds: int,
        response_row_limit: int,
        license_scope: str,
        license_reference: str = "",
        minimum_entitlement_points: int = 6000,
        max_requests_per_minute: int = 180,
        max_retries: int = 3,
        retry_base_seconds: float = 1.0,
        transport: Callable[[bytes, int], bytes] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """保存秘密、超时、共享节流和重试门；token 永不进入错误或日志。"""
        normalized_token = token.strip()
        if not normalized_token:
            raise ValueError("DATA_SYNC_TUSHARE_TOKEN is required")
        if timeout_seconds < 1:
            raise ValueError("Tushare timeout must be positive")
        if response_row_limit < 100:
            raise ValueError("Tushare response row limit is too small")
        if minimum_entitlement_points < 6000:
            raise ValueError("Tushare market overview requires at least 6000 entitlement points")
        if license_scope not in {
            "personal-research",
            "commercial-redistribution-approved",
        }:
            raise ValueError("Tushare market data license scope is not approved")
        if license_scope == "commercial-redistribution-approved" and not license_reference.strip():
            raise ValueError("commercial market data license reference is required")
        if not 1 <= max_requests_per_minute <= 500:
            raise ValueError("Tushare request rate must be between 1 and 500 per minute")
        if not 0 <= max_retries <= 5:
            raise ValueError("Tushare max retries must be between 0 and 5")
        if retry_base_seconds <= 0:
            raise ValueError("Tushare retry base seconds must be positive")
        self._token = normalized_token
        self._timeout_seconds = timeout_seconds
        self._response_row_limit = response_row_limit
        self._minimum_entitlement_points = minimum_entitlement_points
        self._license_scope = license_scope
        self._license_reference_fingerprint = (
            None
            if not license_reference.strip()
            else hashlib.sha256(license_reference.strip().encode()).hexdigest()
        )
        self._max_requests_per_minute = max_requests_per_minute
        self._max_retries = max_retries
        self._retry_base_seconds = retry_base_seconds
        self._transport = transport or self._http_post
        self._sleep = sleep
        self._rate_lock = asyncio.Lock()
        self._request_times: deque[float] = deque()

    @property
    def provider_id(self) -> str:
        """返回不会泄漏账户身份的稳定 adapter 编码。"""
        return _PROVIDER_ID

    def capabilities(self) -> frozenset[str]:
        """返回已实现且不会静默降级到 AKShare 的能力集合。"""
        return _CAPABILITIES

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """按中立 capability 获取并规范化批次；未知能力在发网请求前拒绝。"""
        if request.capability not in _CAPABILITIES:
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "unsupported Tushare market capability",
                retryable=False,
            )
        parameters = dict(request.parameters)
        observed_at = datetime.now(UTC)
        evidence_token = _REQUEST_EVIDENCE.set([])
        try:
            payload = await self._dispatch(request.capability, parameters)
        except ProviderError as error:
            _attach_failure_evidence(
                error,
                provider_id=self.provider_id,
                capability=request.capability,
                evidence=_REQUEST_EVIDENCE.get() or [],
                observed_at=observed_at,
            )
            raise
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            normalized_error = ProviderError(
                ProviderErrorCode.SCHEMA,
                "Tushare response cannot be normalized",
                retryable=False,
            )
            _attach_failure_evidence(
                normalized_error,
                provider_id=self.provider_id,
                capability=request.capability,
                evidence=_REQUEST_EVIDENCE.get() or [],
                observed_at=observed_at,
            )
            raise normalized_error from error
        finally:
            request_evidence = _REQUEST_EVIDENCE.get() or []
            _REQUEST_EVIDENCE.reset(evidence_token)
        payload["source"]["observedAt"] = observed_at.isoformat().replace("+00:00", "Z")
        payload["source"]["schemaFingerprint"] = hashlib.sha256(
            str(payload["schema"]).encode()
        ).hexdigest()
        payload["source"]["evidence"] = request_evidence
        payload["source"]["licenseScope"] = self._license_scope
        payload["source"]["licenseReferenceFingerprint"] = self._license_reference_fingerprint
        encoded = _canonical_json(payload)
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=encoded,
            observed_at=observed_at,
            content_type=_CONTENT_TYPE,
            upstream_source=str(payload["source"]["upstreamSource"]),
            adapter_version=_ADAPTER_VERSION,
            schema_fingerprint=hashlib.sha256(str(payload["schema"]).encode()).hexdigest(),
        )

    async def _dispatch(self, capability: str, parameters: dict[str, str]) -> dict[str, Any]:
        """把中立能力路由到一个封闭供应商映射函数。"""
        target = _required_date(parameters, "tradeDate") if "tradeDate" in parameters else None
        if capability == "market.source.preflight":
            if target is None:
                raise ValueError("tradeDate is required")
            return await self._preflight(target)
        if capability == "market.calendar":
            return await self._calendar(parameters)
        if capability == "index.bar.1d":
            return await self._index_bars(parameters)
        if capability == "equity.catalog":
            return await self._equity_catalog()
        if capability == "equity.quote.eod":
            return await self._equity_quotes(_required_target(target))
        if capability == "equity.daily-basic.eod":
            return await self._equity_daily_basic(_required_target(target))
        if capability == "equity.suspension.eod":
            return await self._equity_suspensions(_required_target(target))
        if capability == "equity.limit-price.eod":
            return await self._equity_limits(_required_target(target), parameters.get("codes", ""))
        if capability == "market.turnover.qa.reported":
            return await self._market_turnover_qa(_required_target(target))
        if capability == "money-flow.market.dc.eod":
            return await self._market_money_flow(_required_target(target))
        if capability == "money-flow.equity.order-size.eod":
            return await self._equity_money_flow(_required_target(target))
        if capability == "sector.catalog.dc":
            return await self._sector_catalog(_required_target(target))
        if capability == "sector.quote.eod.dc":
            return await self._sector_quotes(_required_target(target))
        if capability == "sector.membership.dc":
            return await self._sector_memberships(
                _required_target(target),
                parameters.get("sectors", ""),
            )
        if capability == "sector.money-flow.dc.eod":
            return await self._sector_money_flow(_required_target(target))
        if capability == "sw.taxonomy":
            return await self._sw_taxonomy()
        if capability == "sw.membership":
            return await self._sw_memberships(
                _required_target(target),
                parameters.get("levelThreeCodes", ""),
            )
        return await self._sw_market_data(_required_target(target))

    async def _preflight(self, target: date) -> dict[str, Any]:
        """真实探测 token、必需端点权限、schema、行上限和目标交易日新鲜度。"""
        trade_date = _compact_date(target)
        probes: list[dict[str, Any]] = []

        async def probe(
            api_name: str,
            params: dict[str, object],
            fields: tuple[str, ...],
            *,
            allow_empty: bool,
        ) -> tuple[dict[str, Any], ...]:
            """执行一个真实端点探针并记录不含秘密的权限与 schema 证据。"""
            rows = await self._query(
                api_name,
                params=params,
                fields=fields,
                allow_empty=allow_empty,
                reject_limit=True,
            )
            probes.append(
                {
                    "endpoint": api_name,
                    "status": "passed",
                    "rowCount": len(rows),
                    "schema": list(fields),
                }
            )
            return rows

        calendar = await probe(
            "trade_cal",
            {"exchange": "SSE", "start_date": trade_date, "end_date": trade_date},
            ("exchange", "cal_date", "is_open", "pretrade_date"),
            allow_empty=False,
        )
        if str(calendar[0]["cal_date"]) != trade_date:
            raise _schema_error("Tushare trade calendar is stale for requested date")
        if int(calendar[0]["is_open"]) != 1:
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "market overview target is not an open trading day",
                retryable=False,
            )
        daily = await probe(
            "daily",
            {"trade_date": trade_date},
            (
                "ts_code",
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "pre_close",
                "change",
                "pct_chg",
                "vol",
                "amount",
            ),
            allow_empty=False,
        )
        if len(daily) < 1000:
            raise _schema_error("Tushare daily cross-section is incomplete")
        representative_code = str(daily[0]["ts_code"])
        listed = await probe(
            "stock_basic",
            {"exchange": "", "list_status": "L"},
            (
                "ts_code",
                "symbol",
                "name",
                "area",
                "industry",
                "market",
                "exchange",
                "list_status",
                "list_date",
                "delist_date",
            ),
            allow_empty=False,
        )
        if any(str(row.get("list_status")) != "L" for row in listed):
            raise _schema_error("Tushare stock_basic listed partition drifted")
        await probe(
            "daily_basic",
            {"trade_date": trade_date},
            (
                "ts_code",
                "trade_date",
                "turnover_rate",
                "total_mv",
                "circ_mv",
                "limit_status",
            ),
            allow_empty=False,
        )
        await probe(
            "suspend_d",
            {"trade_date": trade_date, "suspend_type": "S"},
            ("ts_code", "trade_date", "suspend_timing", "suspend_type"),
            allow_empty=True,
        )
        await probe(
            "stk_limit",
            {"trade_date": trade_date, "ts_code": representative_code},
            ("ts_code", "trade_date", "pre_close", "up_limit", "down_limit"),
            allow_empty=False,
        )
        for market_code in ("SH_A", "SZ_A"):
            daily_info = await probe(
                "daily_info",
                {"trade_date": trade_date, "ts_code": market_code},
                ("trade_date", "ts_code", "ts_name", "com_count", "amount", "exchange"),
                allow_empty=False,
            )
            if (
                len(daily_info) != 1
                or str(daily_info[0]["ts_code"]) != market_code
                or str(daily_info[0]["trade_date"]) != trade_date
            ):
                raise _schema_error("Tushare daily_info A-share partition is incomplete")
        await probe(
            "index_daily",
            {
                "ts_code": "000001.SH",
                "start_date": trade_date,
                "end_date": trade_date,
            },
            (
                "ts_code",
                "trade_date",
                "close",
                "open",
                "high",
                "low",
                "pre_close",
                "change",
                "pct_chg",
                "vol",
                "amount",
            ),
            allow_empty=False,
        )
        await probe(
            "moneyflow",
            {"trade_date": trade_date},
            (
                "ts_code",
                "trade_date",
                "buy_lg_amount",
                "sell_lg_amount",
                "buy_elg_amount",
                "sell_elg_amount",
                "net_mf_amount",
            ),
            allow_empty=False,
        )
        await probe(
            "moneyflow_mkt_dc",
            {"trade_date": trade_date},
            ("trade_date", "close_sh", "pct_change_sh", "net_amount"),
            allow_empty=False,
        )
        for content_type in ("行业", "概念"):
            flow_rows = await probe(
                "moneyflow_ind_dc",
                {"trade_date": trade_date, "content_type": content_type},
                (
                    "trade_date",
                    "content_type",
                    "ts_code",
                    "name",
                    "pct_change",
                    "close",
                    "net_amount",
                ),
                allow_empty=False,
            )
            expected_scheme = (
                "eastmoney.industry" if content_type == "行业" else "eastmoney.concept"
            )
            if any(_dc_scheme(row.get("content_type")) != expected_scheme for row in flow_rows):
                raise _schema_error("Tushare moneyflow_ind_dc content_type partition drifted")
        catalog: list[dict[str, Any]] = []
        for sector_type in ("行业板块", "概念板块"):
            catalog.extend(
                await probe(
                    "dc_index",
                    {"trade_date": trade_date, "idx_type": sector_type},
                    (
                        "ts_code",
                        "trade_date",
                        "name",
                        "pct_change",
                        "total_mv",
                        "turnover_rate",
                        "up_num",
                        "down_num",
                        "idx_type",
                        "level",
                    ),
                    allow_empty=False,
                )
            )
        await probe(
            "dc_daily",
            {"trade_date": trade_date},
            (
                "ts_code",
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "change",
                "pct_change",
                "vol",
                "amount",
                "swing",
                "turnover_rate",
            ),
            allow_empty=False,
        )
        representative_sectors: list[str] = []
        for scheme_label in ("行业", "概念"):
            representative = next(
                (row for row in catalog if scheme_label in str(row.get("idx_type") or "")),
                None,
            )
            if representative is None:
                raise _schema_error(f"Tushare dc_index has no {scheme_label} sector")
            representative_sectors.append(str(representative["ts_code"]))
        for sector_code in representative_sectors:
            await probe(
                "dc_member",
                {"ts_code": sector_code, "trade_date": trade_date},
                ("ts_code", "trade_date", "con_code", "name"),
                allow_empty=False,
            )
        taxonomy = await probe(
            "index_classify",
            {"src": "SW2021"},
            ("index_code", "industry_name", "level", "parent_code", "src"),
            allow_empty=False,
        )
        representative_l3 = next(
            (row for row in taxonomy if str(row.get("level") or "").strip().upper() in {"L3", "3"}),
            None,
        )
        if representative_l3 is None:
            raise _schema_error("Tushare SW taxonomy has no L3 node")
        for is_new in ("Y", "N"):
            await probe(
                "index_member_all",
                {
                    "l3_code": str(representative_l3["index_code"]),
                    "is_new": is_new,
                },
                (
                    "l1_code",
                    "l1_name",
                    "l2_code",
                    "l2_name",
                    "l3_code",
                    "l3_name",
                    "ts_code",
                    "name",
                    "in_date",
                    "out_date",
                    "is_new",
                ),
                allow_empty=True,
            )
        await probe(
            "sw_daily",
            {"trade_date": trade_date},
            (
                "ts_code",
                "trade_date",
                "name",
                "open",
                "high",
                "low",
                "close",
                "change",
                "pct_change",
                "vol",
                "amount",
                "pe",
                "pb",
                "float_mv",
                "total_mv",
            ),
            allow_empty=False,
        )
        return _envelope(
            schema="quant-v2.market-source-preflight.v1",
            source_dataset="tushare-pro-required-endpoints",
            upstream_source="Tushare",
            records=probes,
            extra={
                "tradeDate": target.isoformat(),
                "minimumEntitlementPoints": self._minimum_entitlement_points,
                "entitlementVerifiedByEndpointAccess": True,
                "rowLimit": self._response_row_limit,
                "freshness": "current",
                "licenseScope": self._license_scope,
                "licenseReferenceFingerprint": self._license_reference_fingerprint,
            },
        )

    async def _calendar(self, parameters: dict[str, str]) -> dict[str, Any]:
        """读取沪深交易日历并保留场所，不用周末规则猜开闭市。"""
        start = _required_date(parameters, "start")
        end = _required_date(parameters, "end")
        records: list[dict[str, Any]] = []
        for venue in ("SSE", "SZSE"):
            rows = await self._query(
                "trade_cal",
                params={
                    "exchange": venue,
                    "start_date": _compact_date(start),
                    "end_date": _compact_date(end),
                },
                fields=("exchange", "cal_date", "is_open", "pretrade_date"),
                allow_empty=False,
            )
            records.extend(
                {
                    "venue": venue,
                    "tradeDate": _iso_date(row["cal_date"]),
                    "isTradingDay": int(row["is_open"]) == 1,
                    "previousTradingDate": _optional_iso_date(row.get("pretrade_date")),
                    "sessions": (
                        [
                            {"name": "morning", "start": "09:30:00", "end": "11:30:00"},
                            {"name": "afternoon", "start": "13:00:00", "end": "15:00:00"},
                        ]
                        if int(row["is_open"]) == 1
                        else []
                    ),
                }
                for row in rows
            )
        return _envelope(
            schema="quant-v2.market-calendar.v1",
            source_dataset="trade_cal",
            upstream_source="SSE/SZSE via Tushare",
            records=records,
            extra={"timezone": "Asia/Shanghai", "sessionScheduleVersion": "cn-a-cash-2026-v1"},
        )

    async def _index_bars(self, parameters: dict[str, str]) -> dict[str, Any]:
        """读取四个固定指数的真实日线；不接触成分或权重数据。"""
        start = _required_date(parameters, "start")
        end = _required_date(parameters, "end")
        records: list[dict[str, Any]] = []
        for ts_code, (index_id, name) in _INDEX_IDENTITIES.items():
            rows = await self._query(
                "index_daily",
                params={
                    "ts_code": ts_code,
                    "start_date": _compact_date(start),
                    "end_date": _compact_date(end),
                },
                fields=(
                    "ts_code",
                    "trade_date",
                    "close",
                    "open",
                    "high",
                    "low",
                    "pre_close",
                    "change",
                    "pct_chg",
                    "vol",
                    "amount",
                ),
                allow_empty=False,
            )
            records.extend(
                {
                    "indexId": index_id,
                    "indexCode": ts_code,
                    "name": name,
                    "tradeDate": _iso_date(row["trade_date"]),
                    "open": _decimal(row["open"]),
                    "high": _decimal(row["high"]),
                    "low": _decimal(row["low"]),
                    "close": _decimal(row["close"]),
                    "previousClose": _decimal(row["pre_close"]),
                    "change": _decimal(row["change"]),
                    "changePercent": _decimal(row["pct_chg"]),
                    "volume": _optional_decimal(row.get("vol")),
                    # Tushare 指数日线 amount 原始单位为千元，canonical 统一精确 CNY。
                    "amountCny": _scaled_decimal(row.get("amount"), Decimal("1000")),
                    "finality": "final",
                }
                for row in rows
            )
        return _envelope(
            schema="quant-v2.index-bar-1d.v1",
            source_dataset="index_daily",
            upstream_source="SSE/SZSE/CSI via Tushare",
            records=records,
            extra={
                "period": "1d",
                "currency": "CNY",
                "volumeUnit": "lot",
                "amountRawUnit": "thousand_CNY",
                "amountUnit": "CNY",
            },
        )

    async def _equity_catalog(self) -> dict[str, Any]:
        """读取 A 股身份、名称及上市区间，为横截面覆盖率提供权威样本池输入。"""
        records: list[dict[str, Any]] = []
        for status in ("L", "D", "P"):
            rows = await self._query(
                "stock_basic",
                params={"exchange": "", "list_status": status},
                fields=(
                    "ts_code",
                    "symbol",
                    "name",
                    "area",
                    "industry",
                    "market",
                    "exchange",
                    "list_status",
                    "list_date",
                    "delist_date",
                ),
                allow_empty=(status != "L"),
                reject_limit=True,
            )
            records.extend(
                {
                    "tsCode": str(row["ts_code"]),
                    "symbol": str(row["symbol"]),
                    "name": str(row["name"]),
                    "exchange": _exchange(str(row["ts_code"])),
                    "market": _optional_text(row.get("market")),
                    "listStatus": str(row["list_status"]),
                    "listDate": _optional_iso_date(row.get("list_date")),
                    "delistDate": _optional_iso_date(row.get("delist_date")),
                }
                for row in rows
                if _is_a_share_code(str(row["ts_code"]))
            )
        return _envelope(
            schema="quant-v2.equity-catalog.v1",
            source_dataset="stock_basic",
            upstream_source="Tushare",
            records=_unique_records(records, "tsCode"),
        )

    async def _equity_quotes(self, target: date) -> dict[str, Any]:
        """读取单交易日完整股票行情并把成交额从千元转换为 CNY。"""
        rows = await self._query(
            "daily",
            params={"trade_date": _compact_date(target)},
            fields=(
                "ts_code",
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "pre_close",
                "change",
                "pct_chg",
                "vol",
                "amount",
            ),
            allow_empty=False,
            reject_limit=True,
        )
        records = [
            {
                "tsCode": str(row["ts_code"]),
                "exchange": _exchange(str(row["ts_code"])),
                "symbol": str(row["ts_code"]).split(".", 1)[0],
                "tradeDate": _iso_date(row["trade_date"]),
                "open": _decimal(row["open"]),
                "high": _decimal(row["high"]),
                "low": _decimal(row["low"]),
                "close": _decimal(row["close"]),
                "previousClose": _decimal(row["pre_close"]),
                "change": _decimal(row["change"]),
                "changePercent": _decimal(row["pct_chg"]),
                # Tushare 股票日线 vol 原始单位为手；保留原单位，不伪装成股数。
                "volumeLots": _optional_decimal(row.get("vol")),
                "amountCny": _scaled_decimal(row.get("amount"), Decimal("1000")),
            }
            for row in rows
            if _is_a_share_code(str(row["ts_code"]))
        ]
        return _envelope(
            schema="quant-v2.equity-quote-eod.v1",
            source_dataset="daily",
            upstream_source="Tushare",
            records=records,
            extra={"tradeDate": target.isoformat(), "amountRawUnit": "thousand_CNY"},
        )

    async def _equity_daily_basic(self, target: date) -> dict[str, Any]:
        """读取换手、市值和 source-reported 涨跌停状态，空值保持空。"""
        rows = await self._query(
            "daily_basic",
            params={"trade_date": _compact_date(target)},
            fields=(
                "ts_code",
                "trade_date",
                "turnover_rate",
                "total_mv",
                "circ_mv",
                "limit_status",
            ),
            allow_empty=False,
            reject_limit=True,
        )
        records = [
            {
                "tsCode": str(row["ts_code"]),
                "tradeDate": _iso_date(row["trade_date"]),
                "turnoverPercent": _optional_decimal(row.get("turnover_rate")),
                # daily_basic 市值原始单位为万元，canonical 统一 CNY。
                "totalMarketValueCny": _scaled_decimal(row.get("total_mv"), Decimal("10000")),
                "circulatingMarketValueCny": _scaled_decimal(row.get("circ_mv"), Decimal("10000")),
                "limitStatus": _optional_int(row.get("limit_status")),
            }
            for row in rows
            if _is_a_share_code(str(row["ts_code"]))
        ]
        return _envelope(
            schema="quant-v2.equity-daily-basic-eod.v1",
            source_dataset="daily_basic",
            upstream_source="Tushare",
            records=records,
            extra={
                "tradeDate": target.isoformat(),
                "marketValueRawUnit": "ten_thousand_CNY",
                "limitStatusMethod": "source_reported",
            },
        )

    async def _equity_suspensions(self, target: date) -> dict[str, Any]:
        """读取当日停牌事件；不把无行情行自行解释为停牌。"""
        rows = await self._query(
            "suspend_d",
            params={"trade_date": _compact_date(target), "suspend_type": "S"},
            fields=("ts_code", "trade_date", "suspend_timing", "suspend_type"),
            allow_empty=True,
            reject_limit=True,
        )
        records = [
            {
                "tsCode": str(row["ts_code"]),
                "tradeDate": _iso_date(row["trade_date"]),
                "suspendType": _optional_text(row.get("suspend_type")),
                "suspendTiming": _optional_text(row.get("suspend_timing")),
            }
            for row in rows
            if _is_a_share_code(str(row["ts_code"]))
        ]
        return _envelope(
            schema="quant-v2.equity-suspension-eod.v1",
            source_dataset="suspend_d",
            upstream_source="Tushare",
            records=records,
            extra={"tradeDate": target.isoformat()},
        )

    async def _equity_limits(self, target: date, codes_value: str) -> dict[str, Any]:
        """只逐证券补取 source-reported 涨跌停证券限价，规避 5800 全市场截断。"""
        codes = tuple(code for code in codes_value.split(",") if code)
        if len(codes) > 1000:
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "too many limit-price verification codes",
                retryable=False,
            )
        records: list[dict[str, Any]] = []
        for code in codes:
            rows = await self._query(
                "stk_limit",
                params={"trade_date": _compact_date(target), "ts_code": code},
                fields=("ts_code", "trade_date", "pre_close", "up_limit", "down_limit"),
                allow_empty=False,
            )
            if len(rows) != 1:
                raise _schema_error("Tushare stk_limit did not return exactly one verification row")
            row = rows[0]
            records.append(
                {
                    "tsCode": str(row["ts_code"]),
                    "tradeDate": _iso_date(row["trade_date"]),
                    "previousClose": _decimal(row["pre_close"]),
                    "upLimit": _decimal(row["up_limit"]),
                    "downLimit": _decimal(row["down_limit"]),
                }
            )
        return _envelope(
            schema="quant-v2.equity-limit-price-eod.v1",
            source_dataset="stk_limit",
            upstream_source="Tushare",
            records=records,
            extra={
                "tradeDate": target.isoformat(),
                "fetchStrategy": "per_limit_security",
                "fullMarketEndpointLimit": 5800,
            },
        )

    async def _market_money_flow(self, target: date) -> dict[str, Any]:
        """读取东财大盘资金流，保留供应商方法学身份而不称为统一市场事实。"""
        rows = await self._query(
            "moneyflow_mkt_dc",
            params={"trade_date": _compact_date(target)},
            fields=(
                "trade_date",
                "close_sh",
                "pct_change_sh",
                "close_sz",
                "pct_change_sz",
                "net_amount",
            ),
            allow_empty=False,
        )
        records = [
            {
                "tradeDate": _iso_date(row["trade_date"]),
                "sseClose": _optional_decimal(row.get("close_sh")),
                "sseChangePercent": _optional_decimal(row.get("pct_change_sh")),
                "szseClose": _optional_decimal(row.get("close_sz")),
                "szseChangePercent": _optional_decimal(row.get("pct_change_sz")),
                "netAmountCny": _optional_decimal(row.get("net_amount")),
            }
            for row in rows
        ]
        return _envelope(
            schema="quant-v2.money-flow-market-dc-eod.v1",
            source_dataset="moneyflow_mkt_dc",
            upstream_source="Eastmoney via Tushare",
            records=records,
            extra={
                "tradeDate": target.isoformat(),
                "semanticFamily": "trade_direction_flow",
                "methodologyId": "eastmoney-market-flow-dc",
                "methodologyVersion": "unknown",
                "methodologyStatus": "source_reported",
                "rawUnit": "CNY",
            },
        )

    async def _market_turnover_qa(self, target: date) -> dict[str, Any]:
        """读取交易所 SH_A/SZ_A 日统计，作为 A 股横截面成交额独立 QA 对照。"""
        records: list[dict[str, Any]] = []
        for market_code in ("SH_A", "SZ_A"):
            rows = await self._query(
                "daily_info",
                params={"trade_date": _compact_date(target), "ts_code": market_code},
                fields=("trade_date", "ts_code", "ts_name", "com_count", "amount", "exchange"),
                allow_empty=False,
            )
            if len(rows) != 1:
                raise _schema_error("Tushare daily_info A-share partition is incomplete")
            row = rows[0]
            records.append(
                {
                    "venue": "SSE" if market_code == "SH_A" else "SZSE",
                    "marketCode": market_code,
                    "marketName": str(row["ts_name"]),
                    "tradeDate": _iso_date(row["trade_date"]),
                    "listedCount": int(row["com_count"]),
                    # daily_info amount 原始单位为亿元，canonical 统一 CNY。
                    "amountCny": _scaled_decimal(row["amount"], Decimal("100000000")),
                }
            )
        return _envelope(
            schema="quant-v2.market-turnover-qa-reported.v1",
            source_dataset="daily_info",
            upstream_source="SSE/SZSE via Tushare",
            records=records,
            extra={
                "tradeDate": target.isoformat(),
                "universe": "CN-A-SSE-SZSE",
                "amountRawUnit": "hundred_million_CNY",
                "methodology": "exchange_reported_market_statistics",
            },
        )

    async def _equity_money_flow(self, target: date) -> dict[str, Any]:
        """读取沪深股票订单规模资金流；不同分桶和值不与 DC 大盘流合并。"""
        rows = await self._query(
            "moneyflow",
            params={"trade_date": _compact_date(target)},
            fields=(
                "ts_code",
                "trade_date",
                "buy_lg_amount",
                "sell_lg_amount",
                "buy_elg_amount",
                "sell_elg_amount",
                "net_mf_amount",
            ),
            allow_empty=False,
            reject_limit=True,
        )
        records = [
            {
                "tsCode": str(row["ts_code"]),
                "exchange": _exchange(str(row["ts_code"])),
                "symbol": str(row["ts_code"]).split(".", 1)[0],
                "tradeDate": _iso_date(row["trade_date"]),
                # moneyflow 金额原始单位为万元，所有分桶使用同一倍率后才能比较。
                "buyLargeAmountCny": _scaled_sum(
                    row.get("buy_lg_amount"), row.get("buy_elg_amount"), Decimal("10000")
                ),
                "sellLargeAmountCny": _scaled_sum(
                    row.get("sell_lg_amount"), row.get("sell_elg_amount"), Decimal("10000")
                ),
                "netAmountCny": _scaled_decimal(row.get("net_mf_amount"), Decimal("10000")),
            }
            for row in rows
            if str(row["ts_code"]).endswith((".SH", ".SZ"))
            and _is_a_share_code(str(row["ts_code"]))
        ]
        return _envelope(
            schema="quant-v2.money-flow-equity-order-size-eod.v1",
            source_dataset="moneyflow",
            upstream_source="Tushare",
            records=records,
            extra={
                "tradeDate": target.isoformat(),
                "semanticFamily": "order_size_flow",
                "methodologyId": "tushare-order-size-flow",
                "methodologyVersion": "1",
                "methodologyStatus": "source_reported",
                "rawUnit": "ten_thousand_CNY",
            },
        )

    async def _sector_catalog(self, target: date) -> dict[str, Any]:
        """按交易日分别读取东财行业与概念横截面，排除地域等其他目录。"""
        rows: list[dict[str, Any]] = []
        for sector_type in ("行业板块", "概念板块"):
            rows.extend(
                await self._query(
                    "dc_index",
                    params={
                        "trade_date": _compact_date(target),
                        "idx_type": sector_type,
                    },
                    fields=(
                        "ts_code",
                        "trade_date",
                        "name",
                        "pct_change",
                        "total_mv",
                        "turnover_rate",
                        "up_num",
                        "down_num",
                        "idx_type",
                        "level",
                    ),
                    allow_empty=False,
                    reject_limit=True,
                )
            )
        records = [
            {
                "scheme": _dc_scheme(row.get("idx_type")),
                "sectorCode": str(row["ts_code"]),
                "name": str(row["name"]),
                "publisher": "Eastmoney",
                "tradeDate": _iso_date(row["trade_date"]),
                "changePercent": _optional_decimal(row.get("pct_change")),
                "level": _optional_text(row.get("level")),
                # dc_index 总市值原始单位为万元；目录快照值不与 dc_daily 成交额混用。
                "totalMarketValueCny": _scaled_decimal(row.get("total_mv"), Decimal("10000")),
                "turnoverPercent": _optional_decimal(row.get("turnover_rate")),
                "advancing": _optional_int(row.get("up_num")),
                "declining": _optional_int(row.get("down_num")),
            }
            for row in rows
            if _dc_scheme(row.get("idx_type")) is not None
        ]
        return _envelope(
            schema="quant-v2.sector-catalog-dc.v1",
            source_dataset="dc_index",
            upstream_source="Eastmoney via Tushare",
            records=_unique_records(records, "sectorCode"),
            extra={
                "tradeDate": target.isoformat(),
                "marketValueRawUnit": "ten_thousand_CNY",
                "schemes": ["eastmoney.industry", "eastmoney.concept"],
            },
        )

    async def _sector_quotes(self, target: date) -> dict[str, Any]:
        """读取东财板块日线原子横截面；目录身份由应用层冻结 publication 关联。"""
        rows = await self._query(
            "dc_daily",
            params={"trade_date": _compact_date(target)},
            fields=(
                "ts_code",
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "change",
                "pct_change",
                "vol",
                "amount",
                "swing",
                "turnover_rate",
            ),
            allow_empty=False,
            reject_limit=True,
        )
        records = [
            {
                "sectorCode": str(row["ts_code"]),
                "tradeDate": _iso_date(row["trade_date"]),
                "open": _decimal(row["open"]),
                "high": _decimal(row["high"]),
                "low": _decimal(row["low"]),
                "close": _decimal(row["close"]),
                "change": _decimal(row["change"]),
                "previousClose": _decimal_difference(row["close"], row["change"]),
                "changePercent": _decimal(row["pct_change"]),
                "volume": _optional_decimal(row.get("vol")),
                "turnoverPercent": _optional_decimal(row.get("turnover_rate")),
                "amountCny": _optional_decimal(row.get("amount")),
                "swingPercent": _optional_decimal(row.get("swing")),
            }
            for row in rows
        ]
        return _envelope(
            schema="quant-v2.sector-quote-eod-dc.v1",
            source_dataset="dc_daily",
            upstream_source="Eastmoney via Tushare",
            records=records,
            extra={
                "tradeDate": target.isoformat(),
                "amountRawUnit": "CNY",
                "previousCloseMethodology": "same_row_close_minus_change",
            },
        )

    async def _sector_memberships(
        self,
        target: date,
        sectors_value: str,
    ) -> dict[str, Any]:
        """逐板块读取完整当前成分；每个查询达到行上限即阻断，绝不发布截断关系。"""
        catalog = _canonical_object_list(
            sectors_value,
            required_keys=("scheme", "sectorCode", "name"),
            maximum=2000,
        )
        records: list[dict[str, Any]] = []
        for sector in catalog:
            rows = await self._query(
                "dc_member",
                params={
                    "ts_code": sector["sectorCode"],
                    "trade_date": _compact_date(target),
                },
                fields=("ts_code", "trade_date", "con_code", "name"),
                allow_empty=False,
                reject_limit=True,
            )
            records.extend(
                {
                    "scheme": sector["scheme"],
                    "sectorCode": str(row["ts_code"]),
                    "tsCode": str(row["con_code"]),
                    "name": str(row["name"]),
                    "observationDate": _iso_date(row["trade_date"]),
                }
                for row in rows
                if _is_a_share_code(str(row["con_code"]))
                and _iso_date(row["trade_date"]) == target.isoformat()
            )
        return _envelope(
            schema="quant-v2.sector-membership-dc.v1",
            source_dataset="dc_member",
            upstream_source="Eastmoney via Tushare",
            records=_unique_composite(records, ("scheme", "sectorCode", "tsCode")),
            extra={
                "observationDate": target.isoformat(),
                "semantics": "provider_as_of_trade_date",
            },
        )

    async def _sector_money_flow(self, target: date) -> dict[str, Any]:
        """读取东财板块资金流，并保留其独立 DC 方法学。"""
        rows: list[dict[str, Any]] = []
        for content_type, expected_scheme in (
            ("行业", "eastmoney.industry"),
            ("概念", "eastmoney.concept"),
        ):
            partition = await self._query(
                "moneyflow_ind_dc",
                params={
                    "trade_date": _compact_date(target),
                    "content_type": content_type,
                },
                fields=(
                    "trade_date",
                    "content_type",
                    "ts_code",
                    "name",
                    "pct_change",
                    "close",
                    "net_amount",
                ),
                allow_empty=False,
                reject_limit=True,
            )
            if any(_dc_scheme(row.get("content_type")) != expected_scheme for row in partition):
                raise _schema_error("Tushare moneyflow_ind_dc content_type partition drifted")
            rows.extend(partition)
        records = [
            {
                "scheme": _dc_scheme(row.get("content_type")),
                "sectorCode": str(row["ts_code"]),
                "name": str(row["name"]),
                "tradeDate": _iso_date(row["trade_date"]),
                "changePercent": _decimal(row["pct_change"]),
                "close": _decimal(row["close"]),
                "netAmountCny": _decimal(row["net_amount"]),
            }
            for row in rows
            if _dc_scheme(row.get("content_type")) is not None
        ]
        return _envelope(
            schema="quant-v2.sector-money-flow-dc-eod.v1",
            source_dataset="moneyflow_ind_dc",
            upstream_source="Eastmoney via Tushare",
            records=records,
            extra={
                "tradeDate": target.isoformat(),
                "semanticFamily": "trade_direction_flow",
                "methodologyId": "eastmoney-sector-flow-dc",
                "methodologyVersion": "unknown",
                "methodologyStatus": "source_reported",
                "rawUnit": "CNY",
            },
        )

    async def _sw_taxonomy(self) -> dict[str, Any]:
        """读取申万 2021 一级、二级、三级 taxonomy，保留父代码和层级。"""
        rows = await self._query(
            "index_classify",
            params={"src": "SW2021"},
            fields=("index_code", "industry_name", "level", "parent_code", "src"),
            allow_empty=False,
            reject_limit=True,
        )
        records = [
            {
                "code": str(row["index_code"]),
                "name": str(row["industry_name"]),
                "level": _sw_level(row["level"]),
                "parentCode": _optional_text(row.get("parent_code")),
                "sourceRevision": str(row.get("src") or "SW2021"),
            }
            for row in rows
        ]
        return _envelope(
            schema="quant-v2.sw-taxonomy.v1",
            source_dataset="index_classify",
            upstream_source="Shenwan via Tushare",
            records=_unique_records(records, "code"),
            extra={"taxonomyVersion": "SW2021"},
        )

    async def _sw_memberships(
        self,
        target: date,
        level_three_codes_value: str,
    ) -> dict[str, Any]:
        """按三级节点分区读取申万正式成分和有效区间，避免单次全量截断。"""
        level_three_codes = _canonical_string_list(
            level_three_codes_value,
            maximum=1000,
        )
        records: list[dict[str, Any]] = []
        for node_code in level_three_codes:
            rows: list[dict[str, Any]] = []
            for is_new in ("Y", "N"):
                partition = await self._query(
                    "index_member_all",
                    params={"l3_code": node_code, "is_new": is_new},
                    fields=(
                        "l1_code",
                        "l1_name",
                        "l2_code",
                        "l2_name",
                        "l3_code",
                        "l3_name",
                        "ts_code",
                        "name",
                        "in_date",
                        "out_date",
                        "is_new",
                    ),
                    allow_empty=True,
                    reject_limit=True,
                )
                if any(str(row.get("is_new", "")).upper() != is_new for row in partition):
                    raise _schema_error("Tushare index_member_all ignored is_new partition")
                rows.extend(partition)
            records.extend(
                {
                    "l1Code": str(row["l1_code"]),
                    "l2Code": str(row["l2_code"]),
                    "l3Code": str(row["l3_code"]),
                    "tsCode": str(row["ts_code"]),
                    "name": str(row["name"]),
                    "inDate": _iso_date(row["in_date"]),
                    "outDate": _optional_iso_date(row.get("out_date")),
                    "isActive": str(row.get("is_new", "Y")).upper() in {"Y", "1", "TRUE"},
                    "isNew": str(row.get("is_new", "")).upper(),
                }
                for row in rows
                if _is_a_share_code(str(row["ts_code"]))
            )
        return _envelope(
            schema="quant-v2.sw-membership.v1",
            source_dataset="index_member_all",
            upstream_source="Shenwan via Tushare",
            records=_unique_composite(records, ("l3Code", "tsCode", "inDate")),
            extra={
                "snapshotDate": target.isoformat(),
                "taxonomyVersion": "SW2021",
                "historyMode": "latest_revision_effective_interval",
                "knowledgeCutoff": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "partitionedByIsNew": ["Y", "N"],
            },
        )

    async def _sw_market_data(self, target: date) -> dict[str, Any]:
        """读取申万日行情和 source-reported 估值，不映射为东财行业。"""
        rows = await self._query(
            "sw_daily",
            params={"trade_date": _compact_date(target)},
            fields=(
                "ts_code",
                "trade_date",
                "name",
                "open",
                "high",
                "low",
                "close",
                "change",
                "pct_change",
                "vol",
                "amount",
                "pe",
                "pb",
                "float_mv",
                "total_mv",
            ),
            allow_empty=False,
            reject_limit=True,
        )
        records = [
            {
                "code": str(row["ts_code"]),
                "name": str(row["name"]),
                "tradeDate": _iso_date(row["trade_date"]),
                "open": _decimal(row["open"]),
                "high": _decimal(row["high"]),
                "low": _decimal(row["low"]),
                "close": _decimal(row["close"]),
                "change": _decimal(row["change"]),
                "changePercent": _decimal(row["pct_change"]),
                # sw_daily 不直报昨收；同一行 `close-change` 可精确复算，并显式保留字段方法学。
                "previousClose": _decimal_difference(row["close"], row["change"]),
                "previousCloseAvailability": "available",
                "previousCloseMethodology": {
                    "kind": "derived",
                    "id": "sw-previous-close-from-close-change",
                    "version": "1",
                    "inputs": ["close", "change"],
                },
                "volume": _optional_decimal(row.get("vol")),
                # sw_daily 成交额和市值原始单位均为万元，统一转换为 CNY。
                "amountCny": _scaled_decimal(row.get("amount"), Decimal("10000")),
                "pe": _optional_decimal(row.get("pe")),
                "pb": _optional_decimal(row.get("pb")),
                "dividendYield": None,
                "floatMarketValueCny": _scaled_decimal(row.get("float_mv"), Decimal("10000")),
                "totalMarketValueCny": _scaled_decimal(row.get("total_mv"), Decimal("10000")),
                "finality": "final",
            }
            for row in rows
        ]
        return _envelope(
            schema="quant-v2.sw-market-data.v1",
            source_dataset="sw_daily",
            upstream_source="Shenwan via Tushare",
            records=records,
            extra={
                "tradeDate": target.isoformat(),
                "valuationMethodology": "source_reported",
                "taxonomyVersion": "SW2021",
                # 当前契约只保证数值来自 `sw_daily.vol`，在供应商单位证据冻结前不做猜测性换算。
                "volumeUnit": "provider_native",
                "amountRawUnit": "ten_thousand_CNY",
                "amountUnit": "CNY",
                "marketValueRawUnit": "ten_thousand_CNY",
                "marketValueUnit": "CNY",
                "previousCloseMethodology": {
                    "kind": "derived",
                    "id": "sw-previous-close-from-close-change",
                    "version": "1",
                    "inputs": ["close", "change"],
                },
            },
        )

    async def _query(
        self,
        api_name: str,
        *,
        params: dict[str, object],
        fields: tuple[str, ...],
        allow_empty: bool,
        reject_limit: bool = False,
    ) -> tuple[dict[str, Any], ...]:
        """调用一个 Tushare 端点并严格验证 code、字段和潜在截断。"""
        request_fingerprint = hashlib.sha256(
            _canonical_json(
                {
                    "apiName": api_name,
                    "params": params,
                    "fields": list(fields),
                }
            )
        ).hexdigest()
        request_body = _canonical_json(
            {
                "api_name": api_name,
                "token": self._token,
                "params": params,
                "fields": ",".join(fields),
            }
        )
        evidence = _REQUEST_EVIDENCE.get()
        try:
            raw = await self._request_with_retry(api_name, request_body)
        except ProviderError as error:
            if evidence is not None:
                evidence.append(
                    {
                        "endpoint": api_name,
                        "requestFingerprint": request_fingerprint,
                        "outcome": "transport_failed",
                        "errorCode": error.code.value,
                        "retryable": error.retryable,
                    }
                )
            raise
        evidence_entry: dict[str, Any] = {
            "endpoint": api_name,
            "requestFingerprint": request_fingerprint,
            "rawSha256": hashlib.sha256(raw).hexdigest(),
            "outcome": "response_received",
        }
        if evidence is not None:
            evidence.append(evidence_entry)
        try:
            response = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            evidence_entry["outcome"] = "schema_failed"
            raise _schema_error(f"Tushare endpoint {api_name} returned invalid JSON") from error
        if not isinstance(response, dict):
            evidence_entry["outcome"] = "schema_failed"
            raise _schema_error(f"Tushare endpoint {api_name} returned invalid envelope")
        code = response.get("code")
        if code != 0:
            evidence_entry["outcome"] = "provider_rejected"
            evidence_entry["providerCode"] = code if isinstance(code, int) else "non_integer"
            # 官方 code=2002 表示接口权限不足；消息可能含账户细节，因此不向上抛原文。
            if code == 2002:
                raise ProviderError(
                    ProviderErrorCode.AUTHENTICATION,
                    f"Tushare endpoint {api_name} entitlement is missing",
                    retryable=False,
                )
            if code in {-2001, -2002, 2001, 40101}:
                raise ProviderError(
                    ProviderErrorCode.AUTHENTICATION,
                    "Tushare token is invalid",
                    retryable=False,
                )
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                f"Tushare endpoint {api_name} rejected the request",
                retryable=False,
            )
        data = response.get("data")
        if not isinstance(data, dict):
            evidence_entry["outcome"] = "schema_failed"
            raise _schema_error(f"Tushare endpoint {api_name} returned no data object")
        actual_fields = data.get("fields")
        items = data.get("items")
        if not isinstance(actual_fields, list) or not all(
            isinstance(field, str) for field in actual_fields
        ):
            evidence_entry["outcome"] = "schema_failed"
            raise _schema_error(f"Tushare endpoint {api_name} returned invalid fields")
        if not isinstance(items, list):
            evidence_entry["outcome"] = "schema_failed"
            raise _schema_error(f"Tushare endpoint {api_name} returned invalid items")
        missing_fields = set(fields).difference(actual_fields)
        if missing_fields:
            evidence_entry["outcome"] = "schema_failed"
            raise _schema_error(f"Tushare endpoint {api_name} schema drifted")
        endpoint_limit = min(
            self._response_row_limit,
            _API_ROW_LIMITS.get(api_name, self._response_row_limit),
        )
        if reject_limit and len(items) >= endpoint_limit:
            evidence_entry["outcome"] = "schema_failed"
            raise _schema_error(f"Tushare endpoint {api_name} may be truncated at row limit")
        rows: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, list) or len(item) != len(actual_fields):
                evidence_entry["outcome"] = "schema_failed"
                raise _schema_error(f"Tushare endpoint {api_name} row width drifted")
            rows.append(dict(zip(actual_fields, item, strict=True)))
        if not allow_empty and not rows:
            evidence_entry["outcome"] = "empty"
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                f"Tushare endpoint {api_name} has no current records",
                retryable=True,
            )
        evidence_entry["outcome"] = "accepted"
        evidence_entry["rowCount"] = len(rows)
        return tuple(rows)

    async def _request_with_retry(self, api_name: str, body: bytes) -> bytes:
        """对频控、网络和服务端暂态失败执行共享节流下的有界抖动重试。"""
        for attempt in range(self._max_retries + 1):
            await self._acquire_request_slot()
            try:
                return await asyncio.to_thread(
                    self._transport,
                    body,
                    self._timeout_seconds,
                )
            except ProviderError:
                raise
            except urllib.error.HTTPError as error:
                retryable = error.code == 429 or 500 <= error.code < 600
                if not retryable or attempt >= self._max_retries:
                    raise ProviderError(
                        ProviderErrorCode.UNAVAILABLE,
                        f"Tushare endpoint {api_name} is unavailable",
                        retryable=retryable,
                    ) from error
            except (TimeoutError, urllib.error.URLError) as error:
                if attempt >= self._max_retries:
                    raise ProviderError(
                        ProviderErrorCode.UNAVAILABLE,
                        f"Tushare endpoint {api_name} is unavailable",
                        retryable=True,
                    ) from error
            # 抖动只影响暂态重试，不改变 canonical 载荷或幂等内容摘要。
            delay = (
                self._retry_base_seconds
                * (2**attempt)
                * (Decimal("0.75") + Decimal(str(random.random())) / Decimal("2"))
            )
            await self._sleep(float(delay))
        raise RuntimeError("bounded retry loop exhausted unexpectedly")

    async def _acquire_request_slot(self) -> None:
        """以 adapter 级共享滑动窗口限制所有并发 capability 的总请求速率。"""
        loop = asyncio.get_running_loop()
        while True:
            async with self._rate_lock:
                now = loop.time()
                while self._request_times and now - self._request_times[0] >= 60:
                    self._request_times.popleft()
                if len(self._request_times) < self._max_requests_per_minute:
                    self._request_times.append(now)
                    return
                wait_seconds = max(0.001, 60 - (now - self._request_times[0]))
            await self._sleep(wait_seconds)

    def _http_post(self, body: bytes, timeout_seconds: int) -> bytes:
        """经系统 CA 与 hostname 校验执行 HTTPS POST，并禁止重定向降级。"""
        request = urllib.request.Request(
            _API_URL,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        opener = urllib.request.build_opener(_HttpsOnlyRedirectHandler())
        with opener.open(request, timeout=timeout_seconds) as response:
            return response.read()


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """只允许 Tushare 请求在 HTTPS 之间重定向，防止 token 降级明文传输。"""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        """在构造重定向请求前拒绝非 HTTPS 目标。"""
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            raise urllib.error.URLError("Tushare redirect scheme is not allowed")
        return super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            newurl,
        )


def _envelope(
    *,
    schema: str,
    source_dataset: str,
    upstream_source: str,
    records: Iterable[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造所有 Tushare 能力共用的中立 schema、来源和记录 envelope。"""
    payload: dict[str, Any] = {
        "schema": schema,
        "source": {
            "provider": _PROVIDER_ID,
            "upstreamSource": upstream_source,
            "sourceDataset": source_dataset,
            "adapterVersion": _ADAPTER_VERSION,
        },
        "records": list(records),
    }
    if extra:
        payload.update(extra)
    return payload


def _canonical_json(value: object) -> bytes:
    """以稳定键序列编码 UTF-8 JSON，支持内容寻址和幂等重跑。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _attach_failure_evidence(
    error: ProviderError,
    *,
    provider_id: str,
    capability: str,
    evidence: list[dict[str, Any]],
    observed_at: datetime,
) -> None:
    """附加请求级脱敏摘要；响应原文、token 和供应商消息一律不持久化。"""
    allowed_keys = {
        "endpoint",
        "requestFingerprint",
        "rawSha256",
        "outcome",
        "providerCode",
        "errorCode",
        "retryable",
        "rowCount",
    }
    safe_entries = [
        {key: value for key, value in entry.items() if key in allowed_keys} for entry in evidence
    ]
    error.attach_failure_evidence(
        _canonical_json(
            {
                "schema": "quant-v2.provider-failure-evidence.v1",
                "provider": provider_id,
                "capability": capability,
                "observedAt": observed_at.isoformat().replace("+00:00", "Z"),
                "errorCode": error.code.value,
                "retryable": error.retryable,
                "requests": safe_entries,
                "rawResponseRetention": "hash_only",
            }
        )
    )


def _canonical_object_list(
    value: str,
    *,
    required_keys: tuple[str, ...],
    maximum: int,
) -> list[dict[str, str]]:
    """解析应用层冻结的 canonical 对象列表，拒绝缺字段、重复或无界输入。"""
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("canonical object list is invalid") from error
    if not isinstance(decoded, list) or not 1 <= len(decoded) <= maximum:
        raise ValueError("canonical object list size is invalid")
    rows: list[dict[str, str]] = []
    for item in decoded:
        if not isinstance(item, dict) or any(key not in item for key in required_keys):
            raise ValueError("canonical object list schema is invalid")
        row = {key: str(item[key]) for key in required_keys}
        if any(not value.strip() for value in row.values()):
            raise ValueError("canonical object list contains blank values")
        rows.append(row)
    identities = [tuple(row[key] for key in required_keys) for row in rows]
    if len(identities) != len(set(identities)):
        raise ValueError("canonical object list contains duplicates")
    return rows


def _canonical_string_list(value: str, *, maximum: int) -> list[str]:
    """解析应用层冻结的 canonical 字符串列表，拒绝空值和重复代码。"""
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("canonical string list is invalid") from error
    if (
        not isinstance(decoded, list)
        or not 1 <= len(decoded) <= maximum
        or any(not isinstance(item, str) or not item.strip() for item in decoded)
        or len(decoded) != len(set(decoded))
    ):
        raise ValueError("canonical string list schema is invalid")
    return decoded


def _required_date(parameters: dict[str, str], key: str) -> date:
    """读取 ISO 日期参数；缺失或非法值在发网请求前失败。"""
    value = parameters.get(key)
    if value is None:
        raise ValueError(f"{key} is required")
    return date.fromisoformat(value)


def _required_target(value: date | None) -> date:
    """要求日频 capability 显式携带交易日。"""
    if value is None:
        raise ValueError("tradeDate is required")
    return value


def _compact_date(value: date) -> str:
    """把 canonical ISO 日期转换为 Tushare `YYYYMMDD` 参数。"""
    return value.strftime("%Y%m%d")


def _iso_date(value: object) -> str:
    """把 Tushare 紧凑日期严格转换为 ISO 日期。"""
    normalized = str(value).strip()
    return datetime.strptime(normalized, "%Y%m%d").date().isoformat()


def _optional_iso_date(value: object) -> str | None:
    """转换可空供应商日期；空字面量不被伪造成有效日期。"""
    normalized = _optional_text(value)
    return None if normalized is None else _iso_date(normalized)


def _decimal(value: object) -> str:
    """把必填供应商数值转换为无科学计数法十进制字符串。"""
    if value is None or str(value).strip() == "":
        raise ValueError("required decimal value is missing")
    return format(_finite_decimal(value), "f")


def _optional_decimal(value: object) -> str | None:
    """保持来源空值为 null，同时把真实零值转换为精确字符串。"""
    if value is None or str(value).strip().lower() in {"", "none", "null"}:
        return None
    return format(_finite_decimal(value), "f")


def _scaled_decimal(value: object, scale: Decimal) -> str | None:
    """按已确认供应商单位缩放成 canonical CNY，不对未知单位猜倍率。"""
    if value is None or str(value).strip().lower() in {"", "none", "null"}:
        return None
    return format(_finite_decimal(value) * scale, "f")


def _decimal_difference(minuend: object, subtrahend: object) -> str:
    """用同一来源同一截点两字段复算差值，避免跨 publication 拼接昨收。"""
    return format(_finite_decimal(minuend) - _finite_decimal(subtrahend), "f")


def _scaled_sum(left: object, right: object, scale: Decimal) -> str | None:
    """将同一来源、同一订单语义族的两个大单分桶相加后统一成 CNY。"""
    left_value = _optional_finite_decimal(left)
    right_value = _optional_finite_decimal(right)
    if left_value is None and right_value is None:
        return None
    left_decimal = Decimal("0") if left_value is None else left_value
    right_decimal = Decimal("0") if right_value is None else right_value
    return format((left_decimal + right_decimal) * scale, "f")


def _finite_decimal(value: object) -> Decimal:
    """解析有限十进制；NaN 和 Infinity 不能进入 final canonical JSON。"""
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("provider decimal value must be finite")
    return parsed


def _optional_finite_decimal(value: object) -> Decimal | None:
    """仅把协议空值视为空；非有限数值必须失败而不是被吞成 null。"""
    if value is None or str(value).strip().lower() in {"", "none", "null"}:
        return None
    return _finite_decimal(value)


def _optional_text(value: object) -> str | None:
    """统一供应商空白、None 和 pandas 式空值字面量。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return (
        None
        if not normalized or normalized.lower() in {"none", "nan", "nat", "null"}
        else normalized
    )


def _optional_int(value: object) -> int | None:
    """读取可空整数状态码，不接受非整数浮点近似。"""
    normalized = _optional_text(value)
    return None if normalized is None else int(normalized)


def _exchange(ts_code: str) -> str:
    """把 Tushare 代码后缀映射到仓库稳定交易所编码。"""
    if ts_code.endswith(".SH"):
        return "SSE"
    if ts_code.endswith(".SZ"):
        return "SZSE"
    if ts_code.endswith(".BJ"):
        return "BSE"
    raise ValueError("unsupported A-share exchange suffix")


def _is_a_share_code(ts_code: str) -> bool:
    """只接受沪深北 A 股代码，排除 B 股、基金和其他同端点工具。"""
    symbol, separator, suffix = ts_code.partition(".")
    if not separator or len(symbol) != 6 or not symbol.isdigit():
        return False
    if suffix == "SH":
        return symbol.startswith(("600", "601", "603", "605", "688", "689"))
    if suffix == "SZ":
        return symbol.startswith(("000", "001", "002", "003", "300", "301"))
    if suffix == "BJ":
        return symbol.startswith(("4", "8", "9"))
    return False


def _dc_scheme(value: object) -> str | None:
    """把东财供应商分类标签映射为两个不可混用的 canonical scheme。"""
    normalized = (_optional_text(value) or "").lower()
    if "行业" in normalized or normalized in {"industry", "i"}:
        return "eastmoney.industry"
    if "概念" in normalized or normalized in {"concept", "n"}:
        return "eastmoney.concept"
    return None


def _sw_level(value: object) -> int:
    """把申万层级字段规范为 1/2/3，并拒绝未知 taxonomy 层级。"""
    normalized = str(value).strip().upper()
    mapping = {"L1": 1, "L2": 2, "L3": 3, "1": 1, "2": 2, "3": 3}
    try:
        return mapping[normalized]
    except KeyError as error:
        raise ValueError("unsupported SW taxonomy level") from error


def _unique_records(records: Iterable[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """按单业务键拒绝冲突重复；完全相同行只保留一次。"""
    by_key: dict[object, dict[str, Any]] = {}
    for record in records:
        identity = record[key]
        existing = by_key.get(identity)
        if existing is not None and existing != record:
            raise ValueError("conflicting provider records share one identity")
        by_key[identity] = record
    return list(by_key.values())


def _unique_composite(
    records: Iterable[dict[str, Any]], keys: tuple[str, ...]
) -> list[dict[str, Any]]:
    """按复合业务键拒绝冲突重复，保持成分关系可幂等发布。"""
    by_key: dict[tuple[object, ...], dict[str, Any]] = {}
    for record in records:
        identity = tuple(record[key] for key in keys)
        existing = by_key.get(identity)
        if existing is not None and existing != record:
            raise ValueError("conflicting provider records share one composite identity")
        by_key[identity] = record
    return list(by_key.values())


def _schema_error(message: str) -> ProviderError:
    """构造不可重试 schema 或截断错误，阻止错误批次进入 canonical。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)


__all__ = ["TushareMarketOverviewAdapter"]
