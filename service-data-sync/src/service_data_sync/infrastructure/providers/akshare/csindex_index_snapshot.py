"""经由 `AKShare` 中证接口提供目录、当前成分和当前权重的影子快照。

这些响应描述抓取时的供应商观察，不等同于指数公司的正式历史生效文件。目录不携带
指数代码，成分和权重必须绑定到明确的中证指数；当前成分没有来源日期时输出空值，
防止下游伪造 `PIT`（时点）有效区间。权重则要求同一批行暴露一个统一的收盘日期。
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
from service_data_sync.domain.index import (
    IndexAdministrator,
    IndexCapability,
    IndexCatalogEntry,
    IndexConstituentObservation,
    IndexIdentifier,
)

_ADAPTER_VERSION = "akshare-1.18.78-csindex-index-snapshot-v1"
_CATALOG_SCHEMA = "quant-v2.index-catalog-snapshot.v1"
_CONSTITUENT_SCHEMA = "quant-v2.index-constituent-observed-snapshot.v1"
_WEIGHT_SCHEMA = "quant-v2.index-weight-close-observed-snapshot.v1"


class AkshareCsindexIndexSnapshotAdapter:
    """调用中证目录、当前成分和权重接口，不伪造历史或正式生效事实。

    每一能力独立产生标准批次和来源指纹，不能用目录结果补充成分或用权重日期倒推名单。
    """

    provider_id = "akshare-csindex-index-snapshot"

    def __init__(self, *, request_timeout_seconds: int) -> None:
        """保存一次阻塞 SDK 调用可占用的最大墙钟时间。"""
        self._request_timeout_seconds = request_timeout_seconds

    def capabilities(self) -> frozenset[str]:
        """声明已由固定 AKShare 版本封装的三项中证影子观察能力。"""
        return frozenset(
            {
                IndexCapability.CATALOG_SNAPSHOT.value,
                IndexCapability.CONSTITUENT_SNAPSHOT.value,
                IndexCapability.WEIGHT_SNAPSHOT.value,
            }
        )

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """获取一批标准化快照，同时保留完整来源记录和 `schema` 指纹。

        供应商短暂不可用可重试；表头、日期或数值形状变化则标记为不可重试的口径漂移。
        """
        capability, identifier = _request_values(request)
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                frame = await asyncio.to_thread(_fetch_frame, capability, identifier)
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
                "provider returned an empty index snapshot",
                retryable=False,
            )
        try:
            records = frame.to_dict(orient="records")
            payload = _normalize_payload(capability, identifier, records)
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "provider index snapshot schema changed",
                retryable=False,
            ) from error
        raw_payload = json.dumps(
            {
                "administrator": IndexAdministrator.CSI.value,
                "capability": capability.value,
                "indexCode": None if identifier is None else identifier.code,
                "records": records,
            },
            ensure_ascii=False,
            default=_json_default,
            separators=(",", ":"),
        ).encode()
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=capability.value,
            payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
            observed_at=datetime.now(UTC),
            content_type="application/vnd.quant-v2.index-snapshot+json",
            raw_payload=raw_payload,
            raw_content_type="application/json",
            upstream_source="csindex",
            adapter_version=_ADAPTER_VERSION,
            schema_fingerprint=_schema_fingerprint(records),
        )


def _request_values(
    request: SourceRequest,
) -> tuple[IndexCapability, IndexIdentifier | None]:
    """解析中立能力和中证身份，拒绝不属于本 adapter 的来源或请求形状。"""
    try:
        capability = IndexCapability(request.capability)
    except ValueError as error:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "unsupported capability",
            retryable=False,
        ) from error
    if capability not in {
        IndexCapability.CATALOG_SNAPSHOT,
        IndexCapability.CONSTITUENT_SNAPSHOT,
        IndexCapability.WEIGHT_SNAPSHOT,
    }:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "unsupported capability",
            retryable=False,
        )
    parameters = dict(request.parameters)
    administrator = parameters.get("administrator")
    if administrator != IndexAdministrator.CSI.value:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "csindex adapter requires administrator CSI",
            retryable=False,
        )
    if capability is IndexCapability.CATALOG_SNAPSHOT:
        if "indexCode" in parameters:
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "catalog snapshot must not include indexCode",
                retryable=False,
            )
        return capability, None
    try:
        return capability, IndexIdentifier(IndexAdministrator.CSI, parameters["indexCode"])
    except KeyError as error:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "index snapshot requires indexCode",
            retryable=False,
        ) from error
    except ValueError as error:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "index snapshot indexCode is invalid",
            retryable=False,
        ) from error


def _fetch_frame(capability: IndexCapability, identifier: IndexIdentifier | None) -> Any:
    """将 SDK 函数和参数限制在唯一 adapter 边界内。"""
    if capability is IndexCapability.CATALOG_SNAPSHOT:
        return ak.index_csindex_all()
    assert identifier is not None
    if capability is IndexCapability.CONSTITUENT_SNAPSHOT:
        return ak.index_stock_cons_csindex(symbol=identifier.code)
    return ak.index_stock_cons_weight_csindex(symbol=identifier.code)


def _normalize_payload(
    capability: IndexCapability,
    identifier: IndexIdentifier | None,
    records: list[dict[str, Any]],
) -> dict[str, object]:
    """将固定版中文列名转为中立 JSON，且不把观察快照升级为历史有效事实。

    每个分支只输出其所属的指数数据集，避免当前成员、权重和目录间不受证据支持的拼接。
    """
    if capability is IndexCapability.CATALOG_SNAPSHOT:
        entries = [_normalize_catalog_entry(record) for record in records]
        return {
            "schema": _CATALOG_SCHEMA,
            "administrator": IndexAdministrator.CSI.value,
            "records": entries,
        }
    assert identifier is not None
    if capability is IndexCapability.CONSTITUENT_SNAPSHOT:
        constituents = [_normalize_constituent(record) for record in records]
        return {
            "schema": _CONSTITUENT_SCHEMA,
            "administrator": identifier.administrator.value,
            "indexCode": identifier.code,
            # 上游当前列表没有历史日期参数；空值阻止后续代码伪造 PIT 有效区间。
            "sourceAsOfDate": None,
            "constituents": constituents,
        }
    weights = [_normalize_weight(record) for record in records]
    weight_dates = {weight["weightDate"] for weight in weights}
    if len(weight_dates) != 1:
        raise ValueError("weight snapshot must expose one source date")
    return {
        "schema": _WEIGHT_SCHEMA,
        "administrator": identifier.administrator.value,
        "indexCode": identifier.code,
        "weightDate": next(iter(weight_dates)),
        "weightType": "OFFICIAL_CLOSE",
        "weights": weights,
    }


def _normalize_catalog_entry(record: dict[str, Any]) -> dict[str, object]:
    """映射目录的稳定身份和可选元数据，不猜测缺失生命周期字段。"""
    entry = IndexCatalogEntry(
        identifier=IndexIdentifier(IndexAdministrator.CSI, _six_digit(record["指数代码"])),
        name=_required_text(record["指数简称"]),
    )
    return {
        "indexCode": entry.identifier.code,
        "indexName": entry.name,
        "fullName": _optional_text(record.get("指数全称")),
        "baseDate": _optional_date_text(record.get("基日")),
        "baseValue": _optional_decimal_text(record.get("基点")),
        "publishedDate": _optional_date_text(record.get("发布日期")),
        "constituentCount": _optional_non_negative_int(record.get("样本数量")),
    }


def _normalize_constituent(record: dict[str, Any]) -> dict[str, str | None]:
    """映射来源证券身份，不用名称推断交易所或平台 security_id。"""
    constituent = IndexConstituentObservation(
        source_symbol=_six_digit(record["成分券代码"]),
        source_name=_required_text(record["成分券名称"]),
        source_exchange=_required_text(record["交易所"]),
    )
    return {
        "sourceSymbol": constituent.source_symbol,
        "sourceName": constituent.source_name,
        "sourceExchange": constituent.source_exchange,
    }


def _normalize_weight(record: dict[str, Any]) -> dict[str, str]:
    """映射官方收盘权重；缺日期、非百分比或越界值都必须触发隔离。"""
    weight = Decimal(_required_text(record["权重"]))
    if not weight.is_finite() or not Decimal("0") <= weight <= Decimal("100"):
        raise ValueError("weight must be a finite percentage from 0 to 100")
    return {
        "sourceSymbol": _six_digit(record["成分券代码"]),
        "sourceName": _required_text(record["成分券名称"]),
        "sourceExchange": _required_text(record["交易所"]),
        "weightDate": _required_date_text(record["日期"]),
        "weightValue": format(weight, "f"),
    }


def _six_digit(value: object) -> str:
    """保留来源代码前导零，并拒绝超出证券或指数代码形状的值。"""
    normalized = _required_text(value)
    if normalized.isdigit() and len(normalized) <= 6:
        normalized = normalized.zfill(6)
    if len(normalized) != 6 or not normalized.isdigit():
        raise ValueError("source code must be six digits")
    return normalized


def _required_text(value: object) -> str:
    """读取来源必填文本，拒绝空白和 pandas 空值文本。"""
    normalized = _optional_text(value)
    if normalized is None:
        raise ValueError("required source field is missing")
    return normalized


def _optional_text(value: object) -> str | None:
    """将缺失、空白和 pandas 空值统一为真实空值。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return None if not normalized or normalized.lower() == "nan" else normalized


def _required_date_text(value: object) -> str:
    """解析来源日期并标准化为 ISO 日历字符串。"""
    normalized = _optional_date_text(value)
    if normalized is None:
        raise ValueError("required source date is missing")
    return normalized


def _optional_date_text(value: object) -> str | None:
    """接受日期、时间戳或 ISO 文本，并统一输出无时区业务日期。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    normalized = _optional_text(value)
    if normalized is None:
        return None
    return date.fromisoformat(normalized[:10]).isoformat()


def _optional_decimal_text(value: object) -> str | None:
    """把可选来源数值转为精确十进制文本，避免二进制浮点进入 canonical 边界。"""
    normalized = _optional_text(value)
    if normalized is None:
        return None
    decimal = Decimal(normalized)
    if not decimal.is_finite():
        raise ValueError("source decimal must be finite")
    return format(decimal, "f")


def _optional_non_negative_int(value: object) -> int | None:
    """解析可选样本数量，拒绝小数、布尔值和负数。"""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("source count must be a non-negative integer")
    normalized = _optional_text(value)
    if normalized is None:
        return None
    result = int(normalized)
    if result < 0 or str(result) != normalized:
        raise ValueError("source count must be a non-negative integer")
    return result


def _schema_fingerprint(records: list[dict[str, Any]]) -> str:
    """以来源列集合哈希记录 schema 漂移证据，不泄漏记录内容。"""
    keys = sorted({str(key) for record in records for key in record})
    return hashlib.sha256(
        json.dumps(keys, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _json_default(value: object) -> str:
    """归档 pandas 日期、数值等非原生 JSON 值时保留其文本表示。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)
