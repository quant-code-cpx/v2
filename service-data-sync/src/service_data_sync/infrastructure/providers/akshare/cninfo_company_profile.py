"""经由 `AKShare SDK` 获取巨潮个股当前公司概况的适配器。

公司概况是当前披露状态，不承诺历史回溯；适配器把巨潮中文展示字段裁剪为平台许可的
稳定字段，空白和 `pandas` 空值统一为真实空值。名称缺失被视为 `schema` 问题，其他可选
字段不能被临时缺失误解为“应清空既有公司资料”。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any

import akshare as ak

from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.domain.equity import EquityIdentifier

_CAPABILITY = "equity.profile"
_SCHEMA = "quant-v2.equity-profile.v1"


class AkshareCninfoCompanyProfileAdapter:
    """读取巨潮当前公司概况并裁剪为平台允许的标准字段。

    它不以代码相似性推断市场；请求中的平台证券身份是唯一归属依据。
    """

    provider_id = "akshare-cninfo-company-profile"

    def __init__(self, *, request_timeout_seconds: int) -> None:
        """保存阻塞式 AKShare 公司概况请求的墙钟超时。"""
        self._request_timeout_seconds = request_timeout_seconds

    def capabilities(self) -> frozenset[str]:
        """声明单一公司概况能力。"""
        return frozenset({_CAPABILITY})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """获取一只证券当前公司概况并输出中立标准载荷。"""
        identifier = _request_identifier(request)
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                frame = await asyncio.to_thread(
                    ak.stock_profile_cninfo,
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
        if frame.empty:
            raise ProviderError(
                ProviderErrorCode.SCHEMA, "provider returned no company profile", retryable=False
            )
        try:
            raw_records = frame.to_dict(orient="records")
            # 当前接口应只给出一份概况；首行作为本次观测，其余行仍保留在失败证据中。
            profile = _normalize_record(raw_records[0])
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA, "provider company-profile schema changed", retryable=False
            ) from error
        payload = json.dumps(
            {
                "schema": _SCHEMA,
                "instrument": identifier.qualified_symbol,
                "profile": profile,
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
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=payload,
            observed_at=datetime.now(UTC),
            content_type="application/vnd.quant-v2.equity-profile+json",
            raw_payload=raw_payload,
            raw_content_type="application/json",
            upstream_source="cninfo-company-profile",
            adapter_version="akshare-1.18.78-v1",
            schema_fingerprint=hashlib.sha256(
                json.dumps(sorted(raw_records[0]), ensure_ascii=False).encode()
            ).hexdigest(),
        )


def _request_identifier(request: SourceRequest) -> EquityIdentifier:
    """解析只包含标准证券身份的公司概况请求。"""
    if request.capability != _CAPABILITY:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST, "unsupported capability", retryable=False
        )
    parameters = dict(request.parameters)
    try:
        return EquityIdentifier.parse(parameters["instrument"])
    except (KeyError, ValueError) as error:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST, "invalid company-profile request", retryable=False
        ) from error


def _normalize_record(record: dict[str, Any]) -> dict[str, str | None]:
    """将巨潮中文字段映射为稳定公司概况结构。

    地址、经营范围等文本不做语义改写；仅清理空值，避免适配器擅自解释公司披露。
    """
    return {
        "companyName": _required_text(record.get("公司名称")),
        "englishName": _optional_text(record.get("英文名称")),
        "industry": _optional_text(record.get("所属行业")),
        "legalRepresentative": _optional_text(record.get("法人代表")),
        "establishedOn": _date_text(record.get("成立日期")),
        "website": _optional_text(record.get("官方网站")),
        "email": _optional_text(record.get("电子邮箱")),
        "phone": _optional_text(record.get("联系电话")),
        "registeredAddress": _optional_text(record.get("注册地址")),
        "officeAddress": _optional_text(record.get("办公地址")),
        "mainBusiness": _optional_text(record.get("主营业务")),
        "businessScope": _optional_text(record.get("经营范围")),
        "summary": _optional_text(record.get("机构简介")),
    }


def _required_text(value: object) -> str:
    """读取非空供应商文本。"""
    normalized = _optional_text(value)
    if normalized is None:
        raise ValueError("company name is missing")
    return normalized


def _optional_text(value: object) -> str | None:
    """将 pandas 空值和空白文本映射为真实空值。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return None if not normalized or normalized.lower() in {"nan", "nat", "none"} else normalized


def _date_text(value: object) -> str | None:
    """将可空 pandas 日期或文本转换为 ISO 日期。"""
    normalized = _optional_text(value)
    if normalized is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    compact = normalized.replace("-", "")
    if len(compact) == 8 and compact.isdigit():
        return date(int(compact[:4]), int(compact[4:6]), int(compact[6:])).isoformat()
    return date.fromisoformat(normalized[:10]).isoformat()


def _json_default(value: object) -> str:
    """序列化 raw evidence 中的日期和 pandas 标量展示值。"""
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)
