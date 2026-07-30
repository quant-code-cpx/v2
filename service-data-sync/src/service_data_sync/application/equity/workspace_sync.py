"""股票中心普通停牌、股本结构与申万三级归属的严格同步用例。"""

from __future__ import annotations

import hashlib
import json
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
from service_data_sync.application.ports.equity_workspace import (
    EquityWorkspaceRepository,
    EquityWorkspaceSourceObservation,
    PublishedEquityWorkspaceDataset,
)
from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.domain.equity import EquityIdentifier
from service_data_sync.domain.equity_workspace import (
    EquityShareCapital,
    EquityTradingStatus,
    SwEquityMembership,
)

_TRADING_STATUS_CAPABILITY = "equity.trading_status.1d"
_TRADING_STATUS_SCHEMA = "quant-v2.equity-trading-status.v1"
_SHARE_CAPITAL_CAPABILITY = "equity.share_capital.reported"
_SHARE_CAPITAL_SCHEMA = "quant-v2.equity-share-capital.v1"
_SW_MEMBERSHIP_CAPABILITY = "sector.sw2021.membership.snapshot"
_SW_MEMBERSHIP_SCHEMA = "quant-v2.sw2021-membership.v1"


class EquityTradingStatusSyncService:
    """同步一个观察日来源明确披露的普通停牌清单。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: EquityWorkspaceRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收 provider-neutral 来源、事实仓储与失败留证端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(self, *, observation_date: date) -> PublishedEquityWorkspaceDataset:
        """抓取并严格发布一个日期分区；响应缺席不会被解释为成交状态。"""
        batch = await _fetch(
            self._source,
            _TRADING_STATUS_CAPABILITY,
            (("observationDate", observation_date.isoformat()),),
        )
        payload_date, statuses = decode_trading_status_batch(batch.payload)
        if payload_date != observation_date:
            raise _schema_error("trading status observation date does not match request")
        return self._repository.publish_trading_statuses(
            observation_date=observation_date,
            statuses=statuses,
            source=_archive_batch(batch, self._raw_payload_store),
        )


class EquityShareCapitalSyncService:
    """同步一只证券来源报告的完整历史股本结构。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: EquityWorkspaceRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收 provider-neutral 来源、事实仓储与失败留证端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(
        self,
        *,
        identifier: EquityIdentifier,
        instrument_id: UUID,
        identity_as_of: date,
    ) -> PublishedEquityWorkspaceDataset:
        """按冻结永久身份发布非空历史；空响应视为不可发布而不是零股本。"""
        batch = await _fetch(
            self._source,
            _SHARE_CAPITAL_CAPABILITY,
            (("instrument", identifier.qualified_symbol),),
        )
        payload_identifier, structures = decode_share_capital_batch(batch.payload)
        if payload_identifier != identifier:
            raise _schema_error("share capital instrument does not match request")
        if not structures:
            raise _schema_error("share capital response contains no reportable structure")
        return self._repository.publish_share_capital(
            identifier=identifier,
            instrument_id=instrument_id,
            identity_as_of=identity_as_of,
            structures=structures,
            source=_archive_batch(batch, self._raw_payload_store),
        )


class SwMembershipSyncService:
    """同步一个申万三级节点的当前完整证券成分快照。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: EquityWorkspaceRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收 provider-neutral 来源、事实仓储与失败留证端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(
        self, *, node_code: str, observation_date: date
    ) -> PublishedEquityWorkspaceDataset:
        """发布当前节点非空快照；历史日期由 adapter 明确拒绝。"""
        batch = await _fetch(
            self._source,
            _SW_MEMBERSHIP_CAPABILITY,
            (
                ("nodeCode", node_code),
                ("observationDate", observation_date.isoformat()),
            ),
        )
        payload_node, payload_date, memberships = decode_sw_membership_batch(batch.payload)
        if payload_node != node_code or payload_date != observation_date:
            raise _schema_error("SW membership identity does not match request")
        if not memberships:
            raise _schema_error("SW membership response contains no constituents")
        return self._repository.publish_sw_memberships(
            node_code=node_code,
            observation_date=observation_date,
            memberships=memberships,
            source=_archive_batch(batch, self._raw_payload_store),
        )


async def _fetch(
    source: DataSourcePort, capability: str, parameters: tuple[tuple[str, str], ...]
) -> ProviderBatch:
    """从唯一声明能力的来源读取标准批次，未注册能力立即失败。"""
    if capability not in source.capabilities():
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            f"unsupported equity workspace capability: {capability}",
            retryable=False,
        )
    return await source.fetch(SourceRequest(capability=capability, parameters=parameters))


def decode_trading_status_batch(
    payload: bytes,
) -> tuple[date, tuple[EquityTradingStatus, ...]]:
    """严格解码普通停牌清单并拒绝重复证券。"""
    decoded = _payload(payload, _TRADING_STATUS_SCHEMA)
    _reject_unknown(decoded, {"schema", "observationDate", "statuses"}, "root")
    observation_date = date.fromisoformat(_required(decoded, "observationDate"))
    values = _array(decoded, "statuses")
    try:
        statuses = tuple(_trading_status(value, observation_date) for value in values)
    except (TypeError, ValueError) as error:
        raise _schema_error("trading status value is invalid") from error
    keys = {(item.identifier, item.trade_date) for item in statuses}
    if len(keys) != len(statuses):
        raise _schema_error("trading status response contains duplicate securities")
    return observation_date, tuple(
        sorted(statuses, key=lambda item: item.identifier.qualified_symbol)
    )


def decode_share_capital_batch(
    payload: bytes,
) -> tuple[EquityIdentifier, tuple[EquityShareCapital, ...]]:
    """严格解码证券股本历史并拒绝重复生效日。"""
    decoded = _payload(payload, _SHARE_CAPITAL_SCHEMA)
    _reject_unknown(decoded, {"schema", "instrument", "structures"}, "root")
    identity = decoded.get("instrument")
    if not isinstance(identity, dict):
        raise _schema_error("share capital instrument is not an object")
    _reject_unknown(identity, {"exchange", "symbol"}, "instrument")
    identifier = EquityIdentifier.parse(
        f"{_required(identity, 'exchange')}.{_required(identity, 'symbol')}"
    )
    values = _array(decoded, "structures")
    try:
        structures = tuple(_share_capital(value, identifier) for value in values)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise _schema_error("share capital value is invalid") from error
    if len({item.effective_on for item in structures}) != len(structures):
        raise _schema_error("share capital response contains duplicate effective dates")
    return identifier, tuple(sorted(structures, key=lambda item: item.effective_on))


def decode_sw_membership_batch(
    payload: bytes,
) -> tuple[str, date, tuple[SwEquityMembership, ...]]:
    """严格解码一个申万三级节点当前成分，并拒绝重复代码。"""
    decoded = _payload(payload, _SW_MEMBERSHIP_SCHEMA)
    _reject_unknown(
        decoded,
        {"schema", "schemeVersion", "nodeCode", "observationDate", "memberships"},
        "root",
    )
    if _required(decoded, "schemeVersion") != "SW2021":
        raise _schema_error("SW membership scheme version is unsupported")
    node_code = _required(decoded, "nodeCode")
    observation_date = date.fromisoformat(_required(decoded, "observationDate"))
    values = _array(decoded, "memberships")
    try:
        memberships = tuple(
            _sw_membership(value, node_code=node_code, observation_date=observation_date)
            for value in values
        )
    except (TypeError, ValueError) as error:
        raise _schema_error("SW membership value is invalid") from error
    if len({item.symbol for item in memberships}) != len(memberships):
        raise _schema_error("SW membership response contains duplicate securities")
    return node_code, observation_date, tuple(sorted(memberships, key=lambda item: item.symbol))


def _trading_status(value: object, observation_date: date) -> EquityTradingStatus:
    """转换一条普通停牌来源记录，预计复牌日只留在来源证据中而不冒充实际复牌。"""
    item = _object(value, "trading status")
    _reject_unknown(
        item,
        {
            "symbol",
            "market",
            "status",
            "suspendedOn",
            "expectedResumeOn",
            "reason",
        },
        "trading status",
    )
    return EquityTradingStatus(
        identifier=EquityIdentifier.parse(
            f"{_required(item, 'market')}.{_required(item, 'symbol')}"
        ),
        trade_date=observation_date,
        status=_required(item, "status"),
        reason=_optional_text(item.get("reason")),
    )


def _share_capital(value: object, identifier: EquityIdentifier) -> EquityShareCapital:
    """转换一个股本结构生效日，来源空组成项保持为空。"""
    item = _object(value, "share capital")
    _reject_unknown(
        item,
        {
            "effectiveOn",
            "totalShares",
            "listedTradableAShares",
            "restrictedShares",
            "changeReason",
        },
        "share capital",
    )
    return EquityShareCapital(
        identifier=identifier,
        effective_on=date.fromisoformat(_required(item, "effectiveOn")),
        total_shares=Decimal(_required(item, "totalShares")),
        listed_tradable_a_shares=_optional_decimal(item.get("listedTradableAShares")),
        restricted_shares=_optional_decimal(item.get("restrictedShares")),
        change_reason=_optional_text(item.get("changeReason")),
    )


def _sw_membership(value: object, *, node_code: str, observation_date: date) -> SwEquityMembership:
    """转换申万成分行；名称与层级仅作来源展示，不参与证券身份解析。"""
    item = _object(value, "SW membership")
    _reject_unknown(
        item,
        {
            "symbol",
            "name",
            "sourceIncludedOn",
            "level1Name",
            "level2Name",
            "level3Name",
        },
        "SW membership",
    )
    included_on = _optional_text(item.get("sourceIncludedOn"))
    return SwEquityMembership(
        node_code=node_code,
        symbol=_required(item, "symbol"),
        name=_required(item, "name"),
        observed_on=observation_date,
        source_included_on=None if included_on is None else date.fromisoformat(included_on),
        level1_name=_optional_text(item.get("level1Name")),
        level2_name=_optional_text(item.get("level2Name")),
        level3_name=_optional_text(item.get("level3Name")),
    )


def _archive_batch(
    batch: ProviderBatch, payload_store: RawPayloadStore
) -> EquityWorkspaceSourceObservation:
    """生成来源观察；失败包装器使成功路径得到不可回放标记，异常路径才保留字节。"""
    raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
    raw_digest = hashlib.sha256(raw_payload).hexdigest()
    normalized_digest = hashlib.sha256(batch.payload).hexdigest()
    prefix = f"equity-workspace/{batch.capability}/{batch.provider_id}/{batch.observed_at:%Y/%m/%d}"
    raw_uri = payload_store.put(
        RawPayload(
            object_key=f"raw/{prefix}/{raw_digest}.json",
            content_sha256=raw_digest,
            content_type=batch.raw_content_type or batch.content_type,
            payload=raw_payload,
        )
    )
    normalized_uri = payload_store.put(
        RawPayload(
            object_key=f"normalized/{prefix}/{normalized_digest}.json",
            content_sha256=normalized_digest,
            content_type=batch.content_type,
            payload=batch.payload,
        )
    )
    return EquityWorkspaceSourceObservation(
        provider_id=batch.provider_id,
        capability=batch.capability,
        raw_payload_sha256=raw_digest,
        raw_uri=raw_uri,
        raw_content_type=batch.raw_content_type or batch.content_type,
        raw_byte_size=len(raw_payload),
        normalized_payload_sha256=normalized_digest,
        normalized_uri=normalized_uri,
        normalized_content_type=batch.content_type,
        normalized_byte_size=len(batch.payload),
        observed_at=batch.observed_at,
        upstream_source=batch.upstream_source or batch.provider_id,
        adapter_version=batch.adapter_version,
        schema_fingerprint=batch.schema_fingerprint
        or hashlib.sha256(batch.capability.encode()).hexdigest(),
    )


def _payload(payload: bytes, schema: str) -> dict[str, object]:
    """解析指定标准 schema 的 JSON 对象。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _schema_error("equity workspace payload is not JSON") from error
    if not isinstance(decoded, dict) or decoded.get("schema") != schema:
        raise _schema_error("unexpected equity workspace schema")
    return decoded


def _object(value: object, location: str) -> dict[str, object]:
    """要求数组成员为对象。"""
    if not isinstance(value, dict):
        raise ValueError(f"{location} is not an object")
    return value


def _array(value: dict[str, object], key: str) -> list[object]:
    """要求根字段为数组。"""
    result = value.get(key)
    if not isinstance(result, list):
        raise _schema_error(f"{key} is not an array")
    return result


def _required(value: dict[str, object], key: str) -> str:
    """读取非空标准文本。"""
    result = _optional_text(value.get(key))
    if result is None:
        raise ValueError(f"{key} is required")
    return result


def _optional_text(value: object) -> str | None:
    """将标准 JSON 的可选文本保持为空或去除首尾空白。"""
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _optional_decimal(value: object) -> Decimal | None:
    """读取可选精确数值，缺失不转换为零。"""
    result = _optional_text(value)
    return None if result is None else Decimal(result)


def _reject_unknown(value: dict[str, object], allowed: set[str], location: str) -> None:
    """拒绝未冻结字段，迫使供应商 schema 漂移进入显式评审。"""
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unexpected {location} fields: {', '.join(sorted(unknown))}")


def _schema_error(message: str) -> ProviderError:
    """构造不可重试 schema 错误。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)
