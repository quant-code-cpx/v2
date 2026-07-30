"""龙虎榜与大宗交易 `P0` 的原子 `canonical` 发布仓储。

只发布获批来源、可按事实日唯一解析证券身份的公开交易事实。龙虎榜的原因映射和席位
集合参与内容摘要；大宗交易的来源键加 `occurrence` 保留同日相同经济字段的合法多笔
成交。仓储不混入事后统计，也不以名称或当前代码状态修复历史歧义。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from service_data_sync.application.ports.canonical_release import (
    CanonicalLineageRecord,
    CanonicalQualityDecision,
    CanonicalQualityRule,
    CanonicalReleaseCandidate,
)
from service_data_sync.application.ports.trading_events import (
    BlockTradeRepository,
    DragonTigerRepository,
    PublishedTradingEvents,
    TradingEventsSourceObservation,
)
from service_data_sync.domain.equity import EquityIdentifier
from service_data_sync.domain.trading_events import BlockTrade, DragonTigerEvent
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import CanonicalCheckpoint
from service_data_sync.infrastructure.database.models.equity.identity.equity_identifier_version import (  # noqa: E501
    EquityIdentifierVersion,
)
from service_data_sync.infrastructure.database.models.market import (
    BlockTradeExecutionRevision,
    DragonTigerEventRevision,
    DragonTigerSeatItem,
    TradingDisclosureReasonMapVersion,
    TradingVenue,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.persistence.canonical_release_repository import (
    SqlAlchemyCanonicalReleaseRepository,
)
from service_data_sync.infrastructure.persistence.event_window_coverage import (
    EventCoverageIdentity,
    EventCoverageRecords,
    PublishedEventCoverages,
    publish_event_window_coverages,
    resolve_event_coverage_identities,
)
from service_data_sync.infrastructure.persistence.typed_p0_support import (
    TypedP0SourceApproval,
    ensure_dataset,
    ensure_methodology,
    ensure_source_dataset,
    record_manifest,
    record_normalization_run,
    record_source_batch,
)

_DRAGON_DATASET = "equity.dragon_tiger.disclosure.reported"
_BLOCK_DATASET = "equity.block_trade.execution.reported"
_DRAGON_METHODOLOGY = "dragon-tiger-disclosure-reported"
_BLOCK_METHODOLOGY = "block-trade-execution-reported"
_DRAGON_MAPPING = "dragon-tiger-v1"
_BLOCK_MAPPING = "block-trade-v1"


@dataclass(frozen=True, slots=True)
class TradingEventsSourceApproval(TypedP0SourceApproval):
    """标识可用于交易公开信息生产发布的经批准真实来源。"""


@dataclass(frozen=True, slots=True)
class _PreparedDragon:
    """封装待写龙虎榜 revision 的领域事件、永久身份、原因家族与摘要。"""

    value: DragonTigerEvent
    security_id: int
    venue_id: UUID
    reason_family: str
    content_hash: str
    revision_no: int


@dataclass(frozen=True, slots=True)
class _PreparedBlock:
    """封装待写大宗交易 revision 的领域事实、永久身份、经济摘要与版本。"""

    value: BlockTrade
    security_id: int
    venue_id: UUID
    economic_fingerprint: str
    content_hash: str
    revision_no: int


class SqlAlchemyTradingEventsRepository(DragonTigerRepository, BlockTradeRepository):
    """只发布已批准来源、可解析证券身份且不混入事后指标的交易公开事实。"""

    def __init__(
        self,
        database: DatabaseClient,
        *,
        approved_sources: Mapping[str, TradingEventsSourceApproval] | None = None,
    ) -> None:
        """保存事务工厂和来源批准表；没有批准项时所有发布保持 fail-closed。"""
        self._database = database
        self._approved_sources = dict(approved_sources or {})
        self._release_repository = SqlAlchemyCanonicalReleaseRepository(database)

    def publish_dragon_tiger(
        self,
        *,
        events: Sequence[DragonTigerEvent],
        source: TradingEventsSourceObservation,
        start: date,
        end: date,
        identifier: EquityIdentifier | None = None,
    ) -> PublishedTradingEvents:
        """原子发布龙虎榜事实及逐证券窗口 manifest，合法空窗 record_count 为零。"""
        values = tuple(events)
        if start > end:
            raise ValueError("dragon-tiger publication window is invalid")
        if len({value.source_event_key for value in values}) != len(values):
            raise ValueError("dragon-tiger events must be unique by source event key")
        if identifier is not None and any(
            value.source_security_code != identifier.symbol for value in values
        ):
            raise ValueError("dragon-tiger events do not match requested instrument")
        approval = _approved_source(self._approved_sources, source)
        prepared: list[_PreparedDragon] = []
        source_batch_id: UUID | None = None
        dataset_id: UUID | None = None
        methodology_id: UUID | None = None
        coverage_identities: tuple[EventCoverageIdentity, ...] = ()
        coverage_scope = ""
        universe_hash = ""
        coverage_publication: PublishedEventCoverages | None = None
        accepted_values: list[DragonTigerEvent] = []
        excluded_count = 0

        def prepare(session: Session) -> CanonicalReleaseCandidate:
            """在单事务内登记来源、解析证券及交易所、映射原因并生成 release 候选。"""
            nonlocal excluded_count
            nonlocal coverage_identities, coverage_scope, dataset_id
            nonlocal methodology_id, source_batch_id, universe_hash
            now = datetime.now(UTC)
            dataset_id = ensure_dataset(
                session,
                code=_DRAGON_DATASET,
                domain="trading_disclosure",
                grain="security + trade date + source event + disclosed seat",
                now=now,
            )
            methodology_id = ensure_methodology(
                session,
                code=_DRAGON_METHODOLOGY,
                semantic_family="reported-dragon-tiger-disclosure",
                mapping_version=_DRAGON_MAPPING,
                documentation_ref="docs/service-data-sync/0025-trading-events/index.html",
            )
            source_dataset_id = ensure_source_dataset(
                session,
                approval=approval,
                capability=source.capability,
                native_grain="dragon-tiger event + seat",
            )
            source_batch_id = record_source_batch(
                session, source=source, source_dataset_id=source_dataset_id, now=now
            )
            (
                coverage_identities,
                coverage_scope,
                universe_hash,
            ) = resolve_event_coverage_identities(
                session,
                start=start,
                end=end,
                identifier=identifier,
            )
            eligible_values, excluded_count = _dragon_roster_values(
                values=values,
                identities=coverage_identities,
                identifier=identifier,
            )
            accepted_values[:] = eligible_values
            resolved = {
                value.source_event_key: _resolve_security_and_venue(
                    session,
                    source_code=value.source_security_code,
                    trade_date=value.trade_date,
                    identifier=identifier,
                )
                for value in eligible_values
            }
            current = _current_dragon(
                session, provider_id=source.provider_id, methodology_version_id=methodology_id
            )
            prepared[:] = []
            for value in eligible_values:
                security_id, venue_id = resolved[value.source_event_key]
                reason_family = _reason_family(
                    session,
                    venue_id=venue_id,
                    reason_code=value.reason_code,
                    trade_date=value.trade_date,
                )
                content_hash = _dragon_hash(value, reason_family)
                existing = current.get(value.source_event_key)
                if existing is None or content_hash != existing.content_hash:
                    prepared.append(
                        _PreparedDragon(
                            value=value,
                            security_id=security_id,
                            venue_id=venue_id,
                            reason_family=reason_family,
                            content_hash=content_hash,
                            revision_no=1 if existing is None else existing.revision_no + 1,
                        )
                    )
            partition_key = f"provider:{source.provider_id}"
            normalization_run_id = record_normalization_run(
                session,
                dataset_id=dataset_id,
                partition_key=partition_key,
                source=source,
                source_batch_id=source_batch_id,
                mapping_version=_DRAGON_MAPPING,
                now=now,
            )
            return _dragon_candidate(
                session,
                dataset_id=dataset_id,
                methodology_version_id=methodology_id,
                normalization_run_id=normalization_run_id,
                partition_key=partition_key,
                current=current,
                changed={item.value.source_event_key: item for item in prepared},
                source_batch_id=source_batch_id,
                now=now,
                window_end=end,
            )

        def write(session: Session, candidate: CanonicalReleaseCandidate, release_id: UUID) -> None:
            """关闭相同来源事件的旧知识版本，追加事件头和不合并的原始席位集合。"""
            if source_batch_id is None:
                raise AssertionError("dragon-tiger preparation did not resolve source batch")
            for item in prepared:
                session.execute(
                    update(DragonTigerEventRevision)
                    .where(
                        DragonTigerEventRevision.source_event_key == item.value.source_event_key,
                        DragonTigerEventRevision.methodology_version_id
                        == candidate.methodology_version_id,
                        DragonTigerEventRevision.known_to.is_(None),
                    )
                    .values(known_to=candidate.created_at)
                )
                revision_id = uuid4()
                session.execute(
                    insert(DragonTigerEventRevision).values(
                        trade_date=item.value.trade_date,
                        event_revision_id=revision_id,
                        security_id=item.security_id,
                        venue_id=item.venue_id,
                        reason_code=item.value.reason_code,
                        reason_raw=item.value.reason_text,
                        reason_family=item.reason_family,
                        close_price=item.value.close_price,
                        turnover_amount=item.value.market_turnover_amount,
                        buy_amount=item.value.buy_amount,
                        sell_amount=item.value.sell_amount,
                        net_amount=item.value.net_amount,
                        deal_amount=item.value.deal_amount,
                        deal_ratio=item.value.deal_ratio,
                        net_ratio=item.value.net_ratio,
                        turnover_ratio=item.value.turnover_ratio,
                        currency="CNY",
                        source_event_key=item.value.source_event_key,
                        methodology_version_id=candidate.methodology_version_id,
                        release_id=release_id,
                        revision_no=item.revision_no,
                        source_batch_id=source_batch_id,
                        source_published_at=item.value.source_published_at,
                        source_time_precision=item.value.visible_time_precision,
                        public_usable_at=item.value.visible_at,
                        availability_basis="OFFICIAL_DISCLOSURE",
                        known_from=candidate.created_at,
                        known_to=None,
                        content_hash=item.content_hash,
                        quality_status="passed",
                    )
                )
                session.execute(
                    insert(DragonTigerSeatItem).values(
                        [
                            {
                                "trade_date": item.value.trade_date,
                                "event_revision_id": revision_id,
                                "side": seat.list_side,
                                "rank_no": seat.rank,
                                "seat_code": seat.seat_code,
                                "seat_name_raw": seat.seat_name,
                                "seat_type": None,
                                "amount": seat.buy_amount
                                if seat.list_side == "BUY"
                                else seat.sell_amount,
                                "ratio": seat.buy_ratio
                                if seat.list_side == "BUY"
                                else seat.sell_ratio,
                                "buy_amount": seat.buy_amount,
                                "sell_amount": seat.sell_amount,
                                "net_amount": seat.net_amount,
                                "buy_ratio": seat.buy_ratio,
                                "sell_ratio": seat.sell_ratio,
                            }
                            for seat in item.value.seats
                        ]
                    )
                )
                record_manifest(
                    session,
                    normalization_run_id=candidate.normalization_run_id,
                    record_key_hash=_hash(item.value.source_event_key),
                    canonical_table=DragonTigerEventRevision.__tablename__,
                    canonical_pk={"eventRevisionId": str(revision_id)},
                    content_hash=item.content_hash,
                )

        def write_visibility(
            session: Session,
            candidate: CanonicalReleaseCandidate,
            publication_id: UUID,
            data_version: UUID,
            release_id: UUID,
        ) -> None:
            """为龙虎榜累积 release 选择结果发布逐证券窗口 manifest。"""
            del publication_id, data_version, release_id
            nonlocal coverage_publication
            if (
                dataset_id is None
                or methodology_id is None
                or source_batch_id is None
                or not coverage_identities
            ):
                raise AssertionError("dragon-tiger coverage preparation did not resolve state")
            resolved_methodology_id = methodology_id
            coverage_publication = publish_event_window_coverages(
                session,
                release_repository=self._release_repository,
                dataset_id=dataset_id,
                dataset_code=_DRAGON_DATASET,
                methodology_version_id=resolved_methodology_id,
                mapping_version=_DRAGON_MAPPING,
                source=source,
                source_batch_id=source_batch_id,
                identities=coverage_identities,
                coverage_scope=coverage_scope,
                universe_hash=universe_hash,
                families=("DRAGON_TIGER",),
                records_for=lambda current_session, frozen_identities, family: (
                    _dragon_coverage_records_by_identity(
                        current_session,
                        identities=frozen_identities,
                        methodology_version_id=resolved_methodology_id,
                        provider_id=source.provider_id,
                    )
                ),
                now=candidate.created_at,
            )

        publication = self._release_repository.publish_prepared(
            prepare_candidate=prepare,
            write_facts=write,
            write_visibility=write_visibility,
            record_fenced_progress=False,
        )
        del publication
        if coverage_publication is None:
            raise AssertionError("dragon-tiger publication completed without coverage manifest")
        return PublishedTradingEvents(
            data_version=coverage_publication.data_version,
            inserted_count=len(prepared),
            unchanged_count=len(accepted_values) - len(prepared),
            excluded_count=excluded_count,
        )

    def publish_block_trades(
        self,
        *,
        trades: Sequence[BlockTrade],
        source: TradingEventsSourceObservation,
        start: date,
        end: date,
        identifier: EquityIdentifier | None = None,
    ) -> PublishedTradingEvents:
        """原子发布大宗逐笔及逐证券窗口 manifest，合法空窗仍可被消费。"""
        values = tuple(trades)
        if start > end:
            raise ValueError("block-trade publication window is invalid")
        if len({(value.source_trade_key, value.occurrence_no) for value in values}) != len(values):
            raise ValueError("block trades must be unique by source key/occurrence")
        if identifier is not None and any(
            value.source_security_code != identifier.symbol for value in values
        ):
            raise ValueError("block trades do not match requested instrument")
        approval = _approved_source(self._approved_sources, source)
        prepared: list[_PreparedBlock] = []
        source_batch_id: UUID | None = None
        dataset_id: UUID | None = None
        methodology_id: UUID | None = None
        coverage_identities: tuple[EventCoverageIdentity, ...] = ()
        coverage_scope = ""
        universe_hash = ""
        coverage_publication: PublishedEventCoverages | None = None
        accepted_values: list[BlockTrade] = []
        excluded_count = 0

        def prepare(session: Session) -> CanonicalReleaseCandidate:
            """在事务内注册来源、解析证券场所并按来源逐笔键构造 release 候选。"""
            nonlocal excluded_count
            nonlocal coverage_identities, coverage_scope, dataset_id
            nonlocal methodology_id, source_batch_id, universe_hash
            now = datetime.now(UTC)
            dataset_id = ensure_dataset(
                session,
                code=_BLOCK_DATASET,
                domain="trading_disclosure",
                grain="security + trade date + source trade + occurrence",
                now=now,
            )
            methodology_id = ensure_methodology(
                session,
                code=_BLOCK_METHODOLOGY,
                semantic_family="reported-block-trade-execution",
                mapping_version=_BLOCK_MAPPING,
                documentation_ref="docs/service-data-sync/0025-trading-events/index.html",
            )
            source_dataset_id = ensure_source_dataset(
                session,
                approval=approval,
                capability=source.capability,
                native_grain="block-trade execution",
            )
            source_batch_id = record_source_batch(
                session, source=source, source_dataset_id=source_dataset_id, now=now
            )
            (
                coverage_identities,
                coverage_scope,
                universe_hash,
            ) = resolve_event_coverage_identities(
                session,
                start=start,
                end=end,
                identifier=identifier,
            )
            eligible_values, excluded_count = _block_roster_values(
                values=values,
                identities=coverage_identities,
                identifier=identifier,
            )
            accepted_values[:] = eligible_values
            resolved = {
                (value.source_trade_key, value.occurrence_no): _resolve_security_and_venue(
                    session,
                    source_code=value.source_security_code,
                    trade_date=value.trade_date,
                    identifier=identifier,
                )
                for value in eligible_values
            }
            current = _current_block(
                session, provider_id=source.provider_id, methodology_version_id=methodology_id
            )
            prepared[:] = []
            for value in eligible_values:
                key = (value.source_trade_key, value.occurrence_no)
                security_id, venue_id = resolved[key]
                economic_fingerprint = _block_economic_fingerprint(value)
                content_hash = _block_hash(value, economic_fingerprint)
                existing = current.get(key)
                if existing is None or content_hash != existing.content_hash:
                    prepared.append(
                        _PreparedBlock(
                            value=value,
                            security_id=security_id,
                            venue_id=venue_id,
                            economic_fingerprint=economic_fingerprint,
                            content_hash=content_hash,
                            revision_no=1 if existing is None else existing.revision_no + 1,
                        )
                    )
            partition_key = f"provider:{source.provider_id}"
            normalization_run_id = record_normalization_run(
                session,
                dataset_id=dataset_id,
                partition_key=partition_key,
                source=source,
                source_batch_id=source_batch_id,
                mapping_version=_BLOCK_MAPPING,
                now=now,
            )
            return _block_candidate(
                session,
                dataset_id=dataset_id,
                methodology_version_id=methodology_id,
                normalization_run_id=normalization_run_id,
                partition_key=partition_key,
                current=current,
                changed={
                    (item.value.source_trade_key, item.value.occurrence_no): item
                    for item in prepared
                },
                source_batch_id=source_batch_id,
                now=now,
                window_end=end,
            )

        def write(session: Session, candidate: CanonicalReleaseCandidate, release_id: UUID) -> None:
            """关闭同来源逐笔键旧知识版本，写入单笔真实金额和允许重复的 occurrence。"""
            if source_batch_id is None:
                raise AssertionError("block-trade preparation did not resolve source batch")
            for item in prepared:
                session.execute(
                    update(BlockTradeExecutionRevision)
                    .where(
                        BlockTradeExecutionRevision.source_event_key == item.value.source_trade_key,
                        BlockTradeExecutionRevision.occurrence_no == item.value.occurrence_no,
                        BlockTradeExecutionRevision.methodology_version_id
                        == candidate.methodology_version_id,
                        BlockTradeExecutionRevision.known_to.is_(None),
                    )
                    .values(known_to=candidate.created_at)
                )
                revision_id = uuid4()
                session.execute(
                    insert(BlockTradeExecutionRevision).values(
                        trade_date=item.value.trade_date,
                        execution_revision_id=revision_id,
                        security_id=item.security_id,
                        venue_id=item.venue_id,
                        price=item.value.execution_price,
                        quantity=item.value.quantity_shares,
                        quantity_unit="SHARE",
                        amount=item.value.notional_cny,
                        currency="CNY",
                        buyer_seat_raw=item.value.buyer_seat_name,
                        seller_seat_raw=item.value.seller_seat_name,
                        buyer_seat_code=item.value.buyer_seat_code,
                        seller_seat_code=item.value.seller_seat_code,
                        reference_price=item.value.reference_close_price,
                        reference_price_type="CLOSE"
                        if item.value.reference_close_price is not None
                        else None,
                        premium_ratio=item.value.premium_discount_ratio,
                        source_event_key=item.value.source_trade_key,
                        source_daily_rank=item.value.source_daily_rank,
                        economic_fingerprint=item.economic_fingerprint,
                        occurrence_no=item.value.occurrence_no,
                        methodology_version_id=candidate.methodology_version_id,
                        release_id=release_id,
                        revision_no=item.revision_no,
                        source_batch_id=source_batch_id,
                        source_published_at=item.value.source_published_at,
                        source_time_precision=item.value.visible_time_precision,
                        public_usable_at=item.value.visible_at,
                        availability_basis="OFFICIAL_DISCLOSURE",
                        known_from=candidate.created_at,
                        known_to=None,
                        content_hash=item.content_hash,
                        quality_status="passed",
                    )
                )
                record_manifest(
                    session,
                    normalization_run_id=candidate.normalization_run_id,
                    record_key_hash=_hash(
                        f"{item.value.source_trade_key}:{item.value.occurrence_no}"
                    ),
                    canonical_table=BlockTradeExecutionRevision.__tablename__,
                    canonical_pk={"executionRevisionId": str(revision_id)},
                    content_hash=item.content_hash,
                )

        def write_visibility(
            session: Session,
            candidate: CanonicalReleaseCandidate,
            publication_id: UUID,
            data_version: UUID,
            release_id: UUID,
        ) -> None:
            """为大宗交易累积 release 选择结果发布逐证券窗口 manifest。"""
            del publication_id, data_version, release_id
            nonlocal coverage_publication
            if (
                dataset_id is None
                or methodology_id is None
                or source_batch_id is None
                or not coverage_identities
            ):
                raise AssertionError("block-trade coverage preparation did not resolve state")
            resolved_methodology_id = methodology_id
            coverage_publication = publish_event_window_coverages(
                session,
                release_repository=self._release_repository,
                dataset_id=dataset_id,
                dataset_code=_BLOCK_DATASET,
                methodology_version_id=resolved_methodology_id,
                mapping_version=_BLOCK_MAPPING,
                source=source,
                source_batch_id=source_batch_id,
                identities=coverage_identities,
                coverage_scope=coverage_scope,
                universe_hash=universe_hash,
                families=("BLOCK_TRADE",),
                records_for=lambda current_session, frozen_identities, family: (
                    _block_coverage_records_by_identity(
                        current_session,
                        identities=frozen_identities,
                        methodology_version_id=resolved_methodology_id,
                        provider_id=source.provider_id,
                    )
                ),
                now=candidate.created_at,
            )

        publication = self._release_repository.publish_prepared(
            prepare_candidate=prepare,
            write_facts=write,
            write_visibility=write_visibility,
            record_fenced_progress=False,
        )
        del publication
        if coverage_publication is None:
            raise AssertionError("block-trade publication completed without coverage manifest")
        return PublishedTradingEvents(
            data_version=coverage_publication.data_version,
            inserted_count=len(prepared),
            unchanged_count=len(accepted_values) - len(prepared),
            excluded_count=excluded_count,
        )


def _dragon_roster_values(
    *,
    values: Sequence[DragonTigerEvent],
    identities: Sequence[EventCoverageIdentity],
    identifier: EquityIdentifier | None,
) -> tuple[tuple[DragonTigerEvent, ...], int]:
    """过滤全市场龙虎榜为冻结 A 股 roster；合法目标外事实仅计数并保留在 raw 证据。"""
    del identifier
    accepted = tuple(
        value
        for value in values
        if _in_frozen_roster(
            source_code=value.source_security_code,
            fact_date=value.trade_date,
            identities=identities,
        )
    )
    return accepted, len(values) - len(accepted)


def _block_roster_values(
    *,
    values: Sequence[BlockTrade],
    identities: Sequence[EventCoverageIdentity],
    identifier: EquityIdentifier | None,
) -> tuple[tuple[BlockTrade, ...], int]:
    """过滤全市场大宗交易为冻结 A 股 roster；目标外成交不得阻断目标 coverage。"""
    del identifier
    accepted = tuple(
        value
        for value in values
        if _in_frozen_roster(
            source_code=value.source_security_code,
            fact_date=value.trade_date,
            identities=identities,
        )
    )
    return accepted, len(values) - len(accepted)


def _in_frozen_roster(
    *,
    source_code: str,
    fact_date: date,
    identities: Sequence[EventCoverageIdentity],
) -> bool:
    """判定六位来源代码在事实日是否唯一属于冻结 roster，歧义或坏格式失败关闭。"""
    if re.fullmatch(r"[0-9]{6}", source_code) is None:
        raise ValueError("trading disclosure source security code is malformed")
    symbol_matches = [item for item in identities if item.symbol == source_code]
    matches = [
        item for item in symbol_matches if item.coverage_from <= fact_date <= item.coverage_to
    ]
    if len(matches) > 1:
        raise ValueError("trading disclosure identity is ambiguous in frozen roster")
    if symbol_matches and not matches:
        raise ValueError("trading target fact falls outside the requested coverage window")
    return len(matches) == 1


def _approved_source(
    approvals: Mapping[str, TradingEventsSourceApproval], source: TradingEventsSourceObservation
) -> TradingEventsSourceApproval:
    """取得来源批准项，技术 adapter 不能绕过数据权利和留存审查。"""
    approval = approvals.get(source.provider_id)
    if approval is None:
        raise ValueError("trading-events source provider is not approved for publication")
    return approval


def _resolve_security_and_venue(
    session: Session,
    *,
    source_code: str,
    trade_date: date,
    identifier: EquityIdentifier | None,
) -> tuple[int, UUID]:
    """按事实日期和可选交易所约束解析唯一身份，代码跨场所不得误绑定。"""
    filters = [
        EquityIdentifierVersion.symbol == source_code,
        EquityIdentifierVersion.identity_state == "CONFIRMED",
        EquityIdentifierVersion.effective_from <= trade_date,
        (EquityIdentifierVersion.effective_to.is_(None))
        | (EquityIdentifierVersion.effective_to > trade_date),
        EquityIdentifierVersion.known_to.is_(None),
    ]
    if identifier is not None:
        if source_code != identifier.symbol:
            raise ValueError("trading disclosure does not match requested instrument")
        filters.append(EquityIdentifierVersion.exchange == identifier.exchange.value)
    rows = session.execute(
        select(EquityIdentifierVersion.security_id, EquityIdentifierVersion.exchange).where(
            *filters
        )
    ).all()
    candidates = {(int(security_id), str(exchange)) for security_id, exchange in rows}
    if len(candidates) != 1:
        raise ValueError("trading disclosure security identity is missing or ambiguous")
    security_id, exchange = candidates.pop()
    venue_ids = {
        UUID(str(value))
        for value in session.execute(
            select(TradingVenue.venue_id).where(TradingVenue.code == exchange)
        )
        .scalars()
        .all()
    }
    if len(venue_ids) != 1:
        raise ValueError("trading disclosure venue identity is missing or ambiguous")
    return security_id, venue_ids.pop()


def _reason_family(session: Session, *, venue_id: UUID, reason_code: str, trade_date: date) -> str:
    """查询事实日适用的原因映射；未治理新原因显式保留为 UNKNOWN 而不杜撰分类。"""
    rows = (
        session.execute(
            select(TradingDisclosureReasonMapVersion.canonical_family).where(
                TradingDisclosureReasonMapVersion.venue_id == venue_id,
                TradingDisclosureReasonMapVersion.source_reason_code == reason_code,
                TradingDisclosureReasonMapVersion.effective_from <= trade_date,
                (TradingDisclosureReasonMapVersion.effective_to.is_(None))
                | (TradingDisclosureReasonMapVersion.effective_to > trade_date),
            )
        )
        .scalars()
        .all()
    )
    families = {str(row) for row in rows}
    if len(families) > 1:
        raise ValueError("trading disclosure reason mapping is ambiguous")
    return "UNKNOWN" if not families else families.pop()


def _current_dragon(
    session: Session, *, provider_id: str, methodology_version_id: UUID
) -> dict[str, DragonTigerEventRevision]:
    """读取同 provider 当前龙虎榜事件，避免其他来源同名键进入本发布分区。"""
    rows = (
        session.execute(
            select(DragonTigerEventRevision)
            .join(
                SourceBatch, SourceBatch.source_batch_id == DragonTigerEventRevision.source_batch_id
            )
            .where(
                SourceBatch.provider_id == provider_id,
                DragonTigerEventRevision.methodology_version_id == methodology_version_id,
                DragonTigerEventRevision.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    result = {str(row.source_event_key): row for row in rows}
    if len(result) != len(rows):
        raise ValueError("dragon-tiger current revision is ambiguous")
    return result


def _current_block(
    session: Session, *, provider_id: str, methodology_version_id: UUID
) -> dict[tuple[str, int], BlockTradeExecutionRevision]:
    """读取同 provider 当前大宗逐笔记录，来源 occurrence 是保留合法重数的组成部分。"""
    rows = (
        session.execute(
            select(BlockTradeExecutionRevision)
            .join(
                SourceBatch,
                SourceBatch.source_batch_id == BlockTradeExecutionRevision.source_batch_id,
            )
            .where(
                SourceBatch.provider_id == provider_id,
                BlockTradeExecutionRevision.methodology_version_id == methodology_version_id,
                BlockTradeExecutionRevision.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    result = {(str(row.source_event_key), row.occurrence_no): row for row in rows}
    if len(result) != len(rows):
        raise ValueError("block-trade current revision is ambiguous")
    return result


def _dragon_coverage_records_by_identity(
    session: Session,
    *,
    identities: Sequence[EventCoverageIdentity],
    methodology_version_id: UUID,
    provider_id: str,
) -> Mapping[EventCoverageIdentity, EventCoverageRecords]:
    """一次读取冻结 roster 的龙虎榜血缘，并按交易日分配到唯一身份窗口。"""
    identity_values = tuple(identities)
    mutable_records: dict[EventCoverageIdentity, list[CanonicalLineageRecord]] = {
        identity: [] for identity in identity_values
    }
    mutable_dates: dict[EventCoverageIdentity, list[date]] = {
        identity: [] for identity in identity_values
    }
    if not identity_values:
        return {}
    rows = session.execute(
        select(
            DragonTigerEventRevision.security_id,
            DragonTigerEventRevision.source_event_key,
            DragonTigerEventRevision.content_hash,
            DragonTigerEventRevision.source_batch_id,
            DragonTigerEventRevision.trade_date,
        )
        .join(
            SourceBatch,
            SourceBatch.source_batch_id == DragonTigerEventRevision.source_batch_id,
        )
        .where(
            DragonTigerEventRevision.security_id.in_(
                {identity.security_id for identity in identity_values}
            ),
            DragonTigerEventRevision.methodology_version_id == methodology_version_id,
            DragonTigerEventRevision.known_to.is_(None),
            DragonTigerEventRevision.trade_date
            >= min(identity.coverage_from for identity in identity_values),
            DragonTigerEventRevision.trade_date
            <= max(identity.coverage_to for identity in identity_values),
            SourceBatch.provider_id == provider_id,
        )
        .order_by(
            DragonTigerEventRevision.security_id,
            DragonTigerEventRevision.trade_date,
            DragonTigerEventRevision.source_event_key,
        )
    ).all()
    identities_by_security = _coverage_identities_by_security(identity_values)
    for security_id, source_key, content_hash, batch_id, trade_date in rows:
        identity = _coverage_identity_for_fact(
            identities_by_security=identities_by_security,
            security_id=int(security_id),
            fact_date=trade_date,
        )
        if identity is None:
            continue
        mutable_records[identity].append(
            CanonicalLineageRecord(
                record_key_hash=_hash(str(source_key)),
                content_hash=str(content_hash),
                source_batch_id=UUID(str(batch_id)),
                transform_hash=_hash(_DRAGON_MAPPING),
            )
        )
        mutable_dates[identity].append(trade_date)
    return _coverage_records_result(
        identities=identity_values,
        mutable_records=mutable_records,
        mutable_dates=mutable_dates,
    )


def _block_coverage_records_by_identity(
    session: Session,
    *,
    identities: Sequence[EventCoverageIdentity],
    methodology_version_id: UUID,
    provider_id: str,
) -> Mapping[EventCoverageIdentity, EventCoverageRecords]:
    """一次读取冻结 roster 的大宗逐笔血缘，并按交易日分配到唯一身份窗口。"""
    identity_values = tuple(identities)
    mutable_records: dict[EventCoverageIdentity, list[CanonicalLineageRecord]] = {
        identity: [] for identity in identity_values
    }
    mutable_dates: dict[EventCoverageIdentity, list[date]] = {
        identity: [] for identity in identity_values
    }
    if not identity_values:
        return {}
    rows = session.execute(
        select(
            BlockTradeExecutionRevision.security_id,
            BlockTradeExecutionRevision.source_event_key,
            BlockTradeExecutionRevision.occurrence_no,
            BlockTradeExecutionRevision.content_hash,
            BlockTradeExecutionRevision.source_batch_id,
            BlockTradeExecutionRevision.trade_date,
        )
        .join(
            SourceBatch,
            SourceBatch.source_batch_id == BlockTradeExecutionRevision.source_batch_id,
        )
        .where(
            BlockTradeExecutionRevision.security_id.in_(
                {identity.security_id for identity in identity_values}
            ),
            BlockTradeExecutionRevision.methodology_version_id == methodology_version_id,
            BlockTradeExecutionRevision.known_to.is_(None),
            BlockTradeExecutionRevision.trade_date
            >= min(identity.coverage_from for identity in identity_values),
            BlockTradeExecutionRevision.trade_date
            <= max(identity.coverage_to for identity in identity_values),
            SourceBatch.provider_id == provider_id,
        )
        .order_by(
            BlockTradeExecutionRevision.security_id,
            BlockTradeExecutionRevision.trade_date,
            BlockTradeExecutionRevision.source_event_key,
            BlockTradeExecutionRevision.occurrence_no,
        )
    ).all()
    identities_by_security = _coverage_identities_by_security(identity_values)
    for security_id, source_key, occurrence_no, content_hash, batch_id, trade_date in rows:
        identity = _coverage_identity_for_fact(
            identities_by_security=identities_by_security,
            security_id=int(security_id),
            fact_date=trade_date,
        )
        if identity is None:
            continue
        mutable_records[identity].append(
            CanonicalLineageRecord(
                record_key_hash=_hash(f"{source_key}:{occurrence_no}"),
                content_hash=str(content_hash),
                source_batch_id=UUID(str(batch_id)),
                transform_hash=_hash(_BLOCK_MAPPING),
            )
        )
        mutable_dates[identity].append(trade_date)
    return _coverage_records_result(
        identities=identity_values,
        mutable_records=mutable_records,
        mutable_dates=mutable_dates,
    )


def _coverage_identities_by_security(
    identities: Sequence[EventCoverageIdentity],
) -> Mapping[int, tuple[EventCoverageIdentity, ...]]:
    """按永久证券聚合冻结身份分段，供批量事实查询做常数级 SQL 后分桶。"""
    mutable: dict[int, list[EventCoverageIdentity]] = {}
    for identity in identities:
        mutable.setdefault(identity.security_id, []).append(identity)
    return {security_id: tuple(values) for security_id, values in mutable.items()}


def _coverage_identity_for_fact(
    *,
    identities_by_security: Mapping[int, Sequence[EventCoverageIdentity]],
    security_id: int,
    fact_date: date,
) -> EventCoverageIdentity | None:
    """把事实分配给唯一覆盖身份；窗口重叠会使 coverage 真值不确定并失败关闭。"""
    matches = [
        identity
        for identity in identities_by_security.get(security_id, ())
        if identity.coverage_from <= fact_date <= identity.coverage_to
    ]
    if len(matches) > 1:
        raise ValueError("trading event coverage identity windows overlap")
    return None if not matches else matches[0]


def _coverage_records_result(
    *,
    identities: Sequence[EventCoverageIdentity],
    mutable_records: Mapping[EventCoverageIdentity, Sequence[CanonicalLineageRecord]],
    mutable_dates: Mapping[EventCoverageIdentity, Sequence[date]],
) -> Mapping[EventCoverageIdentity, EventCoverageRecords]:
    """冻结批量分桶结果，并为真实空窗显式保留零记录映射。"""
    return {
        identity: EventCoverageRecords(
            records=tuple(mutable_records[identity]),
            fact_dates=tuple(mutable_dates[identity]),
        )
        for identity in identities
    }


def _dragon_candidate(
    session: Session,
    *,
    dataset_id: UUID,
    methodology_version_id: UUID,
    normalization_run_id: UUID,
    partition_key: str,
    current: Mapping[str, DragonTigerEventRevision],
    changed: Mapping[str, _PreparedDragon],
    source_batch_id: UUID,
    now: datetime,
    window_end: date,
) -> CanonicalReleaseCandidate:
    """合并当前龙虎榜和变化事件，未变化行继续指向其首次接受的来源批次。"""
    records: list[CanonicalLineageRecord] = []
    dates: list[date] = []
    for key in sorted({*current, *changed}):
        item = changed.get(key)
        if item is None:
            existing = current[key]
            content_hash, batch_id, trade_date = (
                existing.content_hash,
                UUID(str(existing.source_batch_id)),
                existing.trade_date,
            )
        else:
            content_hash, batch_id, trade_date = (
                item.content_hash,
                source_batch_id,
                item.value.trade_date,
            )
        dates.append(trade_date)
        records.append(
            CanonicalLineageRecord(
                record_key_hash=_hash(key),
                content_hash=content_hash,
                source_batch_id=batch_id,
                transform_hash=_hash(_DRAGON_MAPPING),
            )
        )
    return _candidate(
        session,
        dataset_id=dataset_id,
        dataset_code=_DRAGON_DATASET,
        methodology_version_id=methodology_version_id,
        normalization_run_id=normalization_run_id,
        partition_key=partition_key,
        records=records,
        dates=dates,
        checkpoint_position={"tradeDate": max(dates, default=window_end).isoformat()},
        policy_code="dragon-tiger.disclosure.quality",
        rule_code="source-seat-and-amount-reconciliation",
        now=now,
        publication_effective_as_of=window_end,
    )


def _block_candidate(
    session: Session,
    *,
    dataset_id: UUID,
    methodology_version_id: UUID,
    normalization_run_id: UUID,
    partition_key: str,
    current: Mapping[tuple[str, int], BlockTradeExecutionRevision],
    changed: Mapping[tuple[str, int], _PreparedBlock],
    source_batch_id: UUID,
    now: datetime,
    window_end: date,
) -> CanonicalReleaseCandidate:
    """合并当前大宗逐笔记录和变化项，来源键与 occurrence 一起构成稳定业务键。"""
    records: list[CanonicalLineageRecord] = []
    dates: list[date] = []
    for key in sorted({*current, *changed}):
        item = changed.get(key)
        if item is None:
            existing = current[key]
            content_hash, batch_id, trade_date = (
                existing.content_hash,
                UUID(str(existing.source_batch_id)),
                existing.trade_date,
            )
        else:
            content_hash, batch_id, trade_date = (
                item.content_hash,
                source_batch_id,
                item.value.trade_date,
            )
        dates.append(trade_date)
        records.append(
            CanonicalLineageRecord(
                record_key_hash=_hash(f"{key[0]}:{key[1]}"),
                content_hash=content_hash,
                source_batch_id=batch_id,
                transform_hash=_hash(_BLOCK_MAPPING),
            )
        )
    latest_key = max({*current, *changed}, default=("", 0))
    return _candidate(
        session,
        dataset_id=dataset_id,
        dataset_code=_BLOCK_DATASET,
        methodology_version_id=methodology_version_id,
        normalization_run_id=normalization_run_id,
        partition_key=partition_key,
        records=records,
        dates=dates,
        checkpoint_position={
            "tradeDate": max(dates, default=window_end).isoformat(),
            "sourceKey": latest_key[0],
        },
        policy_code="block-trade.execution.quality",
        rule_code="occurrence-preserved",
        now=now,
        publication_effective_as_of=window_end,
    )


def _candidate(
    session: Session,
    *,
    dataset_id: UUID,
    dataset_code: str,
    methodology_version_id: UUID,
    normalization_run_id: UUID,
    partition_key: str,
    records: Sequence[CanonicalLineageRecord],
    dates: Sequence[date],
    checkpoint_position: dict[str, object],
    policy_code: str,
    rule_code: str,
    now: datetime,
    publication_effective_as_of: date,
) -> CanonicalReleaseCandidate:
    """构造带 fencing token 的 canonical release 候选，防止并发同步倒退检查点。"""
    fencing_token = session.execute(
        select(CanonicalCheckpoint.fencing_token)
        .where(
            CanonicalCheckpoint.dataset_id == dataset_id,
            CanonicalCheckpoint.partition_key == partition_key,
            CanonicalCheckpoint.checkpoint_kind == "published",
        )
        .with_for_update()
    ).scalar_one_or_none()
    return CanonicalReleaseCandidate(
        dataset_id=dataset_id,
        dataset_code=dataset_code,
        partition_key=partition_key,
        methodology_version_id=methodology_version_id,
        normalization_run_id=normalization_run_id,
        records=tuple(records),
        quality=CanonicalQualityDecision(
            status="passed",
            policy_code=policy_code,
            policy_version=1,
            rules=(CanonicalQualityRule(rule_code, "blocking", True),),
        ),
        fact_min=min(dates) if dates else None,
        fact_max=max(dates) if dates else None,
        checkpoint_kind="published",
        checkpoint_position=checkpoint_position,
        expected_fencing_token=0 if fencing_token is None else int(fencing_token),
        created_at=now,
        publication_effective_as_of=publication_effective_as_of,
    )


def _dragon_hash(value: DragonTigerEvent, reason_family: str) -> str:
    """计算包含原因映射和席位集合的摘要，任何披露修订都触发新事件版本。"""
    return _hash_payload(
        {
            "sourceEventKey": value.source_event_key,
            "securityCode": value.source_security_code,
            "tradeDate": value.trade_date.isoformat(),
            "reasonCode": value.reason_code,
            "reasonText": value.reason_text,
            "reasonFamily": reason_family,
            "closePrice": _decimal(value.close_price),
            "buyAmount": _decimal(value.buy_amount),
            "sellAmount": _decimal(value.sell_amount),
            "netAmount": _decimal(value.net_amount),
            "dealAmount": _decimal(value.deal_amount),
            "marketTurnoverAmount": _decimal(value.market_turnover_amount),
            "dealRatio": _decimal(value.deal_ratio),
            "netRatio": _decimal(value.net_ratio),
            "turnoverRatio": _decimal(value.turnover_ratio),
            "sourcePublishedAt": _datetime(value.source_published_at),
            "visibleAt": _datetime(value.visible_at),
            "visibleTimePrecision": value.visible_time_precision,
            "seats": [
                {
                    "side": seat.list_side,
                    "rank": seat.rank,
                    "seatCode": seat.seat_code,
                    "seatName": seat.seat_name,
                    "buyAmount": _decimal(seat.buy_amount),
                    "sellAmount": _decimal(seat.sell_amount),
                    "netAmount": _decimal(seat.net_amount),
                    "buyRatio": _decimal(seat.buy_ratio),
                    "sellRatio": _decimal(seat.sell_ratio),
                }
                for seat in value.seats
            ],
        }
    )


def _block_economic_fingerprint(value: BlockTrade) -> str:
    """计算不含来源键和 occurrence 的经济字段摘要，用于审计相同经济成交的合法重数。"""
    return _hash_payload(
        {
            "securityCode": value.source_security_code,
            "tradeDate": value.trade_date.isoformat(),
            "executionPrice": _decimal(value.execution_price),
            "quantityShares": value.quantity_shares,
            "notionalCny": _decimal(value.notional_cny),
            "buyerSeatCode": value.buyer_seat_code,
            "buyerSeatName": value.buyer_seat_name,
            "sellerSeatCode": value.seller_seat_code,
            "sellerSeatName": value.seller_seat_name,
        }
    )


def _block_hash(value: BlockTrade, economic_fingerprint: str) -> str:
    """计算完整逐笔摘要，来源键、occurrence、可见时间和参考价均参与 revision 身份。"""
    return _hash_payload(
        {
            "sourceTradeKey": value.source_trade_key,
            "occurrenceNo": value.occurrence_no,
            "economicFingerprint": economic_fingerprint,
            "referenceClosePrice": _decimal(value.reference_close_price),
            "premiumDiscountRatio": _decimal(value.premium_discount_ratio),
            "sourceDailyRank": value.source_daily_rank,
            "sourcePublishedAt": _datetime(value.source_published_at),
            "visibleAt": _datetime(value.visible_at),
            "visibleTimePrecision": value.visible_time_precision,
        }
    )


def _hash_payload(payload: Mapping[str, object]) -> str:
    """以规范 JSON 计算 SHA-256，精确数值先转文本避免浮点格式制造伪修订。"""
    return _hash(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _hash(value: str) -> str:
    """计算 UTF-8 SHA-256，用于内容、映射和业务键摘要。"""
    return hashlib.sha256(value.encode()).hexdigest()


def _decimal(value: object) -> str | None:
    """把可选精确数值稳定投影为字符串，未披露空值不会与真实零混同。"""
    return None if value is None else str(value)


def _datetime(value: datetime | None) -> str | None:
    """把可选带时区时间稳定转为 ISO 文本，避免序列化实现影响内容摘要。"""
    return None if value is None else value.isoformat()
