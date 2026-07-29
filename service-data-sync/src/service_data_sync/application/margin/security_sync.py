"""融资融券 P0 证券日明细和资格同步；成功时不留存来源原始字节。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from service_data_sync.application.margin.market_daily_sync import _archive_batch
from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.margin_market import (
    MarginEligibilityRepository,
    MarginSecurityDailyRepository,
)
from service_data_sync.application.ports.market_data import RawPayloadStore
from service_data_sync.domain.margin import MarginEligibility, MarginSecurityDaily, MarginVenue

_SECURITY_CAPABILITY = "market.margin.security.1d.reported"
_SECURITY_SCHEMA = "quant-v2.margin-security-daily.v1"
_ELIGIBILITY_CAPABILITY = "market.margin.eligibility.reported"
_ELIGIBILITY_SCHEMA = "quant-v2.margin-eligibility.v1"


@dataclass(frozen=True, slots=True)
class MarginSecuritySyncResult:
    """返回两融证券日明细或资格集合的发布版本，合法空集不生成发布。"""

    venue: MarginVenue
    data_version: UUID | None
    inserted_count: int
    unchanged_count: int
    availability: str = "available"


class MarginSecurityDailySyncService:
    """同步沪深证券两融直报日明细，深市缺失的偿还值保持为空。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: MarginSecurityDailyRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收中立 adapter、证券明细仓储和失败排障载荷端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(self, *, venue: MarginVenue, start: date, end: date) -> MarginSecuritySyncResult:
        """抓取有界证券明细；能力、场所或日期不满足 P0 约束时立即停止。"""
        batch = await _fetch(
            source=self._source,
            capability=_SECURITY_CAPABILITY,
            venue=venue,
            start=start,
            end=end,
        )
        records = decode_margin_security_daily_batch(batch.payload, venue=venue)
        if not records:
            return _empty_result(venue)
        published = self._repository.publish_security_daily(
            venue=venue,
            records=records,
            source=_archive_batch(batch=batch, payload_store=self._raw_payload_store),
        )
        return _result(
            published.venue,
            published.data_version,
            published.inserted_count,
            published.unchanged_count,
        )


class MarginEligibilitySyncService:
    """同步沪深两融资格公告或观察目录，绝不由当前缺席自动写入不适格状态。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: MarginEligibilityRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收中立 adapter、资格仓储和失败排障载荷端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(self, *, venue: MarginVenue, start: date, end: date) -> MarginSecuritySyncResult:
        """抓取有界资格记录；当前名单的观察事实不会改写公告证据历史。"""
        batch = await _fetch(
            source=self._source,
            capability=_ELIGIBILITY_CAPABILITY,
            venue=venue,
            start=start,
            end=end,
        )
        records = decode_margin_eligibility_batch(batch.payload, venue=venue)
        if not records:
            return _empty_result(venue)
        published = self._repository.publish_eligibility(
            venue=venue,
            records=records,
            source=_archive_batch(batch=batch, payload_store=self._raw_payload_store),
        )
        return _result(
            published.venue,
            published.data_version,
            published.inserted_count,
            published.unchanged_count,
        )


def decode_margin_security_daily_batch(
    payload: bytes, *, venue: MarginVenue
) -> tuple[MarginSecurityDaily, ...]:
    """解码一个场所证券日明细载荷，拒绝派生偿还、重复代码日期及 venue 漂移。"""
    decoded = _payload(payload, schema=_SECURITY_SCHEMA)
    if decoded.get("venue") != venue.code or decoded.get("valueKind", "REPORTED") != "REPORTED":
        raise _schema_error("margin security identity or value kind mismatch")
    _reject_unknown(decoded, {"schema", "venue", "valueKind", "records"}, "root")
    values = decoded.get("records")
    if not isinstance(values, list):
        raise _schema_error("margin security payload has no records")
    try:
        records = tuple(_security_record(value) for value in values)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise _schema_error("margin security value is invalid") from error
    if len({(item.source_security_code, item.trade_date) for item in records}) != len(records):
        raise _schema_error("margin security payload has duplicate identities")
    return tuple(sorted(records, key=lambda item: (item.trade_date, item.source_security_code)))


def decode_margin_eligibility_batch(
    payload: bytes, *, venue: MarginVenue
) -> tuple[MarginEligibility, ...]:
    """解码场所资格证据，观察目录和官方公告必须保留不同的知识基础。"""
    decoded = _payload(payload, schema=_ELIGIBILITY_SCHEMA)
    if decoded.get("venue") != venue.code:
        raise _schema_error("margin eligibility venue mismatch")
    _reject_unknown(decoded, {"schema", "venue", "records"}, "root")
    values = decoded.get("records")
    if not isinstance(values, list):
        raise _schema_error("margin eligibility payload has no records")
    try:
        records = tuple(_eligibility_record(value) for value in values)
    except (TypeError, ValueError) as error:
        raise _schema_error("margin eligibility value is invalid") from error
    if len({(item.source_security_code, item.effective_from) for item in records}) != len(records):
        raise _schema_error("margin eligibility payload has duplicate identities")
    return tuple(sorted(records, key=lambda item: (item.effective_from, item.source_security_code)))


async def _fetch(
    *, source: DataSourcePort, capability: str, venue: MarginVenue, start: date, end: date
):
    """按场所和日期窗读取一个 P0 capability，禁止沪深合并成一个来源请求。"""
    if start > end:
        raise ValueError("start must not be after end")
    if capability not in source.capabilities():
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            f"unsupported margin capability: {capability}",
            retryable=False,
        )
    return await source.fetch(
        SourceRequest(
            capability=capability,
            parameters=(
                ("venue", venue.code),
                ("start", start.isoformat()),
                ("end", end.isoformat()),
            ),
        )
    )


def _security_record(value: object) -> MarginSecurityDaily:
    """转换一行证券日明细；任何未治理来源列都触发 schema 审查。"""
    if not isinstance(value, dict):
        raise ValueError("margin security record is not an object")
    _reject_unknown(
        value,
        {
            "securityCode",
            "tradeDate",
            "financingBalance",
            "financingBuyAmount",
            "financingRepaymentReported",
            "financingRepaymentDerived",
            "lendingBalanceQty",
            "quantityUnit",
            "currency",
            "nullReason",
        },
        "margin security record",
    )
    return MarginSecurityDaily(
        source_security_code=_required(value, "securityCode"),
        trade_date=date.fromisoformat(_required(value, "tradeDate")),
        financing_balance=_optional_decimal(value.get("financingBalance")),
        financing_buy_amount=_optional_decimal(value.get("financingBuyAmount")),
        financing_repayment_reported=_optional_decimal(value.get("financingRepaymentReported")),
        financing_repayment_derived=_optional_decimal(value.get("financingRepaymentDerived")),
        lending_balance_qty=_optional_decimal(value.get("lendingBalanceQty")),
        quantity_unit=_optional_text(value.get("quantityUnit")),
        currency=_required(value, "currency"),
        null_reason=_optional_text(value.get("nullReason")),
    )


def _eligibility_record(value: object) -> MarginEligibility:
    """转换一个资格有效区间，撤销或调出必须来自明确来源记录而非目录差集。"""
    if not isinstance(value, dict):
        raise ValueError("margin eligibility record is not an object")
    _reject_unknown(
        value,
        {
            "securityCode",
            "status",
            "effectiveFrom",
            "effectiveTo",
            "announcementOn",
            "evidenceBasis",
        },
        "margin eligibility record",
    )
    return MarginEligibility(
        source_security_code=_required(value, "securityCode"),
        status=_required(value, "status"),
        effective_from=date.fromisoformat(_required(value, "effectiveFrom")),
        effective_to=_optional_date(value.get("effectiveTo")),
        announcement_on=_optional_date(value.get("announcementOn")),
        evidence_basis=_required(value, "evidenceBasis"),
    )


def _payload(payload: bytes, *, schema: str) -> dict[str, object]:
    """读取指定 schema 的 JSON 对象，避免资格或市场汇总载荷串到错误能力。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _schema_error("margin payload is not JSON") from error
    if not isinstance(decoded, dict) or decoded.get("schema") != schema:
        raise _schema_error("unexpected margin schema")
    return decoded


def _result(
    venue: MarginVenue, data_version: UUID, inserted_count: int, unchanged_count: int
) -> MarginSecuritySyncResult:
    """收敛两个仓储的发布结果，维持调用方无须依赖具体端口 DTO。"""
    return MarginSecuritySyncResult(
        venue=venue,
        data_version=data_version,
        inserted_count=inserted_count,
        unchanged_count=unchanged_count,
    )


def _empty_result(venue: MarginVenue) -> MarginSecuritySyncResult:
    """返回无匹配事实的成功结果，调用方据此写空观测而不保留成功来源字节。"""
    return MarginSecuritySyncResult(
        venue=venue,
        data_version=None,
        inserted_count=0,
        unchanged_count=0,
        availability="empty",
    )


def _required(value: dict[str, object], key: str) -> str:
    """读取非空文本，拒绝 pandas 缺失字面量污染证券或资格身份。"""
    normalized = _optional_text(value.get(key))
    if normalized is None:
        raise ValueError(f"{key} is required")
    return normalized


def _optional_text(value: object) -> str | None:
    """统一空值与空白字符串，让未披露字段保留为真正的 `None`。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return None if not normalized or normalized.lower() in {"nan", "none", "nat"} else normalized


def _optional_decimal(value: object) -> Decimal | None:
    """保留来源空值且使用精确十进制，真实零值不被转换为缺失。"""
    normalized = _optional_text(value)
    return None if normalized is None else Decimal(normalized)


def _optional_date(value: object) -> date | None:
    """解析可选 ISO 日期；日期未知不能由抓取时刻或当前名单替代。"""
    normalized = _optional_text(value)
    return None if normalized is None else date.fromisoformat(normalized)


def _reject_unknown(value: dict[str, object], allowed: set[str], location: str) -> None:
    """拒绝未治理列，使估算值和滚动排行无法通过标准载荷偷偷进入 P0。"""
    unknown = set(value).difference(allowed)
    if unknown:
        raise ValueError(f"unexpected {location} fields: {', '.join(sorted(unknown))}")


def _schema_error(message: str) -> ProviderError:
    """构造不可重试 schema 异常，避免字段映射错误被任务层误判为临时网络故障。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)
