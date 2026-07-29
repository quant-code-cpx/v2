"""ETF P0 产品目录和日级状态同步；成功时不留存来源原始字节。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from service_data_sync.application.etf.daily_bar_sync import _archive_batch
from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.dataset_availability import DatasetAvailabilityRepository
from service_data_sync.application.ports.etf_market import EtfReferenceRepository
from service_data_sync.application.ports.market_data import RawPayloadStore
from service_data_sync.domain.etf import EtfDailyStatus, EtfIdentifier, EtfProfile

_MASTER_CAPABILITY = "fund.etf.master"
_MASTER_SCHEMA = "quant-v2.etf-master.v1"
_STATUS_CAPABILITY = "fund.etf.trading_state"
_STATUS_SCHEMA = "quant-v2.etf-trading-state.v1"
_MASTER_DATASET = "fund.etf.profile.reported"
_STATUS_DATASET = "fund.etf.trading_state.reported"


@dataclass(frozen=True, slots=True)
class EtfReferenceSyncResult:
    """返回 ETF 主数据或状态的发布结果，不泄漏 Provider 代码和 raw 存储定位。"""

    etf: EtfIdentifier | None
    data_version: UUID | None
    inserted_count: int
    unchanged_count: int
    availability: str = "available"


class EtfMasterSyncService:
    """同步一个交易所的 ETF 产品目录；目录是观察快照而不是退市状态推断器。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: EtfReferenceRepository,
        raw_payload_store: RawPayloadStore,
        availability_repository: DatasetAvailabilityRepository | None = None,
    ) -> None:
        """接收中立 adapter、产品资料仓储和双证据归档端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store
        self._availability_repository = availability_repository

    async def sync(self, *, venue: str, observation_date: date) -> EtfReferenceSyncResult:
        """抓取一个沪深目录快照；来源不可用或合法空目录返回成功空状态。"""
        if venue not in {"SSE", "SZSE"}:
            raise ValueError("ETF P0 master venue must be SSE or SZSE")
        partition_key = _master_partition_key(venue=venue, observation_date=observation_date)
        try:
            batch = await _fetch(
                source=self._source,
                capability=_MASTER_CAPABILITY,
                parameters=(("venue", venue), ("observationDate", observation_date.isoformat())),
            )
        except ProviderError as error:
            if not _is_empty_result_error(error):
                raise
            return _availability_result(
                repository=self._availability_repository,
                dataset=_MASTER_DATASET,
                partition_key=partition_key,
                availability="source_unavailable",
                reason_code=(
                    "capability_not_configured"
                    if error.code is ProviderErrorCode.INVALID_REQUEST
                    and "unsupported ETF capability" in str(error)
                    else error.code.value
                ),
                provider_id=self._source.provider_id,
                observed_at=datetime.now(UTC),
            )
        profiles = decode_etf_master_batch(batch.payload, venue=venue)
        if not profiles:
            return _availability_result(
                repository=self._availability_repository,
                dataset=_MASTER_DATASET,
                partition_key=partition_key,
                availability="empty",
                reason_code="no_matching_facts",
                provider_id=batch.provider_id,
                observed_at=batch.observed_at,
            )
        published = self._repository.publish_profiles(
            profiles=profiles,
            source=_archive_batch(batch=batch, payload_store=self._raw_payload_store),
        )
        if self._availability_repository is not None:
            self._availability_repository.clear(
                dataset=_MASTER_DATASET,
                partition_key=partition_key,
                cleared_at=datetime.now(UTC),
            )
        return _result(
            published.etf,
            published.data_version,
            published.inserted_count,
            published.unchanged_count,
        )


class EtfStatusSyncService:
    """同步单 ETF 的日级状态，交易、申购和赎回状态不会相互回填。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: EtfReferenceRepository,
        raw_payload_store: RawPayloadStore,
        availability_repository: DatasetAvailabilityRepository | None = None,
    ) -> None:
        """接收中立 adapter、状态仓储和双证据归档端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store
        self._availability_repository = availability_repository

    async def sync(self, *, etf: EtfIdentifier, start: date, end: date) -> EtfReferenceSyncResult:
        """抓取 ETF 有界日级状态窗口；来源不可用或空结果不推断停牌且可成功结束。"""
        if start > end:
            raise ValueError("start must not be after end")
        partition_key = _window_partition_key(etf=etf, start=start, end=end)
        try:
            batch = await _fetch(
                source=self._source,
                capability=_STATUS_CAPABILITY,
                parameters=(
                    ("etf", etf.qualified_key),
                    ("start", start.isoformat()),
                    ("end", end.isoformat()),
                ),
            )
        except ProviderError as error:
            if not _is_empty_result_error(error):
                raise
            return _availability_result(
                repository=self._availability_repository,
                dataset=_STATUS_DATASET,
                partition_key=partition_key,
                availability="source_unavailable",
                reason_code=(
                    "capability_not_configured"
                    if error.code is ProviderErrorCode.INVALID_REQUEST
                    and "unsupported ETF capability" in str(error)
                    else error.code.value
                ),
                provider_id=self._source.provider_id,
                observed_at=datetime.now(UTC),
                etf=etf,
            )
        statuses = decode_etf_status_batch(batch.payload, etf=etf)
        if not statuses:
            return _availability_result(
                repository=self._availability_repository,
                dataset=_STATUS_DATASET,
                partition_key=partition_key,
                availability="empty",
                reason_code="no_matching_facts",
                provider_id=batch.provider_id,
                observed_at=batch.observed_at,
                etf=etf,
            )
        published = self._repository.publish_statuses(
            etf=etf,
            statuses=statuses,
            source=_archive_batch(batch=batch, payload_store=self._raw_payload_store),
        )
        if self._availability_repository is not None:
            self._availability_repository.clear(
                dataset=_STATUS_DATASET,
                partition_key=partition_key,
                cleared_at=datetime.now(UTC),
            )
        return _result(
            published.etf,
            published.data_version,
            published.inserted_count,
            published.unchanged_count,
        )


def decode_etf_master_batch(payload: bytes, *, venue: str) -> tuple[EtfProfile, ...]:
    """解码交易所 ETF 目录，拒绝 venue 漂移、重复上市工具和未治理字段。"""
    decoded = _payload(payload, schema=_MASTER_SCHEMA)
    if decoded.get("venue") != venue:
        raise _schema_error("ETF master venue mismatch")
    _reject_unknown(decoded, {"schema", "venue", "profiles"}, "root")
    values = decoded.get("profiles")
    if not isinstance(values, list):
        raise _schema_error("ETF master payload has no profiles")
    try:
        profiles = tuple(_profile(value, venue=venue) for value in values)
    except (TypeError, ValueError) as error:
        raise _schema_error("ETF master value is invalid") from error
    if len({profile.etf.qualified_key for profile in profiles}) != len(profiles):
        raise _schema_error("ETF master payload has duplicate identifiers")
    return tuple(sorted(profiles, key=lambda item: item.etf.qualified_key))


def decode_etf_status_batch(payload: bytes, *, etf: EtfIdentifier) -> tuple[EtfDailyStatus, ...]:
    """解码一个 ETF 的独立状态维度，拒绝没有官方事件依据的推断状态。"""
    decoded = _payload(payload, schema=_STATUS_SCHEMA)
    if decoded.get("etf") != etf.qualified_key:
        raise _schema_error("ETF status identity mismatch")
    _reject_unknown(decoded, {"schema", "etf", "statuses"}, "root")
    values = decoded.get("statuses")
    if not isinstance(values, list):
        raise _schema_error("ETF status payload has no statuses")
    try:
        statuses = tuple(_status(value, etf=etf) for value in values)
    except (TypeError, ValueError) as error:
        raise _schema_error("ETF status value is invalid") from error
    if len({(item.status_dimension, item.effective_from) for item in statuses}) != len(statuses):
        raise _schema_error("ETF status payload has duplicate dimensions and dates")
    return tuple(sorted(statuses, key=lambda item: (item.effective_from, item.status_dimension)))


async def _fetch(
    *, source: DataSourcePort, capability: str, parameters: tuple[tuple[str, str], ...]
):
    """读取已显式注册的 ETF capability，任何来源 fallback 必须在 adapter registry 外单独审核。"""
    if capability not in source.capabilities():
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            f"unsupported ETF capability: {capability}",
            retryable=False,
        )
    return await source.fetch(SourceRequest(capability=capability, parameters=parameters))


def _profile(value: object, *, venue: str) -> EtfProfile:
    """转换目录中的 ETF 产品资料，缺少的管理人或日期保持空值而不从名称/行情推断。"""
    if not isinstance(value, dict):
        raise ValueError("ETF profile is not an object")
    _reject_unknown(
        value,
        {
            "symbol",
            "etfType",
            "managementMode",
            "managerName",
            "custodianName",
            "establishedOn",
            "listedOn",
            "delistedOn",
            "quoteCurrency",
            "navCurrency",
            "listingStatus",
            "effectiveFrom",
            "sourceTimePrecision",
        },
        "ETF profile",
    )
    return EtfProfile(
        etf=EtfIdentifier(venue=venue, symbol=_required(value, "symbol")),
        etf_type=_required(value, "etfType"),
        management_mode=_required(value, "managementMode"),
        manager_name=_optional_text(value.get("managerName")),
        custodian_name=_optional_text(value.get("custodianName")),
        established_on=_optional_date(value.get("establishedOn")),
        listed_on=_optional_date(value.get("listedOn")),
        delisted_on=_optional_date(value.get("delistedOn")),
        quote_currency=_required(value, "quoteCurrency"),
        nav_currency=_required(value, "navCurrency"),
        listing_status=_required(value, "listingStatus"),
        effective_from=date.fromisoformat(_required(value, "effectiveFrom")),
        source_time_precision=_required(value, "sourceTimePrecision"),
    )


def _status(value: object, *, etf: EtfIdentifier) -> EtfDailyStatus:
    """转换日级状态事件，三个维度需要来源显式提供而不是由状态码猜测。"""
    if not isinstance(value, dict):
        raise ValueError("ETF status is not an object")
    _reject_unknown(
        value, {"dimension", "statusCode", "effectiveFrom", "effectiveTo", "reason"}, "ETF status"
    )
    return EtfDailyStatus(
        etf=etf,
        status_dimension=_required(value, "dimension"),
        status_code=_required(value, "statusCode"),
        effective_from=date.fromisoformat(_required(value, "effectiveFrom")),
        effective_to=_optional_date(value.get("effectiveTo")),
        reason=_optional_text(value.get("reason")),
    )


def _payload(payload: bytes, *, schema: str) -> dict[str, object]:
    """读取指定标准 schema，避免产品目录与状态公告载荷混用。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _schema_error("ETF reference payload is not JSON") from error
    if not isinstance(decoded, dict) or decoded.get("schema") != schema:
        raise _schema_error("unexpected ETF reference schema")
    return decoded


def _result(
    etf: EtfIdentifier | None,
    data_version: UUID,
    inserted_count: int,
    unchanged_count: int,
) -> EtfReferenceSyncResult:
    """将仓储结果转为应用 DTO，避免调用方依赖基础设施实现的具体类型。"""
    return EtfReferenceSyncResult(
        etf=etf,
        data_version=data_version,
        inserted_count=inserted_count,
        unchanged_count=unchanged_count,
    )


def _availability_result(
    *,
    repository: DatasetAvailabilityRepository | None,
    dataset: str,
    partition_key: str,
    availability: str,
    reason_code: str,
    provider_id: str | None,
    observed_at: datetime,
    etf: EtfIdentifier | None = None,
) -> EtfReferenceSyncResult:
    """写入非事实 ETF 参考数据观测，并返回可安全显示为空的统一结果。"""
    if repository is not None:
        repository.record(
            dataset=dataset,
            partition_key=partition_key,
            availability=availability,
            reason_code=reason_code,
            provider_id=provider_id,
            observed_at=observed_at,
        )
    return EtfReferenceSyncResult(
        etf=etf,
        data_version=None,
        inserted_count=0,
        unchanged_count=0,
        availability=availability,
    )


def _is_empty_result_error(error: ProviderError) -> bool:
    """限定可降级的来源类错误；schema 和业务身份漂移仍需失败留证。"""
    return error.code in {
        ProviderErrorCode.UNAVAILABLE,
        ProviderErrorCode.RATE_LIMITED,
        ProviderErrorCode.AUTHENTICATION,
        ProviderErrorCode.INVALID_REQUEST,
    }


def _master_partition_key(*, venue: str, observation_date: date) -> str:
    """把 ETF 目录空状态绑定到交易所与观测日期，禁止跨日复用。"""
    return f"{venue}:{observation_date.isoformat()}"


def _window_partition_key(*, etf: EtfIdentifier, start: date, end: date) -> str:
    """把日级状态空观测绑定到 ETF 与精确请求窗口。"""
    return f"{etf.qualified_key}:{start.isoformat()}:{end.isoformat()}"


def _required(value: dict[str, object], key: str) -> str:
    """读取非空文本，拒绝常见 pandas 缺失字面量进入稳定产品和状态字段。"""
    normalized = _optional_text(value.get(key))
    if normalized is None:
        raise ValueError(f"{key} is required")
    return normalized


def _optional_text(value: object) -> str | None:
    """保持真实空值，避免将空白或 `NaN` 保存为管理人或状态原因。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return None if not normalized or normalized.lower() in {"nan", "none", "nat"} else normalized


def _optional_date(value: object) -> date | None:
    """解析可选 ISO 日期，缺失日期不会用目录抓取时间替代。"""
    normalized = _optional_text(value)
    return None if normalized is None else date.fromisoformat(normalized)


def _reject_unknown(value: dict[str, object], allowed: set[str], location: str) -> None:
    """拒绝标准 schema 外字段，让 Provider 新增的非权威现货列经过独立评审。"""
    unknown = set(value).difference(allowed)
    if unknown:
        raise ValueError(f"unexpected {location} fields: {', '.join(sorted(unknown))}")


def _schema_error(message: str) -> ProviderError:
    """构造不可重试 schema 错误，避免载荷漂移在任务重试中伪造为短暂失败。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)
