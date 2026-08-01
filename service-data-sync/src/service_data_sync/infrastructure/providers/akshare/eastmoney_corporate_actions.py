"""经由 `AKShare SDK` 获取东方财富个股分红送转事件的适配器。

标准事件保留报告、公告、登记和除权等来源日期，按“每十股”的原始方案单位输出；
不会把尚未实施的方案当成已生效现金流，也不会将窗口外、但名称相似的记录拼入本次
证券发布。供应商状态和可选日期均被保留，以支持后续修订而非覆盖历史。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import akshare as ak

from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.domain.equity import EquityIdentifier

_CAPABILITY = "equity.corporate_action"
_SCHEMA = "quant-v2.equity-corporate-action.v1"


class AkshareEastmoneyCorporateActionsAdapter:
    """读取分红送转详情，并保留方案状态与实施日期供后续修订。

    能力只覆盖东财披露的分红送转，不能用于补齐拆并股、配股或其他公司行动类型。
    """

    provider_id = "akshare-eastmoney-corporate-action"

    def __init__(self, *, request_timeout_seconds: int) -> None:
        """保存阻塞式 AKShare 公司行动请求的墙钟超时。"""
        self._request_timeout_seconds = request_timeout_seconds

    def capabilities(self) -> frozenset[str]:
        """声明单一分红送转事件能力。"""
        return frozenset({_CAPABILITY})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """获取一个证券的历史方案，并按请求窗口筛选相关事件。"""
        identifier, start, end = _request_values(request)
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                frame = await asyncio.to_thread(
                    ak.stock_fhps_detail_em,
                    symbol=identifier.symbol,
                )
        except TimeoutError as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE, "provider request timed out", retryable=True
            ) from error
        except Exception as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE, "provider request failed", retryable=True
            ) from error
        try:
            raw_records = frame.to_dict(orient="records")
            actions = [
                _normalize_record(record)
                for record in raw_records
                if _record_intersects(record, start=start, end=end)
            ]
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "provider corporate-action schema changed",
                retryable=False,
            ) from error
        payload = json.dumps(
            {
                "schema": _SCHEMA,
                "instrument": identifier.qualified_symbol,
                "actions": actions,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        raw_payload = json.dumps(
            {"instrument": identifier.qualified_symbol, "records": raw_records},
            ensure_ascii=False,
            default=_json_default,
            separators=(",", ":"),
        ).encode()
        keys = sorted(raw_records[0]) if raw_records else []
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=payload,
            observed_at=datetime.now(UTC),
            content_type="application/vnd.quant-v2.equity-corporate-action+json",
            raw_payload=raw_payload,
            raw_content_type="application/json",
            upstream_source="eastmoney-share-bonus",
            adapter_version="akshare-1.18.81-v1",
            schema_fingerprint=hashlib.sha256(
                json.dumps(keys, ensure_ascii=False).encode()
            ).hexdigest(),
        )


def _request_values(request: SourceRequest) -> tuple[EquityIdentifier, date, date]:
    """解析中立公司行动请求与包含端日期窗口。"""
    if request.capability != _CAPABILITY:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST, "unsupported capability", retryable=False
        )
    parameters = dict(request.parameters)
    try:
        identifier = EquityIdentifier.parse(parameters["instrument"])
        start = date.fromisoformat(parameters["start"])
        end = date.fromisoformat(parameters["end"])
    except (KeyError, ValueError) as error:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST, "invalid corporate-action request", retryable=False
        ) from error
    if start > end:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST, "invalid date range", retryable=False
        )
    return identifier, start, end


def _record_intersects(record: dict[str, Any], *, start: date, end: date) -> bool:
    """按 canonical 事件日期命中窗口，和 coverage 及公开读取保持同一优先级。

    公司行动优先采用除权日，其次登记日、最新或预案公告日，最后才是报告期。若一行
    完全没有可解析日期，就无法证明它在请求窗口外，必须作为 schema 错误失败关闭。
    """
    fact_date = next(
        (
            parsed
            for value in (
                record.get("除权除息日"),
                record.get("股权登记日"),
                record.get("最新公告日期"),
                record.get("预案公告日"),
                record.get("报告期"),
            )
            if (parsed := _optional_date(value)) is not None
        ),
        None,
    )
    if fact_date is None:
        raise ValueError("provider corporate-action candidate has no reconcilable date")
    return start <= fact_date <= end


def _normalize_record(record: dict[str, Any]) -> dict[str, str | None]:
    """将东财分红送转中文字段映射为标准每十股事件值。

    数量和现金字段仍以“每十股”为单位，转换为每股由领域用例按明确规则完成，避免
    适配器与消费者各自换算造成双重缩放。
    """
    report_period = _required_date(record["报告期"])
    latest_announcement = _optional_date(record.get("最新公告日期"))
    initial_announcement = _optional_date(record.get("预案公告日"))
    return {
        "sourceEventKey": report_period.isoformat(),
        "reportPeriod": report_period.isoformat(),
        "status": _required_text(record.get("方案进度")),
        "announcementDate": _date_text(latest_announcement or initial_announcement),
        "recordDate": _date_text(_optional_date(record.get("股权登记日"))),
        "exDate": _date_text(_optional_date(record.get("除权除息日"))),
        "cashDividendPer10": _decimal_text(record.get("现金分红-现金分红比例")),
        "bonusSharesPer10": _decimal_text(record.get("送转股份-送股比例")),
        "transferSharesPer10": _decimal_text(record.get("送转股份-转股比例")),
    }


def _required_text(value: object) -> str:
    """读取非空供应商文本。"""
    normalized = _optional_text(value)
    if normalized is None:
        raise ValueError("required provider text is missing")
    return normalized


def _optional_text(value: object) -> str | None:
    """将 pandas 空值与空白文本映射为真实空值。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return None if not normalized or normalized.lower() in {"nan", "nat", "none"} else normalized


def _required_date(value: object) -> date:
    """读取必填供应商日期。"""
    parsed = _optional_date(value)
    if parsed is None:
        raise ValueError("required provider date is missing")
    return parsed


def _optional_date(value: object) -> date | None:
    """把 pandas 日期、日期时间或文本映射为可空日期。"""
    normalized = _optional_text(value)
    if normalized is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(normalized[:10])


def _decimal_text(value: object) -> str | None:
    """把可空供应商数值映射为有限十进制字符串。"""
    normalized = _optional_text(value)
    if normalized is None:
        return None
    parsed = Decimal(normalized)
    if not parsed.is_finite():
        raise ValueError("provider numeric value must be finite")
    return str(parsed)


def _date_text(value: date | None) -> str | None:
    """把可空日期转换为 ISO 文本。"""
    return None if value is None else value.isoformat()


def _json_default(value: object) -> str:
    """序列化 raw evidence 中的日期和 pandas 标量展示值。"""
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)
