"""经由 `AKShare SDK` 获取新浪稀疏累计后复权因子的适配器。

输出的是按生效日变化的累计因子，不是已经复权后的价格，也不表示前复权或后复权
收益率。因子必须为正且有限；上游返回窗口外的行会被过滤，日期窗口按两端包含处理，
以便应用层能够稳定选择历史锚点。
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
from service_data_sync.domain.equity import EquityIdentifier, Exchange

_CAPABILITY = "equity.adjustment_factor"
_SCHEMA = "quant-v2.equity-adjustment-factor.v1"


class AkshareSinaAdjustmentFactorsAdapter:
    """读取新浪稀疏累计后复权因子，不保存供应商的复权价格序列。

    新浪代码前缀只用于 SDK 请求；发布后的证券身份始终是平台的交易所限定代码。
    """

    provider_id = "akshare-sina-adjustment-factor"

    def __init__(self, *, request_timeout_seconds: int) -> None:
        """保存阻塞式 AKShare 因子请求的墙钟超时。"""
        self._request_timeout_seconds = request_timeout_seconds

    def capabilities(self) -> frozenset[str]:
        """声明单一累计因子能力。"""
        return frozenset({_CAPABILITY})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """获取并过滤一个证券日期窗口内的累计后复权因子。"""
        identifier, start, end = _request_values(request)
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                frame = await asyncio.to_thread(
                    ak.stock_zh_a_daily,
                    symbol=_sina_symbol(identifier),
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    adjust="hfq-factor",
                )
        except TimeoutError as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE, "provider request timed out", retryable=True
            ) from error
        except Exception as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE, "provider request failed", retryable=True
            ) from error
        if frame.empty:
            raise ProviderError(
                ProviderErrorCode.SCHEMA, "provider returned no adjustment factors", retryable=False
            )
        try:
            raw_records = frame.to_dict(orient="records")
            # SDK 可能返回更宽日期范围；只允许请求窗口内因子影响本次发布。
            factors = [
                _normalize_record(record)
                for record in raw_records
                if start <= _record_date(record) <= end
            ]
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "provider adjustment-factor schema changed",
                retryable=False,
            ) from error
        if not factors:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "provider returned no factors in requested window",
                retryable=False,
            )
        payload = json.dumps(
            {
                "schema": _SCHEMA,
                "instrument": identifier.qualified_symbol,
                "factors": factors,
            },
            separators=(",", ":"),
        ).encode()
        raw_payload = json.dumps(
            {"instrument": identifier.qualified_symbol, "records": raw_records},
            ensure_ascii=False,
            default=_json_default,
            separators=(",", ":"),
        ).encode()
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=payload,
            observed_at=datetime.now(UTC),
            content_type="application/vnd.quant-v2.equity-adjustment-factor+json",
            raw_payload=raw_payload,
            raw_content_type="application/json",
            upstream_source="sina-hfq-factor",
            adapter_version="akshare-1.18.78-v1",
            schema_fingerprint=hashlib.sha256(
                json.dumps(sorted(raw_records[0]), ensure_ascii=False).encode()
            ).hexdigest(),
        )


def _request_values(request: SourceRequest) -> tuple[EquityIdentifier, date, date]:
    """解析中立因子请求并校验包含端日期窗口。"""
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
            ProviderErrorCode.INVALID_REQUEST, "invalid adjustment-factor request", retryable=False
        ) from error
    if start > end:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST, "invalid date range", retryable=False
        )
    return identifier, start, end


def _sina_symbol(identifier: EquityIdentifier) -> str:
    """把标准交易所身份映射为新浪证券代码。"""
    prefix = {
        Exchange.SSE: "sh",
        Exchange.SZSE: "sz",
        Exchange.BSE: "bj",
    }[identifier.exchange]
    return f"{prefix}{identifier.symbol}"


def _normalize_record(record: dict[str, Any]) -> dict[str, str]:
    """把新浪日期与累计因子映射为标准字段。

    ``cumulativeFactor`` 直接保留来源的累计数，不额外归一化为某个基准日，避免不同批次
    因基准选择不同而产生伪修订。
    """
    factor = Decimal(str(record["hfq_factor"]))
    if not factor.is_finite() or factor <= 0:
        raise ValueError("adjustment factor must be finite and positive")
    return {
        "effectiveDate": _record_date(record).isoformat(),
        "cumulativeFactor": str(factor),
    }


def _record_date(record: dict[str, Any]) -> date:
    """读取 AKShare 因子行的生效日期。"""
    value = record["date"]
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _json_default(value: object) -> str:
    """序列化 raw evidence 中的日期和 pandas 标量展示值。"""
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)
