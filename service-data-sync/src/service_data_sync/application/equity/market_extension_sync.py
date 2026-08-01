"""个股直取周期线、复权因子、公司行动与公司概况同步用例。

周线、月线、累计后复权因子、公司行动和公司资料各走独立 `capability` 与发布分区。
它们不能由日线或其他参考数据互相推导。
所有载荷均先按固定 `schema` 严格解码，再将来源摘要交给仓储；只有失败路径保留排障字节。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.market_data import (
    EquityMarketDataRepository,
    EquitySourceObservation,
    PublishedEquityDataset,
    RawPayload,
    RawPayloadStore,
)
from service_data_sync.domain.equity import (
    EquityAdjustmentFactor,
    EquityBarPeriod,
    EquityCompanyProfile,
    EquityCorporateAction,
    EquityIdentifier,
    EquityPeriodBar,
)

_PERIOD_SCHEMA = "quant-v2.equity-period-bar.v1"
_FACTOR_SCHEMA = "quant-v2.equity-adjustment-factor.v1"
_ACTION_SCHEMA = "quant-v2.equity-corporate-action.v1"
_PROFILE_SCHEMA = "quant-v2.equity-profile.v1"
_FACTOR_CAPABILITY = "equity.adjustment_factor"
_ACTION_CAPABILITY = "equity.corporate_action"
_PROFILE_CAPABILITY = "equity.profile"


@dataclass(frozen=True, slots=True)
class EquityExtensionSyncResult:
    """向 CLI 与任务返回一个证券能力发布的稳定摘要。"""

    instrument: EquityIdentifier
    capability: str
    data_version: UUID
    inserted_count: int
    unchanged_count: int
    coverage_version: UUID | None = None
    source_batch_id: UUID | None = None
    publication_kind: str | None = None
    availability: str = "available"


class EquityPeriodBarSyncService:
    """同步上游直接返回的周线或月线，不读取日线 canonical。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: EquityMarketDataRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收数据源无关来源、canonical 仓储和 raw evidence 存储。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(
        self,
        *,
        identifier: EquityIdentifier,
        period: EquityBarPeriod,
        start: date,
        end: date,
    ) -> EquityExtensionSyncResult:
        """发布包含端窗口内的一种上游原生周期行情。"""
        if period is EquityBarPeriod.DAY_1:
            raise ValueError("daily bars use EquityDailyBarSyncService")
        if start > end:
            raise ValueError("start must not be after end")
        batch = await _fetch(
            self._source,
            capability=period.capability,
            parameters=(
                ("instrument", identifier.qualified_symbol),
                ("period", period.value),
                ("start", start.isoformat()),
                ("end", end.isoformat()),
            ),
        )
        bars = decode_period_bar_batch(batch.payload, identifier=identifier, period=period)
        source = _archive(batch, self._raw_payload_store)
        publication = self._repository.publish_period_bars(
            identifier=identifier,
            period=period,
            bars=bars,
            source=source,
            start=start,
            end=end,
        )
        return _result(identifier, period.capability, publication)


class EquityAdjustmentFactorSyncService:
    """同步新浪稀疏累计后复权因子，并以完整序列发布版本。

    来源已证实但窗口内没有新生效点时，发布零记录快照以证明这次观察；它不会把因子
    解释为零，也不会清空此前已经确认的完整稀疏序列。
    """

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: EquityMarketDataRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收中立来源、canonical 仓储与 raw evidence 存储。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(
        self,
        *,
        identifier: EquityIdentifier,
        start: date,
        end: date,
    ) -> EquityExtensionSyncResult:
        """同步一个包含端日期窗口内的累计因子并发布因子版本。"""
        if start > end:
            raise ValueError("start must not be after end")
        batch = await _fetch(
            self._source,
            capability=_FACTOR_CAPABILITY,
            parameters=(
                ("instrument", identifier.qualified_symbol),
                ("start", start.isoformat()),
                ("end", end.isoformat()),
            ),
        )
        factors = decode_adjustment_factor_batch(batch.payload, identifier=identifier)
        source = _archive(batch, self._raw_payload_store)
        publication = self._repository.publish_adjustment_factors(
            identifier=identifier,
            factors=factors,
            source=source,
            window_end=end,
        )
        return _result(identifier, _FACTOR_CAPABILITY, publication)


class EquityCorporateActionSyncService:
    """同步东方财富分红送转事件并保存状态与日期修订。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: EquityMarketDataRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收中立来源、canonical 仓储与 raw evidence 存储。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(
        self,
        *,
        identifier: EquityIdentifier,
        start: date,
        end: date,
    ) -> EquityExtensionSyncResult:
        """同步窗口内公司行动；合法空事件集仍形成可复验发布。"""
        if start > end:
            raise ValueError("start must not be after end")
        batch = await _fetch(
            self._source,
            capability=_ACTION_CAPABILITY,
            parameters=(
                ("instrument", identifier.qualified_symbol),
                ("start", start.isoformat()),
                ("end", end.isoformat()),
            ),
        )
        actions = decode_corporate_action_batch(batch.payload, identifier=identifier)
        source = _archive(batch, self._raw_payload_store)
        publication = self._repository.publish_corporate_actions(
            identifier=identifier,
            actions=actions,
            source=source,
            start=start,
            end=end,
        )
        return _result(identifier, _ACTION_CAPABILITY, publication)


class EquityCompanyProfileSyncService:
    """同步巨潮公司概况，并以内容哈希追加修订。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: EquityMarketDataRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收中立来源、canonical 仓储与 raw evidence 存储。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(self, *, identifier: EquityIdentifier) -> EquityExtensionSyncResult:
        """同步一只证券当前公司概况。"""
        batch = await _fetch(
            self._source,
            capability=_PROFILE_CAPABILITY,
            parameters=(("instrument", identifier.qualified_symbol),),
        )
        profile = decode_company_profile_batch(batch.payload, identifier=identifier)
        source = _archive(batch, self._raw_payload_store)
        publication = self._repository.publish_company_profile(
            identifier=identifier,
            profile=profile,
            source=source,
        )
        return _result(identifier, _PROFILE_CAPABILITY, publication)


def decode_period_bar_batch(
    payload: bytes,
    *,
    identifier: EquityIdentifier,
    period: EquityBarPeriod,
) -> tuple[EquityPeriodBar, ...]:
    """解析独立周期行情标准载荷，并拒绝身份、周期或日期重复漂移。"""
    decoded = _payload(payload, schema=_PERIOD_SCHEMA, identifier=identifier)
    if decoded.get("period") != period.value:
        raise _schema_error("equity period-bar period mismatch")
    rows = decoded.get("bars")
    if not isinstance(rows, list):
        raise _schema_error("equity period-bar payload has no bars")
    try:
        bars = tuple(_period_bar(row, period=period) for row in rows)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise _schema_error("invalid equity period-bar value") from error
    if len({bar.period_end for bar in bars}) != len(bars):
        raise _schema_error("equity period-bar payload has duplicate periods")
    return tuple(sorted(bars, key=lambda bar: bar.period_end))


def decode_adjustment_factor_batch(
    payload: bytes,
    *,
    identifier: EquityIdentifier,
) -> tuple[EquityAdjustmentFactor, ...]:
    """解析累计后复权因子标准载荷，允许稀疏空窗并拒绝重复生效日。"""
    decoded = _payload(payload, schema=_FACTOR_SCHEMA, identifier=identifier)
    rows = decoded.get("factors")
    if not isinstance(rows, list):
        raise _schema_error("adjustment-factor payload has no factors")
    try:
        factors = tuple(
            EquityAdjustmentFactor(
                effective_date=date.fromisoformat(_required(row, "effectiveDate")),
                cumulative_factor=Decimal(_required(row, "cumulativeFactor")),
            )
            for row in rows
            if isinstance(row, dict)
        )
    except (InvalidOperation, TypeError, ValueError) as error:
        raise _schema_error("invalid adjustment-factor value") from error
    if len(factors) != len(rows) or len({item.effective_date for item in factors}) != len(factors):
        raise _schema_error("adjustment-factor payload has invalid or duplicate rows")
    return tuple(sorted(factors, key=lambda item: item.effective_date))


def decode_corporate_action_batch(
    payload: bytes,
    *,
    identifier: EquityIdentifier,
) -> tuple[EquityCorporateAction, ...]:
    """解析公司行动标准载荷，允许没有历史方案的合法空列表。"""
    decoded = _payload(payload, schema=_ACTION_SCHEMA, identifier=identifier)
    rows = decoded.get("actions")
    if not isinstance(rows, list):
        raise _schema_error("corporate-action payload actions must be a list")
    try:
        actions = tuple(_corporate_action(row) for row in rows)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise _schema_error("invalid corporate-action value") from error
    if len({item.source_event_key for item in actions}) != len(actions):
        raise _schema_error("corporate-action payload has duplicate events")
    return tuple(sorted(actions, key=lambda item: (item.report_period, item.source_event_key)))


def decode_company_profile_batch(
    payload: bytes,
    *,
    identifier: EquityIdentifier,
) -> EquityCompanyProfile:
    """解析公司概况标准载荷，并保留真实空值而不是空字符串。"""
    decoded = _payload(payload, schema=_PROFILE_SCHEMA, identifier=identifier)
    row = decoded.get("profile")
    if not isinstance(row, dict):
        raise _schema_error("company-profile payload is invalid")
    try:
        return EquityCompanyProfile(
            company_name=_required(row, "companyName"),
            english_name=_optional_text(row.get("englishName")),
            industry=_optional_text(row.get("industry")),
            legal_representative=_optional_text(row.get("legalRepresentative")),
            established_on=_optional_date(row.get("establishedOn")),
            website=_optional_text(row.get("website")),
            email=_optional_text(row.get("email")),
            phone=_optional_text(row.get("phone")),
            registered_address=_optional_text(row.get("registeredAddress")),
            office_address=_optional_text(row.get("officeAddress")),
            main_business=_optional_text(row.get("mainBusiness")),
            business_scope=_optional_text(row.get("businessScope")),
            summary=_optional_text(row.get("summary")),
        )
    except (TypeError, ValueError) as error:
        raise _schema_error("invalid company-profile value") from error


async def _fetch(
    source: DataSourcePort,
    *,
    capability: str,
    parameters: tuple[tuple[str, str], ...],
) -> ProviderBatch:
    """验证来源能力后经中立请求获取单个批次。"""
    if capability not in source.capabilities():
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST, "unsupported capability", retryable=False
        )
    return await source.fetch(SourceRequest(capability=capability, parameters=parameters))


def _archive(batch: ProviderBatch, raw_payload_store: RawPayloadStore) -> EquitySourceObservation:
    """归档真实 raw 与标准化对象，再返回 coverage 可复验的完整来源观察。"""
    raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
    raw_content_type = batch.raw_content_type or batch.content_type
    raw_digest = hashlib.sha256(raw_payload).hexdigest()
    raw_uri = raw_payload_store.put(
        RawPayload(
            object_key=(
                f"raw/{batch.capability}/{batch.provider_id}/{batch.observed_at:%Y/%m/%d}/"
                f"{raw_digest}.json"
            ),
            content_sha256=raw_digest,
            content_type=raw_content_type,
            payload=raw_payload,
        )
    )
    normalized_digest = hashlib.sha256(batch.payload).hexdigest()
    normalized_uri = raw_payload_store.put(
        RawPayload(
            object_key=(
                f"normalized/{batch.capability}/{batch.provider_id}/"
                f"{batch.observed_at:%Y/%m/%d}/{normalized_digest}.json"
            ),
            content_sha256=normalized_digest,
            content_type=batch.content_type,
            payload=batch.payload,
        )
    )
    return EquitySourceObservation(
        provider_id=batch.provider_id,
        capability=batch.capability,
        raw_payload_sha256=raw_digest,
        raw_uri=raw_uri,
        raw_content_type=raw_content_type,
        raw_byte_size=len(raw_payload),
        normalized_payload_sha256=normalized_digest,
        normalized_uri=normalized_uri,
        normalized_content_type=batch.content_type,
        normalized_byte_size=len(batch.payload),
        observed_at=batch.observed_at,
        upstream_source=batch.upstream_source or batch.provider_id,
        adapter_version=batch.adapter_version,
        schema_fingerprint=(
            batch.schema_fingerprint or _normalized_schema_fingerprint(batch.payload)
        ),
    )


def _normalized_schema_fingerprint(payload: bytes) -> str:
    """从已验证标准载荷的 schema 标识生成稳定指纹，缺失时失败而非伪造版本。"""
    decoded = json.loads(payload)
    schema = decoded.get("schema") if isinstance(decoded, dict) else None
    if not isinstance(schema, str) or not schema.strip():
        raise ValueError("normalized equity extension payload has no schema identity")
    return hashlib.sha256(schema.encode()).hexdigest()


def _payload(
    payload: bytes,
    *,
    schema: str,
    identifier: EquityIdentifier,
) -> dict[str, object]:
    """解析通用标准对象并校验 schema 与证券身份。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _schema_error("equity extension payload is not JSON") from error
    if not isinstance(decoded, dict) or decoded.get("schema") != schema:
        raise _schema_error("unexpected equity extension schema")
    if decoded.get("instrument") != identifier.qualified_symbol:
        raise _schema_error("equity extension identity mismatch")
    return decoded


def _period_bar(row: object, *, period: EquityBarPeriod) -> EquityPeriodBar:
    """将一条标准 JSON 周/月记录映射为精确领域值。"""
    if not isinstance(row, dict):
        raise ValueError("period bar is not an object")
    turnover = row.get("turnoverRate")
    return EquityPeriodBar(
        period=period,
        period_end=date.fromisoformat(_required(row, "periodEnd")),
        open_price=Decimal(_required(row, "open")),
        high_price=Decimal(_required(row, "high")),
        low_price=Decimal(_required(row, "low")),
        close_price=Decimal(_required(row, "close")),
        volume_shares=int(_required(row, "volumeShares")),
        amount_cny=Decimal(_required(row, "amountCny")),
        turnover_rate=None if turnover is None else Decimal(str(turnover)),
    )


def _corporate_action(row: object) -> EquityCorporateAction:
    """将一条标准事件 JSON 映射为可修订公司行动。"""
    if not isinstance(row, dict):
        raise ValueError("corporate action is not an object")
    return EquityCorporateAction(
        source_event_key=_required(row, "sourceEventKey"),
        report_period=date.fromisoformat(_required(row, "reportPeriod")),
        status=_required(row, "status"),
        announcement_date=_optional_date(row.get("announcementDate")),
        record_date=_optional_date(row.get("recordDate")),
        ex_date=_optional_date(row.get("exDate")),
        cash_dividend_per_10=_optional_decimal(row.get("cashDividendPer10")),
        bonus_shares_per_10=_optional_decimal(row.get("bonusSharesPer10")),
        transfer_shares_per_10=_optional_decimal(row.get("transferSharesPer10")),
    )


def _required(row: dict[str, object], key: str) -> str:
    """读取非空必填标量字段。"""
    value = row.get(key)
    if value is None or not str(value).strip():
        raise ValueError(f"{key} is required")
    return str(value)


def _optional_text(value: object) -> str | None:
    """把缺失、空白与 pandas 空值统一成真实空值。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return None if not normalized or normalized.lower() == "nan" else normalized


def _optional_date(value: object) -> date | None:
    """把可空 ISO 日期解析为日历值。"""
    normalized = _optional_text(value)
    return None if normalized is None else date.fromisoformat(normalized)


def _optional_decimal(value: object) -> Decimal | None:
    """把可空十进制字符串解析为精确数值。"""
    normalized = _optional_text(value)
    return None if normalized is None else Decimal(normalized)


def _schema_error(message: str) -> ProviderError:
    """构造不可重试的标准载荷漂移错误。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)


def _result(
    identifier: EquityIdentifier,
    capability: str,
    publication: PublishedEquityDataset,
) -> EquityExtensionSyncResult:
    """将仓储发布投影为任务与 CLI 使用的稳定结果。"""
    return EquityExtensionSyncResult(
        instrument=identifier,
        capability=capability,
        data_version=publication.data_version,
        inserted_count=publication.inserted_count,
        unchanged_count=publication.unchanged_count,
        coverage_version=publication.coverage_version,
        source_batch_id=publication.source_batch_id,
        publication_kind=publication.publication_kind,
    )
