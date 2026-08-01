"""经由 `AKShare` 国证接口提供目录与带日期样本、权重的影子快照。

国证返回的样本日期和权重日期会原样成为来源观察日期；它们不是平台确认的历史生效
区间。目录、样本和权重仍是独立能力，适配器严格校验管理人、指数代码和表头，避免把
其他指数公司或未知列混进国证数据集。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import akshare as ak
import requests

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
from service_data_sync.infrastructure.providers.akshare.csindex_index_snapshot import (
    _index_code,
    _json_default,
    _required_date_text,
    _required_text,
    _schema_fingerprint,
    _six_digit,
)

_ADAPTER_VERSION = "akshare-1.18.81-cnindex-index-snapshot-v3"
_CATALOG_SCHEMA = "quant-v2.index-catalog-snapshot.v1"
_CONSTITUENT_SCHEMA = "quant-v2.index-constituent-observed-snapshot.v1"
_WEIGHT_SCHEMA = "quant-v2.index-weight-close-observed-snapshot.v1"
_CNI_CATALOG_URL = "https://www.cnindex.com.cn/index/indexList"
_CNI_CATALOG_PARAMS = {"channelCode": "-1", "rows": "2000", "pageNum": "1"}
_CNI_CATALOG_MAX_BYTES = 4 * 1024 * 1024
_CNI_CATALOG_FIELDS = (
    "id",
    "docchannel",
    "indexcode",
    "indextype",
    "showcnindex",
    "indexsource",
    "realtimemarket",
    "remark",
    "indexname",
    "indexename",
    "indexfullename",
    "indexfullcname",
    "samplesize",
    "closeingPoint",
    "percent",
    "peStatic",
    "peDynamic",
    "pb",
    "volume",
    "amount",
    "totalMarketValue",
    "freeMarketValue",
    "sampleshowdate",
    "prefixmonth",
    "showDetail",
    "dataSource",
)


class _CnindexCatalogSchemaError(ValueError):
    """表示国证目录原始 JSON 结构或冻结字段集发生漂移。"""

    pass


class AkshareCnindexIndexSnapshotAdapter:
    """调用国证目录与样本详情接口，不猜测交易所、有效区间或历史覆盖范围。

    同一批次的日期必须可复核；缺失日期不能用抓取时间替代，否则会错误放大数据时效性。
    """

    provider_id = "akshare-cnindex-index-snapshot"

    def __init__(self, *, request_timeout_seconds: int) -> None:
        """保存一次阻塞 SDK 调用允许占用的最大墙钟时间。"""
        self._request_timeout_seconds = request_timeout_seconds

    def capabilities(self) -> frozenset[str]:
        """声明由固定 AKShare 版本提供的国证目录、样本与权重观察能力。"""
        return frozenset(
            {
                IndexCapability.CATALOG_SNAPSHOT.value,
                IndexCapability.CONSTITUENT_SNAPSHOT.value,
                IndexCapability.WEIGHT_SNAPSHOT.value,
            }
        )

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """获取一批国证来源观察，保留原始证据并将未知交易所显式保留为空。

        来源短暂不可用可重试；列、日期或权重形状异常被标为不可重试的 `schema` 漂移。
        """
        capability, identifier = _request_values(request)
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                if capability is IndexCapability.CATALOG_SNAPSHOT:
                    records, raw_payload = await asyncio.to_thread(
                        _fetch_catalog_records,
                        self._request_timeout_seconds,
                    )
                else:
                    assert identifier is not None
                    frame = await asyncio.to_thread(_fetch_frame, capability, identifier)
                    records = frame.to_dict(orient="records")
                    raw_payload = json.dumps(
                        {
                            "administrator": IndexAdministrator.CNI.value,
                            "capability": capability.value,
                            "indexCode": identifier.code,
                            "records": records,
                        },
                        ensure_ascii=False,
                        default=_json_default,
                        separators=(",", ":"),
                    ).encode()
        except TimeoutError as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "provider request timed out",
                retryable=True,
            ) from error
        except requests.Timeout as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "provider request timed out",
                retryable=True,
            ) from error
        except requests.RequestException as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "provider request failed",
                retryable=True,
            ) from error
        except _CnindexCatalogSchemaError as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "provider index catalog schema changed",
                retryable=False,
            ) from error
        except Exception as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "provider request failed",
                retryable=True,
            ) from error
        if not records:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "provider returned an empty index snapshot",
                retryable=False,
            )
        try:
            payload = _normalize_payload(capability, identifier, records)
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "provider index snapshot schema changed",
                retryable=False,
            ) from error
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=capability.value,
            payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
            observed_at=datetime.now(UTC),
            content_type="application/vnd.quant-v2.index-snapshot+json",
            raw_payload=raw_payload,
            raw_content_type="application/json",
            upstream_source="cnindex",
            adapter_version=_ADAPTER_VERSION,
            schema_fingerprint=_schema_fingerprint(records),
        )


def _request_values(
    request: SourceRequest,
) -> tuple[IndexCapability, IndexIdentifier | None]:
    """解析国证请求的中立能力和指数身份，避免跨管理人误路由。"""
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
    if parameters.get("administrator") != IndexAdministrator.CNI.value:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "cnindex adapter requires administrator CNI",
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
        return capability, IndexIdentifier(IndexAdministrator.CNI, parameters["indexCode"])
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
    """将国证 SDK 函数和固定参数限制在本 adapter 内。"""
    assert identifier is not None
    return ak.index_detail_cni(symbol=identifier.code)


def _fetch_catalog_records(request_timeout_seconds: int) -> tuple[list[dict[str, Any]], bytes]:
    """读取 AKShare 同源国证目录 JSON，冻结字段名而不依赖其失配的列重命名实现。

    `AKShare` 1.18.81 的 ``index_all_cni`` 仍把 26 个原始字段强行命名为 25 列，导致
    `DataFrame` 在 adapter 之前失败。这里复用该固定版本函数已验证的 URL 和参数，但仅
    接受来源明确返回的字典字段；不根据位置补列，也不把未知新增字段静默丢弃。
    """
    response = requests.get(
        _CNI_CATALOG_URL,
        params=_CNI_CATALOG_PARAMS,
        timeout=float(request_timeout_seconds),
    )
    response.raise_for_status()
    raw_payload = bytes(response.content)
    if not raw_payload or len(raw_payload) > _CNI_CATALOG_MAX_BYTES:
        raise _CnindexCatalogSchemaError("cnindex catalog payload size is invalid")
    try:
        decoded = json.loads(raw_payload)
    except json.JSONDecodeError as error:
        raise _CnindexCatalogSchemaError("cnindex catalog payload is not JSON") from error
    return _catalog_records_from_payload(decoded), raw_payload


def _catalog_records_from_payload(payload: object) -> list[dict[str, Any]]:
    """从原始国证 envelope 提取带完整冻结字段集的字典目录记录。

    2026-08-01 的真实响应含 1450 个字典行、没有字段头数组；字段集合本身是唯一可审计的
    schema 证据。不能沿用 SDK 的位置列名，也不能为了兼容猜测性地接受列表行。
    """
    if not isinstance(payload, dict):
        raise _CnindexCatalogSchemaError("cnindex catalog root must be an object")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise _CnindexCatalogSchemaError("cnindex catalog has no data object")
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise _CnindexCatalogSchemaError("cnindex catalog has no rows array")

    records: list[dict[str, Any]] = []
    expected_fields = frozenset(_CNI_CATALOG_FIELDS)
    for row in rows:
        if not isinstance(row, dict):
            raise _CnindexCatalogSchemaError("cnindex catalog row type changed")
        if frozenset(row) != expected_fields:
            raise _CnindexCatalogSchemaError("cnindex catalog record fields changed")
        records.append({str(key): value for key, value in row.items()})
    if not records:
        raise _CnindexCatalogSchemaError("cnindex catalog contains no records")
    return records


def _normalize_payload(
    capability: IndexCapability,
    identifier: IndexIdentifier | None,
    records: list[dict[str, Any]],
) -> dict[str, object]:
    """将国证来源列归一化为中立 JSON，同时保留无法解析的交易所空值。

    当前详情中的成分与权重共用同一来源日期，但仍按不同能力分别发布，不能互相补全。
    """
    if capability is IndexCapability.CATALOG_SNAPSHOT:
        return {
            "schema": _CATALOG_SCHEMA,
            "administrator": IndexAdministrator.CNI.value,
            "records": [_normalize_catalog_entry(record) for record in records],
        }
    assert identifier is not None
    rows = [_normalize_detail_row(record) for record in records]
    source_dates = {row["sourceAsOfDate"] for row in rows}
    if len(source_dates) != 1:
        raise ValueError("index detail snapshot must expose one source date")
    source_date = next(iter(source_dates))
    if capability is IndexCapability.CONSTITUENT_SNAPSHOT:
        return {
            "schema": _CONSTITUENT_SCHEMA,
            "administrator": identifier.administrator.value,
            "indexCode": identifier.code,
            "sourceAsOfDate": source_date,
            "constituents": [
                {
                    "sourceSymbol": row["sourceSymbol"],
                    "sourceName": row["sourceName"],
                    # 固定 AKShare 详情未提供交易所；禁止按股票代码前缀猜测。
                    "sourceExchange": None,
                    "sourceIndustry": row["sourceIndustry"],
                }
                for row in rows
            ],
        }
    return {
        "schema": _WEIGHT_SCHEMA,
        "administrator": identifier.administrator.value,
        "indexCode": identifier.code,
        "weightDate": source_date,
        "weightType": "OBSERVED",
        "weights": [
            {
                "sourceSymbol": row["sourceSymbol"],
                "sourceName": row["sourceName"],
                "sourceExchange": None,
                "weightValue": row["weightValue"],
                "sourceTotalMarketCap": row["sourceTotalMarketCap"],
                "sourceIndustry": row["sourceIndustry"],
            }
            for row in rows
        ],
    }


def _normalize_catalog_entry(record: dict[str, Any]) -> dict[str, object]:
    """映射目录稳定身份和样本数，不把 AKShare 已换算数值伪装成未经验证单位。"""
    entry = IndexCatalogEntry(
        identifier=IndexIdentifier(IndexAdministrator.CNI, _index_code(record["indexcode"])),
        name=_required_text(record["indexname"]),
    )
    sample_count = _optional_count(record.get("samplesize"))
    return {
        "indexCode": entry.identifier.code,
        "indexName": entry.name,
        "constituentCount": sample_count,
    }


def _normalize_detail_row(record: dict[str, Any]) -> dict[str, str | None]:
    """映射带日期的样本明细，未知单位和交易所不作补全。"""
    constituent = IndexConstituentObservation(
        source_symbol=_six_digit(record["样本代码"]),
        source_name=_required_text(record["样本简称"]),
        source_exchange=None,
    )
    weight = Decimal(_required_text(record["权重"]))
    if not weight.is_finite() or not Decimal("0") <= weight <= Decimal("100"):
        raise ValueError("weight must be a finite percentage from 0 to 100")
    return {
        "sourceAsOfDate": _required_date_text(record["日期"]),
        "sourceSymbol": constituent.source_symbol,
        "sourceName": constituent.source_name,
        "sourceIndustry": _optional_text(record.get("所属行业")),
        "sourceTotalMarketCap": _optional_decimal_text(record.get("总市值")),
        "weightValue": format(weight, "f"),
    }


def _optional_text(value: object) -> str | None:
    """将缺失、空白和 pandas 空值文本统一为真实空值。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return None if not normalized or normalized.lower() == "nan" else normalized


def _optional_count(value: object) -> int | None:
    """解析可选样本数，只接受数值上严格等于整数的非负来源值。"""
    normalized = _optional_text(value)
    if normalized is None:
        return None
    if isinstance(value, bool):
        raise ValueError("sample count must be a non-negative integer")
    try:
        decimal = Decimal(normalized)
    except InvalidOperation as error:
        raise ValueError("sample count must be a non-negative integer") from error
    if not decimal.is_finite() or decimal < 0 or decimal != decimal.to_integral_value():
        raise ValueError("sample count must be a non-negative integer")
    return int(decimal)


def _optional_decimal_text(value: object) -> str | None:
    """将来源可选数值保留为精确文本，单位未验证时不提供标准单位声明。"""
    normalized = _optional_text(value)
    if normalized is None:
        return None
    decimal = Decimal(normalized)
    if not decimal.is_finite() or decimal < 0:
        raise ValueError("source decimal must be finite and non-negative")
    return format(decimal, "f")
