"""沪深港通官方单日完整包同步。

应用服务以 HKEX 日历决定交易日，读取同一通道方向的 licensed 日统计、官方活跃榜、身份主数据
和最终状态，再发布 canonical 组件与原子 bundle。任何缺源、身份歧义、finality 缺失或 schema
漂移都阻断新 publication；历史回填同样不得用平台猜测状态补洞。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.market_data import RawPayloadStore
from service_data_sync.application.ports.stock_connect import (
    PublishedStockConnectBundle,
    StockConnectCenterRepository,
    StockConnectMarketRepository,
)
from service_data_sync.application.stock_connect.active_security_sync import (
    decode_stock_connect_active_security_batch,
)
from service_data_sync.application.stock_connect.market_daily_sync import (
    _archive_batch,
    decode_stock_connect_market_daily_batch,
)
from service_data_sync.domain.stock_connect import (
    StockConnectCalendarDay,
    StockConnectChannel,
    StockConnectChannelStatus,
    StockConnectInstrumentMaster,
    StockConnectMarketDaily,
)

_CALENDAR_SCHEMA = "quant-v2.stock-connect-calendar.v1"
_MASTER_SCHEMA = "quant-v2.stock-connect-instrument-master.v2"
_STATUS_SCHEMA = "quant-v2.stock-connect-channel-status.v1"
MARKET_STAT_CAPABILITY = "market.stock_connect.market_stat.reported"
ACTIVE_SECURITY_CAPABILITY = "market.stock_connect.active_security.snapshot"
TRADING_CALENDAR_CAPABILITY = "market.stock_connect.trading_calendar"
INSTRUMENT_MASTER_CAPABILITY = "market.stock_connect.instrument_master.reported"
CHANNEL_STATUS_CAPABILITY = "market.stock_connect.channel_status.eod"


@dataclass(frozen=True, slots=True)
class StockConnectDailyBundleSyncResult:
    """描述一个通道日期是否休市跳过，或成功产生哪个原子 bundle。"""

    channel: StockConnectChannel
    trade_date: date
    availability: str
    bundle_release_id: UUID | None
    data_version: str | None
    reused: bool
    active_security_count: int


class StockConnectDailyBundleSyncService:
    """编排五项官方 capability 与两个 canonical 组件的全链路 publication。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        market_repository: StockConnectMarketRepository,
        center_repository: StockConnectCenterRepository,
        raw_payload_store: RawPayloadStore,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """保存官方来源、仓储和时钟；同进程年度日历按真实批次缓存。"""
        self._source = source
        self._market_repository = market_repository
        self._center_repository = center_repository
        self._raw_payload_store = raw_payload_store
        self._now = now or (lambda: datetime.now(UTC))
        self._calendar_cache: dict[
            int, tuple[ProviderBatch, tuple[StockConnectCalendarDay, ...]]
        ] = {}

    async def sync(
        self,
        *,
        channel: StockConnectChannel,
        trade_date: date,
        overview_generation_id: UUID,
        overview_channels: tuple[str, ...],
        before_bundle_publication: Callable[[], None] | None = None,
    ) -> StockConnectDailyBundleSyncResult:
        """同步一个明确业务日；官方日历关闭时不访问成交数据且不制造空 publication。"""
        required = {
            MARKET_STAT_CAPABILITY,
            ACTIVE_SECURITY_CAPABILITY,
            TRADING_CALENDAR_CAPABILITY,
            CHANNEL_STATUS_CAPABILITY,
        }
        missing = required.difference(self._source.capabilities())
        if missing:
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "official stock-connect source lacks required capabilities",
                retryable=False,
            )
        calendar_batch, calendar_records = await self._calendar(trade_date.year)
        matching = [item for item in calendar_records if item.calendar_date == trade_date]
        if len(matching) != 1:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "HKEX calendar does not contain exactly one requested date",
                retryable=False,
            )
        calendar = matching[0]
        trading = (
            calendar.northbound_trading
            if channel.direction == "NORTHBOUND"
            else calendar.southbound_trading
        )
        if not trading:
            return StockConnectDailyBundleSyncResult(
                channel=channel,
                trade_date=trade_date,
                availability="official_calendar_closed",
                bundle_release_id=None,
                data_version=None,
                reused=False,
                active_security_count=0,
            )
        source_refs = [_source_ref(calendar_batch)]
        _archive_batch(batch=calendar_batch, payload_store=self._raw_payload_store)
        master_names: dict[str, str] = {}
        parameters = (
            ("channel", channel.channel),
            ("direction", channel.direction),
            ("start", trade_date.isoformat()),
            ("end", trade_date.isoformat()),
        )
        market_batch = await self._fetch(MARKET_STAT_CAPABILITY, parameters)
        active_batch = await self._fetch(ACTIVE_SECURITY_CAPABILITY, parameters)
        market_records = decode_stock_connect_market_daily_batch(
            market_batch.payload, channel=channel
        )
        active_records = decode_stock_connect_active_security_batch(
            active_batch.payload, channel=channel
        )
        if len(market_records) != 1 or market_records[0].trade_date != trade_date:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "stock-connect market batch does not match the requested business date",
                retryable=False,
            )
        if any(item.trade_date != trade_date for item in active_records):
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "stock-connect active batch crossed the requested business date",
                retryable=False,
            )
        if (
            channel.direction == "SOUTHBOUND"
            and active_records
            and INSTRUMENT_MASTER_CAPABILITY in self._source.capabilities()
        ):
            try:
                master_batch = await self._fetch(
                    INSTRUMENT_MASTER_CAPABILITY,
                    (("trade_date", trade_date.isoformat()),),
                )
            except ProviderError as error:
                if error.code is not ProviderErrorCode.UNAVAILABLE:
                    raise
            else:
                master = decode_stock_connect_instrument_master_batch(master_batch.payload)
                active_codes = {item.source_instrument_code for item in active_records}
                scoped_master = tuple(
                    item for item in master if item.source_instrument_code in active_codes
                )
                master_observation = _archive_batch(
                    batch=master_batch, payload_store=self._raw_payload_store
                )
                # 完整快照只用于关闭已跟踪身份；仓储仍只新增本次活跃榜出现的证券。
                self._market_repository.ensure_hkex_instruments(
                    records=master,
                    target_source_codes=active_codes,
                    source=master_observation,
                )
                master_names = {
                    item.source_instrument_code: item.display_name for item in scoped_master
                }
                source_refs.append(_source_ref(master_batch))
        if master_names:
            active_records = tuple(
                replace(
                    item,
                    source_instrument_name=(
                        item.source_instrument_name or master_names.get(item.source_instrument_code)
                    ),
                )
                for item in active_records
            )
        market_publication = self._market_repository.publish_market_daily(
            channel=channel,
            records=market_records,
            source=_archive_batch(batch=market_batch, payload_store=self._raw_payload_store),
        )
        active_publication = (
            None
            if not active_records
            else self._market_repository.publish_active_securities(
                channel=channel,
                records=active_records,
                source=_archive_batch(batch=active_batch, payload_store=self._raw_payload_store),
            )
        )
        source_refs.append(_source_ref(market_batch))
        source_refs.append(_source_ref(active_batch))
        quality_issues: list[dict[str, str]] = _optional_field_issues(market_records[0])
        status_batch = await self._fetch(CHANNEL_STATUS_CAPABILITY, parameters)
        status = decode_stock_connect_channel_status_batch(status_batch.payload, channel=channel)
        if status.trade_date != trade_date:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "stock-connect status business date does not match request",
                retryable=False,
            )
        _archive_batch(batch=status_batch, payload_store=self._raw_payload_store)
        if status.source_file_sha256 is not None:
            source_refs.append(_source_ref(status_batch))
        quality_issues.extend(_status_quality_issues(status))
        if before_bundle_publication is not None:
            before_bundle_publication()
        published = self._center_repository.publish_bundle(
            channel=channel,
            overview_generation_id=overview_generation_id,
            overview_channels=overview_channels,
            market_data_version=market_publication.data_version,
            active_data_version=(
                None if active_publication is None else active_publication.data_version
            ),
            calendar=calendar,
            calendar_source_ref=_source_ref(calendar_batch),
            calendar_observed_at=calendar_batch.observed_at,
            status=status,
            quality_issues=quality_issues,
            source_refs=source_refs,
        )
        return _result(
            channel=channel,
            trade_date=trade_date,
            active_count=len(active_records),
            publication=published,
        )

    async def trading_dates(
        self,
        *,
        channel: StockConnectChannel,
        start: date,
        end: date,
    ) -> tuple[date, ...]:
        """按 HKEX 官方年度 CSV 返回闭区间真实开放日，不用周末或本地交易日历推测。"""
        if start > end:
            raise ValueError("stock-connect date range is inverted")
        records: list[StockConnectCalendarDay] = []
        for year in range(start.year, end.year + 1):
            _batch, values = await self._calendar(year)
            records.extend(values)
        return tuple(
            item.calendar_date
            for item in records
            if start <= item.calendar_date <= end
            and (
                item.northbound_trading
                if channel.direction == "NORTHBOUND"
                else item.southbound_trading
            )
        )

    async def _calendar(
        self, year: int
    ) -> tuple[ProviderBatch, tuple[StockConnectCalendarDay, ...]]:
        """读取并缓存一个年度官方日历批次，缓存键不使用抓取日。"""
        cached = self._calendar_cache.get(year)
        if cached is not None:
            return cached
        batch = await self._fetch(
            TRADING_CALENDAR_CAPABILITY,
            (("year", str(year)),),
        )
        records = decode_stock_connect_calendar_batch(batch.payload, year=year)
        self._calendar_cache[year] = (batch, records)
        return batch, records

    async def _fetch(
        self, capability: str, parameters: tuple[tuple[str, str], ...]
    ) -> ProviderBatch:
        """向冻结官方 adapter 请求一项 capability，不做跨来源 fallback。"""
        return await self._source.fetch(SourceRequest(capability=capability, parameters=parameters))


def decode_stock_connect_calendar_batch(
    payload: bytes, *, year: int
) -> tuple[StockConnectCalendarDay, ...]:
    """解码 adapter 标准日历并要求全年日期唯一且请求年度一致。"""
    decoded = _payload(payload, schema=_CALENDAR_SCHEMA)
    payload_year = decoded.get("year")
    if isinstance(payload_year, bool) or not isinstance(payload_year, int) or payload_year != year:
        raise _schema_error("stock-connect calendar year mismatch")
    rows = decoded.get("records")
    if not isinstance(rows, list):
        raise _schema_error("stock-connect calendar has no records")
    try:
        records = tuple(
            StockConnectCalendarDay(
                calendar_date=date.fromisoformat(_required(row, "calendarDate")),
                northbound_trading=_required_bool(row, "northboundTrading"),
                southbound_trading=_required_bool(row, "southboundTrading"),
                hong_kong_state=_required(row, "hongKongState"),
                mainland_state=_required(row, "mainlandState"),
            )
            for row in rows
        )
    except (TypeError, ValueError) as error:
        raise _schema_error("stock-connect calendar value is invalid") from error
    if len(records) < 200 or len({item.calendar_date for item in records}) != len(records):
        raise _schema_error("stock-connect calendar coverage is incomplete or duplicated")
    return tuple(sorted(records, key=lambda item: item.calendar_date))


def decode_stock_connect_instrument_master_batch(
    payload: bytes,
) -> tuple[StockConnectInstrumentMaster, ...]:
    """解码 HKEX Securities Master；稳定 ID 缺失保留为不可解析身份。"""
    decoded = _payload(payload, schema=_MASTER_SCHEMA)
    rows = decoded.get("records")
    if not isinstance(rows, list):
        raise _schema_error("stock-connect instrument master has no records")
    try:
        records = tuple(
            StockConnectInstrumentMaster(
                source_security_id=_optional_text(row.get("securityId")),
                source_instrument_code=_required(row, "instrumentCode"),
                display_name=_required(row, "displayName"),
                effective_from=date.fromisoformat(_required(row, "effectiveFrom")),
            )
            for row in rows
        )
    except (TypeError, ValueError) as error:
        raise _schema_error("stock-connect instrument master value is invalid") from error
    if not records or len({item.source_instrument_code for item in records}) != len(records):
        raise _schema_error("stock-connect instrument master is empty or duplicated")
    stable_ids = [
        item.source_security_id for item in records if item.source_security_id is not None
    ]
    if len(set(stable_ids)) != len(stable_ids):
        raise _schema_error("stock-connect instrument master has duplicate stable security ids")
    return tuple(sorted(records, key=lambda item: item.source_instrument_code))


def decode_stock_connect_channel_status_batch(
    payload: bytes, *, channel: StockConnectChannel
) -> StockConnectChannelStatus:
    """解码一个官方最终状态，额度值只允许基础单位人民币。"""
    decoded = _payload(payload, schema=_STATUS_SCHEMA)
    if decoded.get("channel") != channel.channel or decoded.get("direction") != channel.direction:
        raise _schema_error("stock-connect status channel mismatch")
    rows = decoded.get("records")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise _schema_error("stock-connect status must contain exactly one record")
    row = rows[0]
    try:
        return StockConnectChannelStatus(
            trade_date=date.fromisoformat(_required(row, "tradeDate")),
            channel=channel.channel,
            direction=channel.direction,
            trading_day=_required_bool(row, "tradingDay"),
            session_state=_required(row, "sessionState"),
            session_availability=_required(row, "sessionAvailability"),
            buy_order_accepted=_optional_bool(row.get("buyOrderAccepted")),
            sell_order_accepted=_optional_bool(row.get("sellOrderAccepted")),
            quota_state=_required(row, "quotaState"),
            quota_balance=_optional_decimal(row.get("quotaBalance")),
            quota_currency=_required(row, "quotaCurrency"),
            observed_at=_datetime(_required(row, "observedAt")),
            source_code=_required(row, "sourceCode"),
            product_name=_required(row, "productName"),
            source_publication_at=_optional_datetime(row.get("sourcePublicationAt")),
            source_file_sha256=_optional_text(row.get("sourceFileSha256")),
        )
    except (InvalidOperation, TypeError, ValueError) as error:
        raise _schema_error("stock-connect status value is invalid") from error


def _source_ref(batch: ProviderBatch) -> dict[str, object]:
    """投影来源 publication 可用性、真实接收时间与摘要，禁止使用 mtime 补空。"""
    decoded = _payload(batch.payload)
    rows = decoded.get("records")
    first = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    source_code = str(first.get("sourceCode") or batch.upstream_source or "")
    product_name = str(decoded.get("productName") or first.get("productName") or "").strip()
    publication_availability = str(
        decoded.get("sourcePublicationAvailability")
        or first.get("sourcePublicationAvailability")
        or ""
    ).strip()
    publication_value = decoded.get("sourcePublicationAt")
    if publication_value is None:
        publication_value = first.get("sourcePublicationAt")
    observed_value = decoded.get("sourceObservedAt")
    if observed_value is None:
        observed_value = first.get("sourceObservedAt")
    digest = str(decoded.get("sourceFileSha256") or first.get("sourceFileSha256") or "").strip()
    if source_code not in {
        "HKEX_DATA_MARKETPLACE",
        "HKEX_OMDC",
        "HKEX_CALENDAR",
        "SSE_MDGW",
        "SZSE_STEP",
    }:
        raise _schema_error("stock-connect source code is not approved")
    if not product_name or len(digest) != 64:
        raise _schema_error("stock-connect source reference is incomplete")
    if publication_availability not in {
        "NOT_PROVIDED_BY_SOURCE",
        "REPORTED",
    }:
        raise _schema_error("stock-connect source publication availability is invalid")
    if publication_availability == "REPORTED":
        if not isinstance(publication_value, str):
            raise _schema_error("reported stock-connect publication time is missing")
        publication_at: str | None = publication_value
        _datetime(publication_at)
    else:
        if publication_value is not None:
            raise _schema_error("unreported stock-connect publication time must be null")
        publication_at = None
    if not isinstance(observed_value, str):
        raise _schema_error("stock-connect source observed time is missing")
    observed_at = _datetime(observed_value)
    if observed_at != batch.observed_at:
        raise _schema_error("stock-connect source observed time does not match batch")
    return {
        "sourceCode": source_code,
        "productName": product_name,
        "sourcePublicationAvailability": publication_availability,
        "sourcePublicationAt": publication_at,
        "sourceObservedAt": observed_value,
        "sourceFileSha256": digest,
    }


def _optional_field_issues(
    value: StockConnectMarketDaily,
) -> list[dict[str, str]]:
    """把非阻断可选字段来源缺失合并为一个稳定质量警告。"""
    availability = dict(value.field_availability)
    missing = sorted(field for field, state in availability.items() if state == "SOURCE_MISSING")
    if not missing:
        return []
    return [
        {
            "code": "OPTIONAL_FIELD_SOURCE_MISSING",
            "component": "market-stats",
            "detail": "optional official fields unavailable: " + ",".join(missing),
        }
    ]


def _status_quality_issues(
    status: StockConnectChannelStatus,
) -> list[dict[str, str]]:
    """把受控派生或历史缺源状态转成稳定警告，禁止伪装为完整来源。"""
    if status.session_availability == "SOURCE_MISSING":
        return [
            {
                "code": "STATUS_SOURCE_NOT_AVAILABLE_HISTORICAL",
                "component": "channel-status",
                "detail": (
                    "participant status delivery was not declared available for this "
                    "historical business date"
                ),
            }
        ]
    if status.session_availability != "DERIVED":
        return []
    return [
        {
            "code": "SESSION_STATE_DERIVED_FROM_CALENDAR_AND_FINALITY",
            "component": "channel-status",
            "detail": (
                "OMD-C Msg80 does not report session state; CLOSED is derived "
                "from the official open-day calendar and END_OF_DAY_FINAL evidence"
            ),
        }
    ]


def _result(
    *,
    channel: StockConnectChannel,
    trade_date: date,
    active_count: int,
    publication: PublishedStockConnectBundle,
) -> StockConnectDailyBundleSyncResult:
    """将仓储 publication 投影为控制面可汇总的单日结果。"""
    return StockConnectDailyBundleSyncResult(
        channel=channel,
        trade_date=trade_date,
        availability="published",
        bundle_release_id=publication.bundle_release_id,
        data_version=publication.data_version,
        reused=publication.reused,
        active_security_count=active_count,
    )


def _payload(payload: bytes, *, schema: str | None = None) -> dict[str, object]:
    """读取标准 JSON 对象并可选校验 schema。"""
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _schema_error("stock-connect normalized payload is not JSON") from error
    if not isinstance(value, dict) or (schema is not None and value.get("schema") != schema):
        raise _schema_error("stock-connect normalized payload schema is invalid")
    return value


def _required(value: object, key: str) -> str:
    """从对象读取非空文本字段。"""
    if not isinstance(value, dict):
        raise ValueError("stock-connect row is not an object")
    result = _optional_text(value.get(key))
    if result is None:
        raise ValueError(f"{key} is required")
    return result


def _required_bool(value: object, key: str) -> bool:
    """读取严格 JSON 布尔值，不接受字符串真假。"""
    if not isinstance(value, dict) or not isinstance(value.get(key), bool):
        raise ValueError(f"{key} must be boolean")
    return bool(value[key])


def _optional_bool(value: object) -> bool | None:
    """读取可选严格布尔值。"""
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError("optional status flag must be boolean or null")
    return value


def _optional_decimal(value: object) -> Decimal | None:
    """读取可选精确十进制值，空与真实零保持不同。"""
    normalized = _optional_text(value)
    return None if normalized is None else Decimal(normalized)


def _optional_text(value: object) -> str | None:
    """把空白和 JSON null 规范为 None，保留其他来源文本。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _datetime(value: str) -> datetime:
    """读取必须带时区的 RFC 3339 时间。"""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("stock-connect source timestamp must include timezone")
    return parsed


def _optional_datetime(value: object) -> datetime | None:
    """读取可选来源 publication；历史缺源必须保持空值。"""
    normalized = _optional_text(value)
    return None if normalized is None else _datetime(normalized)


def _schema_error(message: str) -> ProviderError:
    """构造不可重试 schema 错误。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)
