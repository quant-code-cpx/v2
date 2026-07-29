"""已发布资金流方法学、日序列和供应商排行的只读仓储。

读取端按固定数据版本与方法学分开返回东财订单规模日序列、东财排行和同花顺方向排行。
供应商排行的窗口、完整性和展示顺序原样暴露，不能被误读为完整市场日线。游标、日期
范围和质量状态均在仓储受控，避免跨分区或跨方法学拼接。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from service_data_sync.application.ports.money_flow import (
    MoneyFlowDailyPage,
    MoneyFlowMethodologyPage,
    MoneyFlowRankingPage,
    MoneyFlowReadRepository,
)
from service_data_sync.domain.money_flow import (
    MoneyFlowScope,
    MoneyFlowScopeType,
    MoneyFlowWindowType,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowBucketDefinition as BucketModel,
)
from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowDailyObservation as DailyObservationModel,
)
from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowMethodology as MethodologyModel,
)
from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowMethodologyScope as MethodologyScopeModel,
)
from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowMethodologyVersion as MethodologyVersionModel,
)
from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowMethodologyWindow as MethodologyWindowModel,
)
from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowRankingItem as RankingItemModel,
)
from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowRankingMetric,
    MoneyFlowSeries,
    MoneyFlowUniverseVersion,
)
from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowRankingSnapshot as RankingSnapshotModel,
)
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.database.models.sector.catalog.sector_entity import (
    SectorEntity,
)
from service_data_sync.infrastructure.persistence.equity_identity_resolver import (
    EquityIdentityWriteConflictError,
    require_single_confirmed_identity_on_connection,
)

from ..database.models.equity.identity.equity_identifier_version import (
    EquityIdentifierVersion,
)
from ..database.models.equity.identity.equity_instrument import EquityInstrument

_DAILY_DATASET = "money_flow.daily"
_RANKING_DATASET = "money_flow.ranking"
_METHODOLOGY_DATASET = "money_flow.methodology"
_CATALOG_PARTITION = "catalog"


class MoneyFlowReadUnavailable(RuntimeError):
    """表示已发布资金流读取因数据库或一致性错误暂时不可用。"""


class MoneyFlowCursorMismatch(ValueError):
    """表示游标签名无效，或游标不属于当前版本和筛选条件。"""


class MoneyFlowIdentityBoundary(ValueError):
    """表示请求日期范围没有唯一已确认证券身份。"""


class SqlAlchemyMoneyFlowReadRepository(MoneyFlowReadRepository):
    """只投影 current publication，研究态 canonical 值不会被旁路读取。"""

    def __init__(self, database: DatabaseClient, *, cursor_secret: bytes) -> None:
        """保存数据库会话工厂和仅用于内部游标认证的 HMAC 密钥。"""
        if len(cursor_secret) < 32:
            raise ValueError("money-flow cursor secret must contain at least 32 bytes")
        self._database = database
        self._cursor_secret = cursor_secret

    def list_methodologies(
        self,
        *,
        semantic_family: str | None,
        methodology_status: str | None,
        scope_type: MoneyFlowScopeType | None,
        cursor: str | None,
        limit: int,
    ) -> MoneyFlowMethodologyPage | None:
        """读取内部方法学目录，研究态条目可见但不会带出未发布值。"""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be from 1 to 100")
        try:
            with self._database.session() as session:
                publication = _current_publication(
                    session,
                    dataset=_METHODOLOGY_DATASET,
                    partition_key=_CATALOG_PARTITION,
                )
                if publication is None:
                    return None
                filters = {
                    "kind": "methodologies",
                    "semanticFamily": semantic_family,
                    "methodologyStatus": methodology_status,
                    "scopeType": None if scope_type is None else scope_type.value,
                    "dataVersion": str(publication["data_version"]),
                }
                cursor_payload = self._decode_cursor(cursor, filters)
                after_key = None if cursor_payload is None else str(cursor_payload["last"])
                statement = (
                    select(
                        MethodologyModel.methodology_id,
                        MethodologyModel.public_key,
                        MethodologyVersionModel.version_id,
                        MethodologyVersionModel.version,
                        MethodologyVersionModel.status,
                        MethodologyVersionModel.production_enabled,
                        MethodologyVersionModel.adapter_provider,
                        MethodologyVersionModel.upstream_source,
                        MethodologyVersionModel.source_dataset,
                        MethodologyVersionModel.semantic_family,
                        MethodologyVersionModel.direction_definition,
                        MethodologyVersionModel.ratio_denominator,
                        MethodologyVersionModel.finality,
                        MethodologyVersionModel.currency,
                        MethodologyVersionModel.raw_amount_unit,
                        MethodologyVersionModel.standard_amount_unit,
                        MethodologyVersionModel.conversion_version,
                        MethodologyVersionModel.supports_gross_inflow,
                        MethodologyVersionModel.supports_gross_outflow,
                        MethodologyVersionModel.supports_net_amount,
                        MethodologyVersionModel.supports_net_ratio,
                        MethodologyVersionModel.effective_from,
                        MethodologyVersionModel.retired_at,
                    )
                    .select_from(MethodologyModel)
                    .join(
                        MethodologyVersionModel,
                        MethodologyVersionModel.methodology_id == MethodologyModel.methodology_id,
                    )
                )
                if semantic_family is not None:
                    statement = statement.where(
                        MethodologyVersionModel.semantic_family == semantic_family
                    )
                if methodology_status is not None:
                    statement = statement.where(
                        MethodologyVersionModel.status == methodology_status
                    )
                if scope_type is not None:
                    statement = statement.where(
                        exists(
                            select(MethodologyScopeModel.version_id).where(
                                MethodologyScopeModel.version_id
                                == MethodologyVersionModel.version_id,
                                MethodologyScopeModel.scope_type == scope_type.value,
                            )
                        )
                    )
                if after_key is not None:
                    statement = statement.where(_methodology_sort_key_expression() > after_key)
                rows = (
                    session.execute(
                        statement.order_by(
                            MethodologyModel.public_key,
                            MethodologyVersionModel.version,
                        ).limit(limit + 1)
                    )
                    .mappings()
                    .all()
                )
                visible = rows[:limit]
                items = tuple(self._methodology_item(session, dict(row)) for row in visible)
                next_cursor = (
                    self._encode_cursor(
                        filters,
                        last=_methodology_sort_key(
                            str(visible[-1]["public_key"]),
                            str(visible[-1]["version"]),
                        ),
                    )
                    if len(rows) > limit and visible
                    else None
                )
                return MoneyFlowMethodologyPage(
                    data_version=UUID(str(publication["data_version"])),
                    published_at=_aware_datetime(publication["published_at"]),
                    items=items,
                    next_cursor=next_cursor,
                )
        except MoneyFlowCursorMismatch:
            raise
        except SQLAlchemyError as error:
            raise MoneyFlowReadUnavailable(
                "money-flow methodology catalog is unavailable"
            ) from error

    def list_daily(
        self,
        *,
        methodology_id: str,
        methodology_version: str,
        scope: MoneyFlowScope,
        bucket: str,
        start: date,
        end: date,
        known_at: datetime | None,
        cursor: str | None,
        limit: int,
    ) -> MoneyFlowDailyPage | None:
        """按 C1 日期感知身份和知识时点读取一条生产日序列。"""
        if start > end or (end - start).days > 365:
            raise ValueError("money-flow date range is invalid")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be from 1 to 500")
        if known_at is not None and known_at.tzinfo is None:
            raise ValueError("known_at must include a timezone")
        try:
            with self._database.session() as session:
                scope_identity = self._daily_scope_identity(
                    session,
                    scope=scope,
                    start=start,
                    end=end,
                    known_at=known_at or datetime.now(UTC),
                )
                header = self._daily_header(
                    session,
                    methodology_id=methodology_id,
                    methodology_version=methodology_version,
                    scope=scope,
                    scope_identity=scope_identity,
                    bucket=bucket,
                )
                if header is None:
                    return None
                publication = _current_publication(
                    session,
                    dataset=_DAILY_DATASET,
                    partition_key=f"series:{header['series_id']}",
                )
                if publication is None:
                    return None
                applied_known_at = known_at or _aware_datetime(publication["knowledge_cutoff"])
                filters = {
                    "kind": "daily",
                    "methodologyId": methodology_id,
                    "methodologyVersion": methodology_version,
                    "scope": _scope_cursor_identity(scope),
                    "bucket": bucket,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "knownAt": applied_known_at.isoformat(),
                    "dataVersion": str(publication["data_version"]),
                }
                cursor_payload = self._decode_cursor(cursor, filters)
                after_date = (
                    None
                    if cursor_payload is None
                    else date.fromisoformat(str(cursor_payload["last"]))
                )
                statement = select(
                    DailyObservationModel.trade_date,
                    DailyObservationModel.observed_at,
                    DailyObservationModel.known_from,
                    DailyObservationModel.gross_inflow,
                    DailyObservationModel.gross_outflow,
                    DailyObservationModel.net_amount,
                    DailyObservationModel.net_ratio,
                    DailyObservationModel.quality_status,
                ).where(
                    DailyObservationModel.series_id == header["series_id"],
                    DailyObservationModel.trade_date >= start,
                    DailyObservationModel.trade_date <= end,
                    DailyObservationModel.known_from <= applied_known_at,
                    or_(
                        DailyObservationModel.known_to.is_(None),
                        DailyObservationModel.known_to > applied_known_at,
                    ),
                )
                if after_date is not None:
                    statement = statement.where(DailyObservationModel.trade_date > after_date)
                rows = (
                    session.execute(
                        statement.order_by(DailyObservationModel.trade_date).limit(limit + 1)
                    )
                    .mappings()
                    .all()
                )
                visible = rows[:limit]
                items = tuple(
                    {
                        "tradeDate": row["trade_date"].isoformat(),
                        "observedAt": _timestamp(row["observed_at"]),
                        "knownFrom": _timestamp(row["known_from"]),
                        "finality": str(header["finality"]),
                        "grossInflow": _decimal_text(row["gross_inflow"]),
                        "grossOutflow": _decimal_text(row["gross_outflow"]),
                        "netAmount": _decimal_text(row["net_amount"]),
                        "netRatio": _decimal_text(row["net_ratio"]),
                        "qualityStatus": str(row["quality_status"]),
                    }
                    for row in visible
                )
                next_cursor = (
                    self._encode_cursor(
                        filters,
                        last=visible[-1]["trade_date"].isoformat(),
                    )
                    if len(rows) > limit and visible
                    else None
                )
                return MoneyFlowDailyPage(
                    series_id=UUID(str(header["series_id"])),
                    data_version=UUID(str(publication["data_version"])),
                    published_at=_aware_datetime(publication["published_at"]),
                    methodology=_methodology_summary(header),
                    scope=_scope_projection(scope, scope_identity, header),
                    universe=str(header["universe_code"]),
                    bucket=bucket,
                    known_at_applied=applied_known_at,
                    items=items,
                    next_cursor=next_cursor,
                )
        except (MoneyFlowCursorMismatch, MoneyFlowIdentityBoundary):
            raise
        except SQLAlchemyError as error:
            raise MoneyFlowReadUnavailable("money-flow daily series is unavailable") from error

    def list_ranking(
        self,
        *,
        methodology_id: str,
        methodology_version: str,
        scope_type: MoneyFlowScopeType,
        universe: str,
        window_type: MoneyFlowWindowType,
        window_size: int,
        bucket: str,
        trade_date: date | None,
        cursor: str | None,
        limit: int,
    ) -> MoneyFlowRankingPage | None:
        """读取 exact 或 latest 的单一已发布 supplier ranking 快照。"""
        if scope_type is MoneyFlowScopeType.MARKET:
            raise ValueError("money-flow rankings do not support market scope")
        if (window_type is MoneyFlowWindowType.SUPPLIER_DAY and window_size != 1) or (
            window_type is MoneyFlowWindowType.SUPPLIER_ROLLING and window_size <= 1
        ):
            raise ValueError("money-flow ranking window is invalid")
        if not 1 <= window_size <= 252 or not 1 <= limit <= 500:
            raise ValueError("money-flow ranking limit or window is invalid")
        try:
            with self._database.session() as session:
                header = self._ranking_header(
                    session,
                    methodology_id=methodology_id,
                    methodology_version=methodology_version,
                    scope_type=scope_type,
                    universe=universe,
                    window_type=window_type,
                    window_size=window_size,
                    bucket=bucket,
                    trade_date=trade_date,
                )
                if header is None:
                    return None
                publication = _current_publication(
                    session,
                    dataset=_RANKING_DATASET,
                    partition_key=_ranking_partition_key(header),
                )
                if publication is None:
                    return None
                filters = {
                    "kind": "ranking",
                    "methodologyId": methodology_id,
                    "methodologyVersion": methodology_version,
                    "scopeType": scope_type.value,
                    "universe": universe,
                    "windowType": window_type.value,
                    "windowSize": window_size,
                    "bucket": bucket,
                    "tradeDate": header["target_trade_date"].isoformat(),
                    "dataVersion": str(publication["data_version"]),
                }
                cursor_payload = self._decode_cursor(cursor, filters)
                after_position = (
                    None if cursor_payload is None else _cursor_positive_int(cursor_payload, "last")
                )
                ranking_known_at = _aware_datetime(publication["knowledge_cutoff"])
                statement = (
                    select(
                        RankingItemModel.supplier_position,
                        RankingItemModel.scope_type,
                        RankingItemModel.security_id,
                        RankingItemModel.sector_key,
                        RankingItemModel.scope_name_at_snapshot,
                        EquityInstrument.instrument_id,
                        EquityIdentifierVersion.exchange,
                        EquityIdentifierVersion.symbol,
                        SectorEntity.sector_id,
                        SectorEntity.scheme,
                        SectorEntity.sector_code,
                        MoneyFlowRankingMetric.gross_inflow,
                        MoneyFlowRankingMetric.gross_outflow,
                        MoneyFlowRankingMetric.net_amount,
                        MoneyFlowRankingMetric.net_ratio,
                    )
                    .select_from(RankingItemModel)
                    .join(
                        MoneyFlowRankingMetric,
                        and_(
                            MoneyFlowRankingMetric.target_trade_date
                            == RankingItemModel.target_trade_date,
                            MoneyFlowRankingMetric.snapshot_id == RankingItemModel.snapshot_id,
                            MoneyFlowRankingMetric.supplier_position
                            == RankingItemModel.supplier_position,
                        ),
                    )
                    .join(
                        BucketModel,
                        BucketModel.bucket_id == MoneyFlowRankingMetric.bucket_id,
                    )
                    .outerjoin(
                        EquityInstrument,
                        EquityInstrument.security_id == RankingItemModel.security_id,
                    )
                    .outerjoin(
                        EquityIdentifierVersion,
                        and_(
                            EquityIdentifierVersion.security_id == RankingItemModel.security_id,
                            EquityIdentifierVersion.identity_state == "CONFIRMED",
                            EquityIdentifierVersion.effective_range.op("@>")(
                                header["target_trade_date"]
                            ),
                            EquityIdentifierVersion.knowledge_range.op("@>")(ranking_known_at),
                        ),
                    )
                    .outerjoin(
                        SectorEntity,
                        SectorEntity.sector_key == RankingItemModel.sector_key,
                    )
                    .where(
                        RankingItemModel.target_trade_date == header["target_trade_date"],
                        RankingItemModel.snapshot_id == header["snapshot_id"],
                        BucketModel.bucket_code == bucket,
                    )
                )
                if after_position is not None:
                    statement = statement.where(RankingItemModel.supplier_position > after_position)
                rows = (
                    session.execute(
                        statement.order_by(RankingItemModel.supplier_position).limit(limit + 1)
                    )
                    .mappings()
                    .all()
                )
                visible = rows[:limit]
                items = tuple(_ranking_item(dict(row)) for row in visible)
                next_cursor = (
                    self._encode_cursor(
                        filters,
                        last=int(visible[-1]["supplier_position"]),
                    )
                    if len(rows) > limit and visible
                    else None
                )
                return MoneyFlowRankingPage(
                    data_version=UUID(str(publication["data_version"])),
                    published_at=_aware_datetime(publication["published_at"]),
                    methodology=_methodology_summary(header),
                    snapshot={
                        "snapshotId": str(header["snapshot_id"]),
                        "scopeType": str(header["scope_type"]),
                        "universe": str(header["universe_code"]),
                        "targetTradeDate": header["target_trade_date"].isoformat(),
                        "sourceCutoffAt": _timestamp(header["source_cutoff_at"]),
                        "observedAt": _timestamp(header["observed_at"]),
                        "finality": str(header["finality"]),
                        "windowType": str(header["window_type"]),
                        "windowSize": int(header["window_size"]),
                        "bucket": str(header["bucket_code"]),
                        "rankingBasis": str(header["ranking_basis"]),
                        "qualityStatus": str(header["quality_status"]),
                    },
                    items=items,
                    next_cursor=next_cursor,
                )
        except MoneyFlowCursorMismatch:
            raise
        except SQLAlchemyError as error:
            raise MoneyFlowReadUnavailable("money-flow supplier ranking is unavailable") from error

    def _methodology_item(self, session: Session, row: Mapping[str, Any]) -> dict[str, object]:
        """把方法学主行和受控 scope、窗口、bucket 子表投影为内部契约。"""
        scopes = (
            session.execute(
                select(
                    MethodologyScopeModel.scope_type,
                    MethodologyScopeModel.universe_id,
                )
                .where(MethodologyScopeModel.version_id == row["version_id"])
                .order_by(MethodologyScopeModel.scope_type)
            )
            .mappings()
            .all()
        )
        windows = (
            session.execute(
                select(
                    MethodologyWindowModel.window_type,
                    MethodologyWindowModel.window_size,
                    MethodologyWindowModel.source_label,
                )
                .where(MethodologyWindowModel.version_id == row["version_id"])
                .order_by(
                    MethodologyWindowModel.window_type,
                    MethodologyWindowModel.window_size,
                )
            )
            .mappings()
            .all()
        )
        buckets = (
            session.execute(
                select(
                    BucketModel.bucket_code,
                    BucketModel.label,
                    BucketModel.definition_status,
                    BucketModel.threshold_min,
                    BucketModel.threshold_max,
                    BucketModel.threshold_unit,
                )
                .where(BucketModel.version_id == row["version_id"])
                .order_by(BucketModel.bucket_code)
            )
            .mappings()
            .all()
        )
        return {
            "methodologyUuid": str(row["methodology_id"]),
            "methodologyId": str(row["public_key"]),
            "methodologyVersion": str(row["version"]),
            "methodologyStatus": str(row["status"]),
            "productionEnabled": bool(row["production_enabled"]),
            "adapterProvider": str(row["adapter_provider"]),
            "upstreamSource": str(row["upstream_source"]),
            "sourceDataset": str(row["source_dataset"]),
            "semanticFamily": str(row["semantic_family"]),
            "scopeTypes": [str(item["scope_type"]) for item in scopes],
            "universeIds": [str(item["universe_id"]) for item in scopes],
            "supportedWindows": [
                {
                    "windowType": str(item["window_type"]),
                    "windowSize": int(item["window_size"]),
                    "label": str(item["source_label"]),
                }
                for item in windows
            ],
            "buckets": [
                {
                    "bucket": str(item["bucket_code"]),
                    "label": str(item["label"]),
                    "definitionStatus": str(item["definition_status"]),
                    "thresholdMin": _decimal_text(item["threshold_min"]),
                    "thresholdMax": _decimal_text(item["threshold_max"]),
                    "thresholdUnit": item["threshold_unit"],
                }
                for item in buckets
            ],
            "supportedMeasures": _supported_measures(row),
            "ratioDenominator": str(row["ratio_denominator"]),
            "directionDefinition": str(row["direction_definition"]),
            "finality": str(row["finality"]),
            "currency": row["currency"],
            "rawAmountUnit": str(row["raw_amount_unit"]),
            "standardAmountUnit": row["standard_amount_unit"],
            "conversionVersion": row["conversion_version"],
            "effectiveFrom": _timestamp(row["effective_from"]),
            "retiredAt": (None if row["retired_at"] is None else _timestamp(row["retired_at"])),
        }

    @staticmethod
    def _daily_scope_identity(
        session: Session,
        *,
        scope: MoneyFlowScope,
        start: date,
        end: date,
        known_at: datetime,
    ) -> int | str:
        """解析日序列请求的唯一强身份，证券范围逐日执行 C1 检查。"""
        if scope.scope_type is MoneyFlowScopeType.EQUITY:
            if scope.exchange is None or scope.symbol is None:
                raise MoneyFlowIdentityBoundary("equity scope lacks exchange or symbol")
            fact_dates = tuple(
                start + timedelta(days=offset) for offset in range((end - start).days + 1)
            )
            try:
                return require_single_confirmed_identity_on_connection(
                    session,
                    exchange=scope.exchange,
                    symbol=scope.symbol,
                    fact_dates=fact_dates,
                    known_at=known_at,
                )
            except EquityIdentityWriteConflictError as error:
                raise MoneyFlowIdentityBoundary(str(error)) from error
        if scope.scope_type is MoneyFlowScopeType.SECTOR:
            row = (
                session.execute(
                    select(SectorEntity.sector_key).where(
                        SectorEntity.scheme == scope.sector_scheme,
                        SectorEntity.sector_code == scope.sector_code,
                        SectorEntity.status == "ACTIVE",
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise MoneyFlowIdentityBoundary("sector identity is unavailable")
            return int(row["sector_key"])
        if scope.market_code is None:
            raise MoneyFlowIdentityBoundary("market identity is unavailable")
        return scope.market_code

    @staticmethod
    def _daily_header(
        session: Session,
        *,
        methodology_id: str,
        methodology_version: str,
        scope: MoneyFlowScope,
        scope_identity: int | str,
        bucket: str,
    ) -> Mapping[str, Any] | None:
        """选择唯一 production-enabled 强身份日序列及其方法学元数据。"""
        statement = (
            select(
                MoneyFlowSeries.series_id,
                MethodologyModel.public_key,
                MethodologyVersionModel.version,
                MethodologyVersionModel.upstream_source,
                MethodologyVersionModel.source_dataset,
                MethodologyVersionModel.semantic_family,
                MethodologyVersionModel.finality,
                MethodologyVersionModel.currency,
                MethodologyVersionModel.raw_amount_unit,
                MethodologyVersionModel.standard_amount_unit,
                MethodologyVersionModel.ratio_denominator,
                MethodologyVersionModel.direction_definition,
                MethodologyVersionModel.supports_gross_inflow,
                MethodologyVersionModel.supports_gross_outflow,
                MethodologyVersionModel.supports_net_amount,
                MethodologyVersionModel.supports_net_ratio,
                MoneyFlowUniverseVersion.universe_code,
                BucketModel.bucket_code,
                EquityInstrument.instrument_id,
                EquityInstrument.name.label("equity_name"),
                SectorEntity.sector_id,
                SectorEntity.name.label("sector_name"),
            )
            .select_from(MoneyFlowSeries)
            .join(
                MethodologyVersionModel,
                MethodologyVersionModel.version_id == MoneyFlowSeries.methodology_version_id,
            )
            .join(
                MethodologyModel,
                MethodologyModel.methodology_id == MethodologyVersionModel.methodology_id,
            )
            .join(
                MoneyFlowUniverseVersion,
                MoneyFlowUniverseVersion.universe_version_id == MoneyFlowSeries.universe_version_id,
            )
            .join(BucketModel, BucketModel.bucket_id == MoneyFlowSeries.bucket_id)
            .outerjoin(
                EquityInstrument,
                EquityInstrument.security_id == MoneyFlowSeries.security_id,
            )
            .outerjoin(
                SectorEntity,
                SectorEntity.sector_key == MoneyFlowSeries.sector_key,
            )
            .where(
                MethodologyModel.public_key == methodology_id,
                MethodologyVersionModel.version == methodology_version,
                MethodologyVersionModel.status == "validated",
                MethodologyVersionModel.production_enabled.is_(True),
                MoneyFlowSeries.scope_type == scope.scope_type.value,
                MoneyFlowSeries.window_type == "daily_source",
                MoneyFlowSeries.window_size == 1,
                MoneyFlowSeries.retired_at.is_(None),
                BucketModel.bucket_code == bucket,
            )
        )
        if scope.scope_type is MoneyFlowScopeType.EQUITY:
            statement = statement.where(MoneyFlowSeries.security_id == int(scope_identity))
        elif scope.scope_type is MoneyFlowScopeType.SECTOR:
            statement = statement.where(MoneyFlowSeries.sector_key == int(scope_identity))
        else:
            statement = statement.where(MoneyFlowSeries.market_code == str(scope_identity))
        row = session.execute(statement).mappings().one_or_none()
        return None if row is None else dict(row)

    @staticmethod
    def _ranking_header(
        session: Session,
        *,
        methodology_id: str,
        methodology_version: str,
        scope_type: MoneyFlowScopeType,
        universe: str,
        window_type: MoneyFlowWindowType,
        window_size: int,
        bucket: str,
        trade_date: date | None,
    ) -> Mapping[str, Any] | None:
        """选择 exact 或 latest 当前排行快照，不从日序列推导替代。"""
        statement = (
            select(
                RankingSnapshotModel.snapshot_id,
                RankingSnapshotModel.target_trade_date,
                RankingSnapshotModel.scope_type,
                RankingSnapshotModel.source_cutoff_at,
                RankingSnapshotModel.observed_at,
                RankingSnapshotModel.window_type,
                RankingSnapshotModel.window_size,
                RankingSnapshotModel.ranking_basis,
                RankingSnapshotModel.quality_status,
                MethodologyModel.public_key,
                MethodologyVersionModel.version,
                MethodologyVersionModel.upstream_source,
                MethodologyVersionModel.source_dataset,
                MethodologyVersionModel.semantic_family,
                MethodologyVersionModel.finality,
                MethodologyVersionModel.currency,
                MethodologyVersionModel.raw_amount_unit,
                MethodologyVersionModel.standard_amount_unit,
                MethodologyVersionModel.ratio_denominator,
                MethodologyVersionModel.direction_definition,
                MethodologyVersionModel.supports_gross_inflow,
                MethodologyVersionModel.supports_gross_outflow,
                MethodologyVersionModel.supports_net_amount,
                MethodologyVersionModel.supports_net_ratio,
                MoneyFlowUniverseVersion.universe_code,
                BucketModel.bucket_code,
            )
            .select_from(RankingSnapshotModel)
            .join(
                MethodologyVersionModel,
                MethodologyVersionModel.version_id == RankingSnapshotModel.methodology_version_id,
            )
            .join(
                MethodologyModel,
                MethodologyModel.methodology_id == MethodologyVersionModel.methodology_id,
            )
            .join(
                MoneyFlowUniverseVersion,
                MoneyFlowUniverseVersion.universe_version_id
                == RankingSnapshotModel.universe_version_id,
            )
            .join(
                BucketModel,
                BucketModel.bucket_id == RankingSnapshotModel.ranking_bucket_id,
            )
            .where(
                MethodologyModel.public_key == methodology_id,
                MethodologyVersionModel.version == methodology_version,
                MethodologyVersionModel.status == "validated",
                MethodologyVersionModel.production_enabled.is_(True),
                RankingSnapshotModel.scope_type == scope_type.value,
                MoneyFlowUniverseVersion.universe_code == universe,
                RankingSnapshotModel.window_type == window_type.value,
                RankingSnapshotModel.window_size == window_size,
                BucketModel.bucket_code == bucket,
                RankingSnapshotModel.status == "published",
                RankingSnapshotModel.superseded_at.is_(None),
            )
        )
        if trade_date is not None:
            statement = statement.where(RankingSnapshotModel.target_trade_date == trade_date)
        row = (
            session.execute(
                statement.order_by(RankingSnapshotModel.target_trade_date.desc()).limit(1)
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else dict(row)

    def _encode_cursor(self, filters: Mapping[str, object], *, last: object) -> str:
        """用稳定 JSON 和 HMAC-SHA256 编码版本绑定游标。"""
        body = json.dumps(
            {"v": 1, "filters": filters, "last": last},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        signature = hmac.new(self._cursor_secret, body, hashlib.sha256).digest()
        return f"{_b64url(body)}.{_b64url(signature)}"

    def _decode_cursor(
        self,
        cursor: str | None,
        filters: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        """验证游标签名、版本与筛选绑定，拒绝任何跨查询重放。"""
        if cursor is None:
            return None
        try:
            body_text, signature_text = cursor.split(".", 1)
            body = _b64url_decode(body_text)
            signature = _b64url_decode(signature_text)
            expected_signature = hmac.new(self._cursor_secret, body, hashlib.sha256).digest()
            payload = json.loads(body)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise MoneyFlowCursorMismatch("money-flow cursor is invalid") from error
        if not hmac.compare_digest(signature, expected_signature):
            raise MoneyFlowCursorMismatch("money-flow cursor signature is invalid")
        if (
            not isinstance(payload, dict)
            or payload.get("v") != 1
            or payload.get("filters") != dict(filters)
            or "last" not in payload
        ):
            raise MoneyFlowCursorMismatch("money-flow cursor does not match this query")
        return payload


def _current_publication(
    session: Session, *, dataset: str, partition_key: str
) -> Mapping[str, Any] | None:
    """读取唯一 current publication 及其缓存和知识截点。"""
    row = (
        session.execute(
            select(
                DatasetPublication.data_version,
                DatasetPublication.published_at,
                DatasetPublication.knowledge_cutoff,
            ).where(
                DatasetPublication.dataset == dataset,
                DatasetPublication.partition_key == partition_key,
                DatasetPublication.superseded_at.is_(None),
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else dict(row)


def _cursor_positive_int(payload: Mapping[str, object], key: str) -> int:
    """读取已验签游标中的正整数位置，拒绝布尔值和类型漂移。"""
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MoneyFlowCursorMismatch("money-flow cursor position is invalid")
    return value


def _methodology_sort_key(public_key: str, version: str) -> str:
    """生成目录稳定游标键，不依赖数据库排序的隐藏列。"""
    return f"{public_key}\u0000{version}"


def _methodology_sort_key_expression() -> Any:
    """生成与目录游标键一致的 PostgreSQL 文本表达式。"""
    return MethodologyModel.public_key + "\u0000" + MethodologyVersionModel.version


def _supported_measures(row: Mapping[str, Any]) -> list[str]:
    """按固定顺序投影方法学支持的四类度量。"""
    flags = (
        ("gross_inflow", "supports_gross_inflow"),
        ("gross_outflow", "supports_gross_outflow"),
        ("net_amount", "supports_net_amount"),
        ("net_ratio", "supports_net_ratio"),
    )
    return [measure for measure, flag in flags if bool(row[flag])]


def _methodology_summary(row: Mapping[str, Any]) -> dict[str, object]:
    """投影日序列和排行共同使用的方法学解释字段。"""
    return {
        "methodologyId": str(row["public_key"]),
        "methodologyVersion": str(row["version"]),
        "upstreamSource": str(row["upstream_source"]),
        "sourceDataset": str(row["source_dataset"]),
        "semanticFamily": str(row["semantic_family"]),
        "supportedMeasures": _supported_measures(row),
        "ratioDenominator": str(row["ratio_denominator"]),
        "directionDefinition": str(row["direction_definition"]),
        "currency": row["currency"],
        "amountUnit": row["standard_amount_unit"] or row["raw_amount_unit"],
    }


def _scope_cursor_identity(scope: MoneyFlowScope) -> dict[str, object]:
    """编码不含内部主键的 scope 查询身份。"""
    return {
        "scopeType": scope.scope_type.value,
        "exchange": None if scope.exchange is None else scope.exchange.value,
        "symbol": scope.symbol,
        "sectorScheme": scope.sector_scheme,
        "sectorCode": scope.sector_code,
        "marketCode": scope.market_code,
    }


def _scope_projection(
    scope: MoneyFlowScope,
    scope_identity: int | str,
    row: Mapping[str, Any],
) -> dict[str, object]:
    """把内部强身份投影为 0015 的三类 scope。"""
    if scope.scope_type is MoneyFlowScopeType.EQUITY:
        return {
            "scopeType": "equity",
            "securityId": int(scope_identity),
            "instrumentId": str(row["instrument_id"]),
            "exchange": scope.exchange.value if scope.exchange is not None else "",
            "symbol": scope.symbol or "",
            "name": row["equity_name"],
        }
    if scope.scope_type is MoneyFlowScopeType.SECTOR:
        return {
            "scopeType": "sector",
            "sectorId": str(row["sector_id"]),
            "scheme": scope.sector_scheme or "",
            "sectorCode": scope.sector_code or "",
            "name": row["sector_name"],
        }
    return {
        "scopeType": "market",
        "marketCode": str(scope_identity),
        "name": scope.name or str(scope_identity),
    }


def _ranking_item(row: Mapping[str, Any]) -> dict[str, object]:
    """把排行位置及一个排序 bucket 的固定四度量投影为内部契约。"""
    if row["scope_type"] == "equity":
        if row["instrument_id"] is None or row["exchange"] is None or row["symbol"] is None:
            raise MoneyFlowReadUnavailable(
                "ranking equity identity is unavailable at publication cutoff"
            )
        scope = {
            "scopeType": "equity",
            "securityId": int(row["security_id"]),
            "instrumentId": str(row["instrument_id"]),
            "exchange": str(row["exchange"]),
            "symbol": str(row["symbol"]),
            "name": row["scope_name_at_snapshot"],
        }
    else:
        scope = {
            "scopeType": "sector",
            "sectorId": str(row["sector_id"]),
            "scheme": str(row["scheme"]),
            "sectorCode": str(row["sector_code"]),
            "name": row["scope_name_at_snapshot"],
        }
    return {
        "supplierPosition": int(row["supplier_position"]),
        "scope": scope,
        "grossInflow": _decimal_text(row["gross_inflow"]),
        "grossOutflow": _decimal_text(row["gross_outflow"]),
        "netAmount": _decimal_text(row["net_amount"]),
        "netRatio": _decimal_text(row["net_ratio"]),
    }


def _ranking_partition_key(row: Mapping[str, Any]) -> str:
    """重建与写仓储一致的供应商排行 publication 分区键。"""
    return (
        f"{row['public_key']}/{row['version']}/ranking/"
        f"{row['scope_type']}/{row['universe_code']}/"
        f"{row['window_type']}/{row['window_size']}/"
        f"{row['bucket_code']}/{row['target_trade_date'].isoformat()}"
    )


def _decimal_text(value: Decimal | None) -> str | None:
    """把数据库精确数值投影为禁止科学计数法的字符串。"""
    return None if value is None else format(value, "f")


def _aware_datetime(value: object) -> datetime:
    """收窄数据库时间并拒绝缺失时区，避免错误点时读取。"""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise MoneyFlowReadUnavailable("money-flow publication timestamp is invalid")
    return value


def _timestamp(value: object) -> str:
    """把带时区时间规范化为 ISO 8601 UTC 文本。"""
    return _aware_datetime(value).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _b64url(value: bytes) -> str:
    """编码无填充 URL-safe Base64。"""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _b64url_decode(value: str) -> bytes:
    """恢复无填充 URL-safe Base64，并由调用方统一处理错误。"""
    return base64.urlsafe_b64decode(value.encode() + b"=" * (-len(value) % 4))


__all__ = [
    "MoneyFlowCursorMismatch",
    "MoneyFlowIdentityBoundary",
    "MoneyFlowReadUnavailable",
    "SqlAlchemyMoneyFlowReadRepository",
]
