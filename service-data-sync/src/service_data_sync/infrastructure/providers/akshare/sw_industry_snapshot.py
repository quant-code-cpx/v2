"""经由 AKShare 固定版本获取乐咕申万三级分类与估值快照。"""

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

_CAPABILITY = "sector.sw.snapshot.raw"
_SCHEMA = "quant-v2.sw-industry-snapshot.v1"
_SCHEME = "sw.industry"
_ADAPTER_VERSION = "akshare-1.18.78-legulegu-sw-overview-v1"
_UPSTREAM_SOURCE = "legulegu.sw-industry-overview"
_METHODOLOGY_CODE = "legulegu-sw-industry-overview"
_METHODOLOGY_VERSION = 1
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_BASE_COLUMNS = frozenset(
    {
        "行业代码",
        "行业名称",
        "成份个数",
        "静态市盈率",
        "TTM(滚动)市盈率",
        "市净率",
        "静态股息率",
    }
)
_LEVEL_COLUMNS = {
    1: _BASE_COLUMNS,
    2: _BASE_COLUMNS | {"上级行业"},
    3: _BASE_COLUMNS | {"上级行业"},
}
_SEMANTIC_SPEC_SHA256 = hashlib.sha256(
    json.dumps(
        {
            "adapter": _ADAPTER_VERSION,
            "dividendYieldSourceUnit": "percent",
            "finality": "provider_observation",
            "levels": [1, 2, 3],
            "source": _UPSTREAM_SOURCE,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()


class AkshareSwIndustrySnapshotAdapter:
    """把三个无参数申万接口合并为一个原子的 provider-neutral 快照。"""

    provider_id = "akshare-legulegu-sw-industry"

    def __init__(self, *, request_timeout_seconds: int) -> None:
        """保存一次三级批量调用的墙钟超时。"""
        if request_timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        self._request_timeout_seconds = request_timeout_seconds

    def capabilities(self) -> frozenset[str]:
        """仅声明完整三级 taxonomy 与估值快照，不声称历史查询能力。"""
        return frozenset({_CAPABILITY})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """按当天观测日调用三个真实 AKShare 函数并冻结字段 fingerprint。"""
        snapshot_date = _request_snapshot_date(request)
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                frames = await asyncio.to_thread(_fetch_frames)
        except TimeoutError as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "SW provider request timed out",
                retryable=True,
            ) from error
        except Exception as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "SW provider request failed",
                retryable=True,
            ) from error
        try:
            levels: list[dict[str, object]] = []
            raw_levels: list[dict[str, object]] = []
            fingerprint_parts: list[str] = []
            for level, frame in enumerate(frames, start=1):
                if frame.empty:
                    raise ValueError("SW provider returned an empty level")
                columns = tuple(str(column) for column in frame.columns)
                if frozenset(columns) != _LEVEL_COLUMNS[level]:
                    raise ValueError("SW provider columns changed")
                fingerprint_parts.extend(f"{level}:{column}" for column in sorted(columns))
                records = frame.to_dict(orient="records")
                levels.append(
                    {
                        "level": level,
                        "items": [_normalize_record(record, level=level) for record in records],
                    }
                )
                raw_levels.append({"level": level, "columns": columns, "records": records})
            schema_fingerprint = hashlib.sha256(
                json.dumps(fingerprint_parts, ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest()
        except (InvalidOperation, KeyError, TypeError, ValueError) as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SW provider schema changed",
                retryable=False,
            ) from error
        payload = json.dumps(
            {
                "schema": _SCHEMA,
                "scheme": _SCHEME,
                "snapshotDate": snapshot_date.isoformat(),
                "methodology": {
                    "code": _METHODOLOGY_CODE,
                    "version": _METHODOLOGY_VERSION,
                    "status": "source_reported",
                    "upstreamSource": _UPSTREAM_SOURCE,
                    "semanticSpecSha256": _SEMANTIC_SPEC_SHA256,
                },
                "levels": levels,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        raw_payload = json.dumps(
            {
                "akshareVersion": ak.__version__,
                "functions": [
                    "sw_index_first_info",
                    "sw_index_second_info",
                    "sw_index_third_info",
                ],
                "snapshotDate": snapshot_date.isoformat(),
                "levels": raw_levels,
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
            content_type="application/vnd.quant-v2.sw-industry-snapshot+json",
            raw_payload=raw_payload,
            raw_content_type="application/json",
            upstream_source=_UPSTREAM_SOURCE,
            adapter_version=_ADAPTER_VERSION,
            schema_fingerprint=schema_fingerprint,
        )


def _request_snapshot_date(request: SourceRequest) -> date:
    """只接受当天快照，历史日期必须从已归档标准载荷重放。"""
    if request.capability != _CAPABILITY:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "unsupported capability",
            retryable=False,
        )
    parameters = dict(request.parameters)
    try:
        snapshot_date = date.fromisoformat(parameters["snapshotDate"])
    except (KeyError, ValueError) as error:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "invalid SW snapshot date",
            retryable=False,
        ) from error
    if snapshot_date != datetime.now(_SHANGHAI).date():
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "historical SW snapshots require archived replay",
            retryable=False,
        )
    return snapshot_date


def _fetch_frames() -> tuple[Any, Any, Any]:
    """调用固定版本源码中真实存在的三个无参数申万分类函数。"""
    return (
        ak.sw_index_first_info(),
        ak.sw_index_second_info(),
        ak.sw_index_third_info(),
    )


def _normalize_record(record: dict[str, Any], *, level: int) -> dict[str, object]:
    """将中文供应商字段映射为中立快照字段并保留来源百分比单位。"""
    return {
        "code": _required_text(record, "行业代码"),
        "name": _required_text(record, "行业名称"),
        "parentName": None if level == 1 else _required_text(record, "上级行业"),
        "componentCount": _required_count(record, "成份个数"),
        "staticPe": _optional_decimal_text(record.get("静态市盈率")),
        "ttmPe": _optional_decimal_text(record.get("TTM(滚动)市盈率")),
        "pb": _optional_decimal_text(record.get("市净率")),
        "dividendYieldPercent": _optional_decimal_text(record.get("静态股息率")),
    }


def _required_text(record: dict[str, Any], key: str) -> str:
    """读取去除首尾空白后的非空身份文本。"""
    value = record.get(key)
    normalized = "" if value is None else str(value).strip()
    if not normalized or normalized.lower() == "nan":
        raise ValueError(f"{key} is required")
    return normalized


def _required_count(record: dict[str, Any], key: str) -> int:
    """把成分数转换为非负整数并拒绝缺失或小数。"""
    value = Decimal(_required_text(record, key))
    if not value.is_finite() or value < 0 or value != value.to_integral():
        raise ValueError(f"{key} must be a non-negative integer")
    return int(value)


def _optional_decimal_text(value: object) -> str | None:
    """把可空估值转换为精确文本并拒绝无穷值。"""
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized or normalized.lower() in {"nan", "none"}:
        return None
    decimal_value = Decimal(normalized)
    if not decimal_value.is_finite():
        raise ValueError("SW valuation must be finite")
    return str(decimal_value)


def _json_default(value: object) -> str:
    """将 pandas/numpy 日期或标量转换为 raw evidence 可读文本。"""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)
