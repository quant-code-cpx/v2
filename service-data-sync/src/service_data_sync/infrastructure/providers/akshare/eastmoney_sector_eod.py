"""经由 `AKShare SDK` 获取东财行业与概念板块收盘后批量横截面的适配器。

每次请求只对应一个分类体系和目标交易日，来源接口本身不支持历史日期过滤，因此
目标日仅作为本次观测分区而非伪造的上游筛选条件。价格、涨跌幅、总市值等字段保留
来源声明值；未确认单位的数值不会被适配器擅自换算。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import akshare as ak

from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.domain.sector import SectorScheme

_CAPABILITY = "sector.quote.eod.snapshot.raw"
_SCHEMA = "quant-v2.sector-eod-snapshot.v1"
_ADAPTER_VERSION = "akshare-1.18.81-eastmoney-sector-eod-v1"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_REQUIRED_COLUMNS = frozenset(
    {
        "排名",
        "板块名称",
        "板块代码",
        "最新价",
        "涨跌额",
        "涨跌幅",
        "总市值",
        "换手率",
        "上涨家数",
        "下跌家数",
        "领涨股票",
        "领涨股票-涨跌幅",
    }
)
_APPROVED_SCHEMA_FINGERPRINTS = frozenset(
    {
        hashlib.sha256(
            json.dumps(
                sorted(_REQUIRED_COLUMNS), ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
    }
)


class AkshareEastmoneySectorEodAdapter:
    """将东财名称批量接口隔离为来源中立 `EOD` 横截面能力。

        完整且获批的列集合是发布前提，未知新增列也会阻断同步，防止供应商语义变更悄悄
    混入既有质量规则。
    """

    provider_id = "akshare-eastmoney-sector-eod"

    def __init__(self, *, request_timeout_seconds: int) -> None:
        """保存阻塞 SDK 调用的有界墙钟超时，避免 worker 无限占用。"""
        self._request_timeout_seconds = request_timeout_seconds

    def capabilities(self) -> frozenset[str]:
        """只声明完整 EOD 批量能力，不提供逐板块补洞或盘中读取。"""
        return frozenset({_CAPABILITY})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """调用一个 scheme 的批量接口，并保留完整 SDK 行作为原始证据。"""
        scheme, trade_date = _request_values(request)
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                frame = await asyncio.to_thread(_fetch_snapshot, scheme=scheme)
        except TimeoutError as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "provider request timed out",
                retryable=True,
            ) from error
        except Exception as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "provider request failed",
                retryable=True,
            ) from error
        if frame.empty:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "provider returned no sector eod quotes",
                retryable=False,
            )
        try:
            columns = tuple(str(column) for column in frame.columns)
            missing_columns = _REQUIRED_COLUMNS.difference(columns)
            if missing_columns:
                raise ValueError("provider eod columns are incomplete")
            schema_fingerprint = _schema_fingerprint(columns)
            if schema_fingerprint not in _APPROVED_SCHEMA_FINGERPRINTS:
                raise ValueError("provider eod schema fingerprint is not approved")
            # 保留完整原始行用于失败证据，标准载荷只暴露经过允许的中立字段。
            raw_records = frame.to_dict(orient="records")
            quotes = [_normalize_record(record) for record in raw_records]
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "provider sector eod schema changed",
                retryable=False,
            ) from error
        payload = json.dumps(
            {
                "schema": _SCHEMA,
                "sectorScheme": scheme.value,
                "tradeDate": trade_date.isoformat(),
                "quotes": quotes,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        raw_payload = json.dumps(
            {
                "sectorScheme": scheme.value,
                "tradeDate": trade_date.isoformat(),
                "columns": columns,
                "records": raw_records,
            },
            ensure_ascii=False,
            default=_json_default,
            separators=(",", ":"),
        ).encode()
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=_CAPABILITY,
            payload=payload,
            observed_at=datetime.now(UTC),
            content_type="application/vnd.quant-v2.sector-eod-snapshot+json",
            raw_payload=raw_payload,
            raw_content_type="application/json",
            upstream_source="eastmoney.board.name",
            adapter_version=_ADAPTER_VERSION,
            schema_fingerprint=schema_fingerprint,
        )


def _schema_fingerprint(columns: tuple[str, ...]) -> str:
    """为完整供应商列集合生成顺序无关 fingerprint，未知附加列也必须显式获批。"""
    return hashlib.sha256(
        json.dumps(sorted(columns), ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _request_values(request: SourceRequest) -> tuple[SectorScheme, date]:
    """解析当天 scheme/date 请求，不伪造上游不具备的历史日期过滤能力。

    东财批量名称接口只返回当前横截面；过去或未来 `tradeDate` 必须在调用 SDK 前
    拒绝，避免把今天观察错误标记为历史 EOD 快照。
    """
    if request.capability != _CAPABILITY:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "unsupported capability",
            retryable=False,
        )
    parameters = dict(request.parameters)
    try:
        scheme = SectorScheme(parameters["sectorScheme"])
        trade_date = date.fromisoformat(parameters["tradeDate"])
    except (KeyError, ValueError) as error:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "invalid sector eod request",
            retryable=False,
        ) from error
    if trade_date != datetime.now(_SHANGHAI).date():
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "historical sector eod snapshots require archived replay",
            retryable=False,
        )
    return scheme, trade_date


def _fetch_snapshot(*, scheme: SectorScheme) -> Any:
    """把分类体系映射为唯一允许的 AKShare 东财批量 name 调用。"""
    if scheme is SectorScheme.EASTMONEY_INDUSTRY:
        return ak.stock_board_industry_name_em()
    return ak.stock_board_concept_name_em()


def _normalize_record(record: dict[str, Any]) -> dict[str, str | int | None]:
    """将供应商中文字段映射为中立名称，并保留未确认单位的原生数值。

    ``changePercent``、``turnoverPercent`` 等名称明确其来源为百分数展示值；消费者不可
    将其误当成已转换为小数比例的字段。
    """
    return {
        "code": _required_text(record, "板块代码"),
        "name": _required_text(record, "板块名称"),
        "latestValue": _optional_decimal_text(record.get("最新价")),
        "changeValue": _optional_decimal_text(record.get("涨跌额")),
        "changePercent": _optional_decimal_text(record.get("涨跌幅")),
        "marketValue": _optional_decimal_text(record.get("总市值")),
        "turnoverPercent": _optional_decimal_text(record.get("换手率")),
        "advancers": _optional_count(record.get("上涨家数")),
        "decliners": _optional_count(record.get("下跌家数")),
        "leaderName": _optional_text(record.get("领涨股票")),
        "leaderChangePercent": _optional_decimal_text(record.get("领涨股票-涨跌幅")),
    }


def _required_text(record: dict[str, Any], key: str) -> str:
    """提取非空文本身份字段，防止无稳定代码行进入 canonical。"""
    value = _optional_text(record.get(key))
    if value is None:
        raise ValueError(f"{key} is required")
    return value


def _optional_text(value: object) -> str | None:
    """将供应商的空文本保留为 `null`，其余文本去除首尾空白。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _optional_decimal_text(value: object) -> str | None:
    """将来源数值变为精确文本，拒绝 NaN、无穷和空白占位。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    decimal_value = Decimal(str(value))
    if not decimal_value.is_finite():
        raise ValueError("provider numeric value must be finite")
    return str(decimal_value)


def _optional_count(value: object) -> int | None:
    """将来源上涨或下跌家数转换为非负整数，拒绝小数和布尔值。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError("provider count must be a non-negative integer")
    decimal_value = Decimal(str(value))
    if (
        not decimal_value.is_finite()
        or decimal_value < 0
        or decimal_value != decimal_value.to_integral()
    ):
        raise ValueError("provider count must be a non-negative integer")
    return int(decimal_value)


def _json_default(value: object) -> str:
    """将 pandas 日期对象转换为原始证据可读的 ISO 文本。"""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)
