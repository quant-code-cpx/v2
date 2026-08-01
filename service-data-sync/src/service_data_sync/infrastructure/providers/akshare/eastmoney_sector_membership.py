"""经由 `AKShare` 东财板块成分接口获取当前集合观测的适配器。

东财只提供抓取时刻的成分集合，本模块不能凭两次快照差集声称某证券“加入”或“移除”。
目标日期必须是上海当前日；历史成员关系只能从已归档快照重放，避免把今天的组成回填
到过去的交易日。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import akshare as ak

from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.domain.sector import SectorIdentifier, SectorScheme

_CAPABILITY = "sector.membership.snapshot.raw"
_SCHEMA = "quant-v2.sector-membership-snapshot.v1"
_ADAPTER_VERSION = "akshare-1.18.81-eastmoney-sector-membership-v1"
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class AkshareEastmoneySectorMembershipAdapter:
    """调用东财行业或概念当前成分接口，不把当前集合伪装为真实变更事件。

    输出只含来源证券代码和名称；即时行情、涨跌幅等列不属于成员关系事实。
    """

    provider_id = "akshare-eastmoney-sector-membership"

    def __init__(self, *, request_timeout_seconds: int) -> None:
        """保存一次阻塞 SDK 调用允许占用的最大墙钟时间。"""
        self._request_timeout_seconds = request_timeout_seconds

    def capabilities(self) -> frozenset[str]:
        """仅声明当前板块成分完整快照能力。"""
        return frozenset({_CAPABILITY})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """拉取一个板块全部来源成员，隔离超时、断连和 `schema` 漂移。

        当前日期按上海时区而非 worker 所在时区判断，避免北京时间零点附近误把新旧快照
        标到错误的观测日。
        """
        identifier, observation_date = _request_values(request)
        if observation_date != datetime.now(_SHANGHAI).date():
            # 供应商没有历史成员查询能力，拒绝请求比伪造历史集合更安全。
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "sector membership source supports only the current Shanghai date",
                retryable=False,
            )
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                frame = await asyncio.to_thread(_fetch_members, identifier)
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
                "provider returned no sector members",
                retryable=False,
            )
        try:
            raw_records = frame.to_dict(orient="records")
            members = [_normalize_member(record) for record in raw_records]
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "provider sector membership schema changed",
                retryable=False,
            ) from error
        payload = json.dumps(
            {
                "schema": _SCHEMA,
                "sectorScheme": identifier.scheme.value,
                "sector": identifier.code,
                "members": members,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        raw_payload = json.dumps(
            {
                "sectorScheme": identifier.scheme.value,
                "sector": identifier.code,
                "records": raw_records,
            },
            ensure_ascii=False,
            default=_json_default,
            separators=(",", ":"),
        ).encode()
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=payload,
            observed_at=datetime.now(UTC),
            content_type="application/vnd.quant-v2.sector-membership+json",
            raw_payload=raw_payload,
            raw_content_type="application/json",
            upstream_source="eastmoney",
            adapter_version=_ADAPTER_VERSION,
            schema_fingerprint=_schema_fingerprint(raw_records),
        )


def _request_values(request: SourceRequest) -> tuple[SectorIdentifier, date]:
    """解析中立身份和目标日，拒绝错误能力或无法诚实支持的历史请求。"""
    if request.capability != _CAPABILITY:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "unsupported capability",
            retryable=False,
        )
    parameters = dict(request.parameters)
    try:
        return (
            SectorIdentifier(
                scheme=SectorScheme(parameters["sectorScheme"]), code=parameters["sector"]
            ),
            date.fromisoformat(parameters["observationDate"]),
        )
    except (KeyError, ValueError) as error:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "invalid sector membership request",
            retryable=False,
        ) from error


def _fetch_members(identifier: SectorIdentifier) -> Any:
    """将中立板块身份映射到唯一允许出现 AKShare SDK 调用的位置。"""
    if identifier.scheme is SectorScheme.EASTMONEY_INDUSTRY:
        return ak.stock_board_industry_cons_em(symbol=identifier.code)
    return ak.stock_board_concept_cons_em(symbol=identifier.code)


def _normalize_member(record: dict[str, Any]) -> dict[str, str]:
    """仅映射来源代码和名称；板块即时行情字段不属于成员 canonical 事实。

    ``zfill(6)`` 只恢复 `pandas` 数值化丢失的前导零，不把其他长度的代码补成合法证券。
    """
    symbol = str(record["代码"]).zfill(6)
    name = str(record["名称"]).strip()
    if len(symbol) != 6 or not symbol.isdigit() or not name:
        raise ValueError("provider member identity is invalid")
    return {"sourceSymbol": symbol, "sourceName": name}


def _schema_fingerprint(records: list[dict[str, Any]]) -> str:
    """以原始列集合哈希记录上游 schema，便于 drift 审计而不泄漏完整 raw。"""
    keys = sorted({str(key) for record in records for key in record})
    return hashlib.sha256(
        json.dumps(keys, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _json_default(value: object) -> str:
    """归档 pandas 日期、数值等非原生 JSON 值时保留其文本表示。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)
