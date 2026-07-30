"""股票中心普通停牌、股本结构与申万归属的 fenced canonical 发布仓储。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from service_data_sync.application.ports.canonical_release import (
    CanonicalLineageRecord,
    CanonicalQualityDecision,
    CanonicalQualityRule,
    CanonicalReleaseCandidate,
)
from service_data_sync.application.ports.equity_workspace import (
    EquityWorkspaceRepository,
    EquityWorkspaceSourceObservation,
    PublishedEquityWorkspaceDataset,
)
from service_data_sync.domain.equity import EquityIdentifier
from service_data_sync.domain.equity_workspace import (
    EquityShareCapital,
    EquityTradingStatus,
    SwEquityMembership,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import CanonicalCheckpoint
from service_data_sync.infrastructure.database.models.equity.workspace import (
    EquityShareCapitalRevision,
    EquityTradingStatusRevision,
    SwMembershipItem,
    SwMembershipRelease,
)
from service_data_sync.infrastructure.persistence.canonical_release_repository import (
    SqlAlchemyCanonicalReleaseRepository,
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

from ..database.models.equity.identity.equity_identifier_version import (
    EquityIdentifierVersion,
)
from ..database.models.equity.identity.equity_instrument import EquityInstrument

_TRADING_DATASET = "equity.trading_status.1d"
_TRADING_MAPPING = "equity-trading-status-v1"
_SHARE_CAPITAL_DATASET = "equity.share_capital.reported"
_SHARE_CAPITAL_MAPPING = "equity-share-capital-v1"
_SW_MEMBERSHIP_DATASET = "sector.sw2021.membership.snapshot"
_SW_MEMBERSHIP_MAPPING = "sw2021-membership-v1"
_DOCUMENTATION = "docs/service-web/0007-equity-market-workspace/index.html"


@dataclass(frozen=True, slots=True)
class EquityWorkspaceSourceApproval(TypedP0SourceApproval):
    """标记股票中心新增事实可使用的显式内部研究来源批准。"""


@dataclass(frozen=True, slots=True)
class _PreparedRevision:
    """保存待插入事实的内容摘要与递增修订号。"""

    value: EquityTradingStatus | EquityShareCapital
    content_hash: str
    revision: int


class SqlAlchemyEquityWorkspaceRepository(EquityWorkspaceRepository):
    """以真实来源证据原子发布股票中心新增事实及申万成分快照。"""

    def __init__(
        self,
        database: DatabaseClient,
        *,
        approved_sources: Mapping[str, EquityWorkspaceSourceApproval] | None = None,
    ) -> None:
        """保存事务工厂和显式来源批准；未提供批准时所有写入安全失败。"""
        self._database = database
        self._approved_sources = dict(approved_sources or {})
        self._releases = SqlAlchemyCanonicalReleaseRepository(database)

    def publish_trading_statuses(
        self,
        *,
        observation_date: date,
        statuses: Sequence[EquityTradingStatus],
        source: EquityWorkspaceSourceObservation,
    ) -> PublishedEquityWorkspaceDataset:
        """发布一个日期完整停牌清单，并关闭来源已撤销的旧当前修订。"""
        values = tuple(statuses)
        if any(item.trade_date != observation_date for item in values):
            raise ValueError("trading status date does not match publication partition")
        if len({item.identifier for item in values}) != len(values):
            raise ValueError("trading status partition contains duplicate securities")
        approval = self._approval(source)
        prepared: list[_PreparedRevision] = []
        resolved: dict[EquityIdentifier, int] = {}
        removed: list[EquityTradingStatusRevision] = []
        source_batch_id: UUID | None = None

        def prepare_candidate(session: Session) -> CanonicalReleaseCandidate:
            """准备来源、身份、当前快照和完整 release 血缘。"""
            nonlocal source_batch_id
            now = datetime.now(UTC)
            dataset_id = ensure_dataset(
                session,
                code=_TRADING_DATASET,
                domain="equity",
                grain="security + trading date + reported ordinary trading status",
                now=now,
            )
            methodology_id = ensure_methodology(
                session,
                code=_TRADING_MAPPING,
                semantic_family="reported-trading-status",
                mapping_version=_TRADING_MAPPING,
                documentation_ref=_DOCUMENTATION,
            )
            source_batch_id = self._record_source(
                session,
                approval=approval,
                source=source,
                native_grain="security + observation date",
                dataset_id=dataset_id,
                partition_key=f"date:{observation_date.isoformat()}",
                mapping_version=_TRADING_MAPPING,
                now=now,
            )[0]
            for value in values:
                resolved[value.identifier] = _resolve_equity_identity(
                    session, identifier=value.identifier, fact_date=observation_date
                )
            current_rows = (
                session.execute(
                    select(EquityTradingStatusRevision).where(
                        EquityTradingStatusRevision.trade_date == observation_date,
                        EquityTradingStatusRevision.known_to.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            current = {row.security_id: row for row in current_rows}
            if len(current) != len(current_rows):
                raise ValueError("current trading status revisions are ambiguous")
            incoming = {resolved[item.identifier]: item for item in values}
            removed[:] = [row for key, row in current.items() if key not in incoming]
            prepared[:] = [
                _PreparedRevision(
                    value=value,
                    content_hash=_trading_content_hash(value),
                    revision=(current[security_id].revision + 1 if security_id in current else 1),
                )
                for security_id, value in incoming.items()
                if security_id not in current
                or _trading_content_hash(value) != bytes(current[security_id].content_sha256).hex()
            ]
            changed = {
                resolved[item.value.identifier]: item
                for item in prepared
                if isinstance(item.value, EquityTradingStatus)
            }
            records = tuple(
                _lineage(
                    record_key=f"{security_id}:{observation_date.isoformat()}",
                    content_hash=(
                        changed[security_id].content_hash
                        if security_id in changed
                        else bytes(current[security_id].content_sha256).hex()
                    ),
                    source_batch_id=(
                        source_batch_id
                        if security_id in changed
                        else UUID(str(current[security_id].source_batch_id))
                    ),
                    mapping_version=_TRADING_MAPPING,
                )
                for security_id in sorted(incoming)
            )
            normalization_run_id = self._normalization_run(
                session,
                dataset_id=dataset_id,
                partition_key=f"date:{observation_date.isoformat()}",
                source=source,
                source_batch_id=source_batch_id,
                mapping_version=_TRADING_MAPPING,
                now=now,
            )
            return CanonicalReleaseCandidate(
                dataset_id=dataset_id,
                dataset_code=_TRADING_DATASET,
                partition_key=f"date:{observation_date.isoformat()}",
                methodology_version_id=methodology_id,
                normalization_run_id=normalization_run_id,
                records=records,
                quality=CanonicalQualityDecision(
                    status="passed",
                    policy_code="equity.trading-status.quality",
                    policy_version=1,
                    rules=(
                        CanonicalQualityRule("identity-resolution", "blocking", True),
                        CanonicalQualityRule("ordinary-status-only", "blocking", True),
                    ),
                ),
                fact_min=observation_date,
                fact_max=observation_date,
                checkpoint_kind="observation_date",
                checkpoint_position={"observationDate": observation_date.isoformat()},
                expected_fencing_token=_checkpoint_token(
                    session,
                    dataset_id=dataset_id,
                    partition_key=f"date:{observation_date.isoformat()}",
                    kind="observation_date",
                ),
                created_at=now,
            )

        def write_facts(
            session: Session, candidate: CanonicalReleaseCandidate, release_id: UUID
        ) -> None:
            """关闭被替换或撤销的当前知识区间，并插入内容变化修订。"""
            del release_id
            if source_batch_id is None:
                raise AssertionError("trading status source batch was not prepared")
            for row in (*removed,):
                session.execute(
                    update(EquityTradingStatusRevision)
                    .where(
                        EquityTradingStatusRevision.security_id == row.security_id,
                        EquityTradingStatusRevision.trade_date == row.trade_date,
                        EquityTradingStatusRevision.revision == row.revision,
                        EquityTradingStatusRevision.known_to.is_(None),
                    )
                    .values(known_to=candidate.created_at)
                )
            for item in prepared:
                value = item.value
                if not isinstance(value, EquityTradingStatus):
                    raise AssertionError("trading status preparation contains another fact type")
                security_id = resolved[value.identifier]
                session.execute(
                    update(EquityTradingStatusRevision)
                    .where(
                        EquityTradingStatusRevision.security_id == security_id,
                        EquityTradingStatusRevision.trade_date == value.trade_date,
                        EquityTradingStatusRevision.known_to.is_(None),
                    )
                    .values(known_to=candidate.created_at)
                )
                session.execute(
                    insert(EquityTradingStatusRevision).values(
                        security_id=security_id,
                        trade_date=value.trade_date,
                        revision=item.revision,
                        status=value.status,
                        market=value.identifier.exchange.value,
                        suspended_at=None,
                        resumed_at=None,
                        reason=value.reason,
                        source_batch_id=source_batch_id,
                        content_sha256=bytes.fromhex(item.content_hash),
                        known_from=candidate.created_at,
                        known_to=None,
                    )
                )
                record_manifest(
                    session,
                    normalization_run_id=candidate.normalization_run_id,
                    record_key_hash=_sha256(f"{security_id}:{value.trade_date.isoformat()}"),
                    canonical_table=EquityTradingStatusRevision.__tablename__,
                    canonical_pk={
                        "securityId": str(security_id),
                        "tradeDate": value.trade_date.isoformat(),
                        "revision": str(item.revision),
                    },
                    content_hash=item.content_hash,
                )

        published = self._releases.publish_prepared(
            prepare_candidate=prepare_candidate,
            write_facts=write_facts,
        )
        return PublishedEquityWorkspaceDataset(
            data_version=published.data_version,
            inserted_count=len(prepared),
            unchanged_count=len(values) - len(prepared),
        )

    def publish_share_capital(
        self,
        *,
        identifier: EquityIdentifier,
        instrument_id: UUID,
        identity_as_of: date,
        structures: Sequence[EquityShareCapital],
        source: EquityWorkspaceSourceObservation,
    ) -> PublishedEquityWorkspaceDataset:
        """按冻结永久身份发布完整股本历史，并保留内容未变日期的原始血缘。"""
        values = tuple(structures)
        if not values or any(item.identifier != identifier for item in values):
            raise ValueError("share capital requires one non-empty instrument partition")
        if len({item.effective_on for item in values}) != len(values):
            raise ValueError("share capital contains duplicate effective dates")
        approval = self._approval(source)
        prepared: list[_PreparedRevision] = []
        removed: list[EquityShareCapitalRevision] = []
        security_id: int | None = None
        source_batch_id: UUID | None = None

        def prepare_candidate(session: Session) -> CanonicalReleaseCandidate:
            """准备一只证券完整历史快照及其内容变化修订。"""
            nonlocal security_id, source_batch_id
            now = datetime.now(UTC)
            security_id = _resolve_frozen_equity_identity(
                session,
                identifier=identifier,
                instrument_id=instrument_id,
                identity_as_of=identity_as_of,
            )
            partition_key = f"security:{security_id}"
            dataset_id = ensure_dataset(
                session,
                code=_SHARE_CAPITAL_DATASET,
                domain="equity",
                grain="security + effective date + reported share capital",
                now=now,
            )
            methodology_id = ensure_methodology(
                session,
                code=_SHARE_CAPITAL_MAPPING,
                semantic_family="reported-share-capital",
                mapping_version=_SHARE_CAPITAL_MAPPING,
                documentation_ref=_DOCUMENTATION,
            )
            source_batch_id = self._record_source(
                session,
                approval=approval,
                source=source,
                native_grain="security + capital effective date",
                dataset_id=dataset_id,
                partition_key=partition_key,
                mapping_version=_SHARE_CAPITAL_MAPPING,
                now=now,
            )[0]
            current_rows = (
                session.execute(
                    select(EquityShareCapitalRevision).where(
                        EquityShareCapitalRevision.security_id == security_id,
                        EquityShareCapitalRevision.known_to.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            current = {row.effective_on: row for row in current_rows}
            if len(current) != len(current_rows):
                raise ValueError("current share capital revisions are ambiguous")
            incoming = {item.effective_on: item for item in values}
            removed[:] = [row for key, row in current.items() if key not in incoming]
            prepared[:] = [
                _PreparedRevision(
                    value=value,
                    content_hash=_share_capital_content_hash(value),
                    revision=(current[day].revision + 1 if day in current else 1),
                )
                for day, value in incoming.items()
                if day not in current
                or _share_capital_content_hash(value) != bytes(current[day].content_sha256).hex()
            ]
            changed = {
                item.value.effective_on: item
                for item in prepared
                if isinstance(item.value, EquityShareCapital)
            }
            records = tuple(
                _lineage(
                    record_key=f"{security_id}:{day.isoformat()}",
                    content_hash=(
                        changed[day].content_hash
                        if day in changed
                        else bytes(current[day].content_sha256).hex()
                    ),
                    source_batch_id=(
                        source_batch_id
                        if day in changed
                        else UUID(str(current[day].source_batch_id))
                    ),
                    mapping_version=_SHARE_CAPITAL_MAPPING,
                )
                for day in sorted(incoming)
            )
            normalization_run_id = self._normalization_run(
                session,
                dataset_id=dataset_id,
                partition_key=partition_key,
                source=source,
                source_batch_id=source_batch_id,
                mapping_version=_SHARE_CAPITAL_MAPPING,
                now=now,
            )
            fact_dates = tuple(sorted(incoming))
            return CanonicalReleaseCandidate(
                dataset_id=dataset_id,
                dataset_code=_SHARE_CAPITAL_DATASET,
                partition_key=partition_key,
                methodology_version_id=methodology_id,
                normalization_run_id=normalization_run_id,
                records=records,
                quality=CanonicalQualityDecision(
                    status="passed",
                    policy_code="equity.share-capital.quality",
                    policy_version=1,
                    rules=(
                        CanonicalQualityRule("identity-resolution", "blocking", True),
                        CanonicalQualityRule("share-components-bounded", "blocking", True),
                    ),
                ),
                fact_min=min(fact_dates),
                fact_max=max(fact_dates),
                checkpoint_kind="effective_date",
                checkpoint_position={"effectiveOn": max(fact_dates).isoformat()},
                expected_fencing_token=_checkpoint_token(
                    session,
                    dataset_id=dataset_id,
                    partition_key=partition_key,
                    kind="effective_date",
                ),
                created_at=now,
            )

        def write_facts(
            session: Session, candidate: CanonicalReleaseCandidate, release_id: UUID
        ) -> None:
            """关闭旧股本知识区间并插入变化修订。"""
            del release_id
            if security_id is None or source_batch_id is None:
                raise AssertionError("share capital source or identity was not prepared")
            for row in removed:
                session.execute(
                    update(EquityShareCapitalRevision)
                    .where(
                        EquityShareCapitalRevision.security_id == row.security_id,
                        EquityShareCapitalRevision.effective_on == row.effective_on,
                        EquityShareCapitalRevision.revision == row.revision,
                        EquityShareCapitalRevision.known_to.is_(None),
                    )
                    .values(known_to=candidate.created_at)
                )
            for item in prepared:
                value = item.value
                if not isinstance(value, EquityShareCapital):
                    raise AssertionError("share capital preparation contains another fact type")
                session.execute(
                    update(EquityShareCapitalRevision)
                    .where(
                        EquityShareCapitalRevision.security_id == security_id,
                        EquityShareCapitalRevision.effective_on == value.effective_on,
                        EquityShareCapitalRevision.known_to.is_(None),
                    )
                    .values(known_to=candidate.created_at)
                )
                session.execute(
                    insert(EquityShareCapitalRevision).values(
                        security_id=security_id,
                        effective_on=value.effective_on,
                        revision=item.revision,
                        total_shares=value.total_shares,
                        listed_tradable_a_shares=value.listed_tradable_a_shares,
                        restricted_shares=value.restricted_shares,
                        change_reason=value.change_reason,
                        source_batch_id=source_batch_id,
                        content_sha256=bytes.fromhex(item.content_hash),
                        known_from=candidate.created_at,
                        known_to=None,
                    )
                )
                record_manifest(
                    session,
                    normalization_run_id=candidate.normalization_run_id,
                    record_key_hash=_sha256(f"{security_id}:{value.effective_on.isoformat()}"),
                    canonical_table=EquityShareCapitalRevision.__tablename__,
                    canonical_pk={
                        "securityId": str(security_id),
                        "effectiveOn": value.effective_on.isoformat(),
                        "revision": str(item.revision),
                    },
                    content_hash=item.content_hash,
                )

        published = self._releases.publish_prepared(
            prepare_candidate=prepare_candidate,
            write_facts=write_facts,
        )
        return PublishedEquityWorkspaceDataset(
            data_version=published.data_version,
            inserted_count=len(prepared),
            unchanged_count=len(values) - len(prepared),
        )

    def publish_sw_memberships(
        self,
        *,
        node_code: str,
        observation_date: date,
        memberships: Sequence[SwEquityMembership],
        source: EquityWorkspaceSourceObservation,
    ) -> PublishedEquityWorkspaceDataset:
        """发布一个申万三级节点的非空完整快照，身份缺失或歧义时全批失败。"""
        values = tuple(memberships)
        if not values or any(
            item.node_code != node_code or item.observed_on != observation_date for item in values
        ):
            raise ValueError("SW membership requires one non-empty node snapshot")
        if len({item.symbol for item in values}) != len(values):
            raise ValueError("SW membership contains duplicate symbols")
        if any(
            item.source_included_on is not None and item.source_included_on > observation_date
            for item in values
        ):
            raise ValueError("SW membership inclusion date is after observation date")
        approval = self._approval(source)
        resolved: dict[str, int] = {}
        source_batch_id: UUID | None = None

        def prepare_candidate(session: Session) -> CanonicalReleaseCandidate:
            """解析全部证券并准备节点级不可变快照。"""
            nonlocal source_batch_id
            now = datetime.now(UTC)
            partition_key = f"SW2021:{node_code}"
            dataset_id = ensure_dataset(
                session,
                code=_SW_MEMBERSHIP_DATASET,
                domain="sector",
                grain="SW2021 third-level node + security + observation date",
                now=now,
            )
            methodology_id = ensure_methodology(
                session,
                code=_SW_MEMBERSHIP_MAPPING,
                semantic_family="reported-sector-membership",
                mapping_version=_SW_MEMBERSHIP_MAPPING,
                documentation_ref=_DOCUMENTATION,
            )
            source_batch_id = self._record_source(
                session,
                approval=approval,
                source=source,
                native_grain="SW third-level node + current constituent",
                dataset_id=dataset_id,
                partition_key=partition_key,
                mapping_version=_SW_MEMBERSHIP_MAPPING,
                now=now,
            )[0]
            for item in values:
                resolved[item.symbol] = _resolve_symbol_without_exchange(
                    session, symbol=item.symbol, fact_date=observation_date
                )
            records = tuple(
                _lineage(
                    record_key=f"{node_code}:{resolved[item.symbol]}",
                    content_hash=_sw_membership_content_hash(item, resolved[item.symbol]),
                    source_batch_id=source_batch_id,
                    mapping_version=_SW_MEMBERSHIP_MAPPING,
                )
                for item in sorted(values, key=lambda current: current.symbol)
            )
            normalization_run_id = self._normalization_run(
                session,
                dataset_id=dataset_id,
                partition_key=partition_key,
                source=source,
                source_batch_id=source_batch_id,
                mapping_version=_SW_MEMBERSHIP_MAPPING,
                now=now,
            )
            return CanonicalReleaseCandidate(
                dataset_id=dataset_id,
                dataset_code=_SW_MEMBERSHIP_DATASET,
                partition_key=partition_key,
                methodology_version_id=methodology_id,
                normalization_run_id=normalization_run_id,
                records=records,
                quality=CanonicalQualityDecision(
                    status="passed",
                    policy_code="sector.sw2021-membership.quality",
                    policy_version=1,
                    rules=(
                        CanonicalQualityRule("identity-resolution", "blocking", True),
                        CanonicalQualityRule("non-empty-node", "blocking", True),
                    ),
                ),
                fact_min=observation_date,
                fact_max=observation_date,
                checkpoint_kind="observation_date",
                checkpoint_position={"observationDate": observation_date.isoformat()},
                expected_fencing_token=_checkpoint_token(
                    session,
                    dataset_id=dataset_id,
                    partition_key=partition_key,
                    kind="observation_date",
                ),
                created_at=now,
            )

        def write_facts(
            session: Session, candidate: CanonicalReleaseCandidate, release_id: UUID
        ) -> None:
            """写入 release 头与全部已解析成分，旧 release 保持不可变。"""
            if source_batch_id is None:
                raise AssertionError("SW membership source batch was not prepared")
            session.execute(
                insert(SwMembershipRelease).values(
                    release_id=release_id,
                    scheme_version="SW2021",
                    node_code=node_code,
                    observation_date=observation_date,
                    source_batch_id=source_batch_id,
                    quality_status="passed",
                    record_count=len(values),
                    published_at=candidate.created_at,
                )
            )
            for item in values:
                security_id = resolved[item.symbol]
                session.execute(
                    insert(SwMembershipItem).values(
                        release_id=release_id,
                        security_id=security_id,
                        third_level_node_code=node_code,
                        source_included_on=item.source_included_on,
                        source_symbol=item.symbol,
                        resolution_status="RESOLVED",
                    )
                )
                record_manifest(
                    session,
                    normalization_run_id=candidate.normalization_run_id,
                    record_key_hash=_sha256(f"{node_code}:{security_id}"),
                    canonical_table=SwMembershipItem.__tablename__,
                    canonical_pk={
                        "releaseId": str(release_id),
                        "securityId": str(security_id),
                    },
                    content_hash=_sw_membership_content_hash(item, security_id),
                )

        published = self._releases.publish_prepared(
            prepare_candidate=prepare_candidate,
            write_facts=write_facts,
        )
        return PublishedEquityWorkspaceDataset(
            data_version=published.data_version,
            inserted_count=0 if published.reused_release else len(values),
            unchanged_count=len(values) if published.reused_release else 0,
        )

    def _approval(self, source: EquityWorkspaceSourceObservation) -> EquityWorkspaceSourceApproval:
        """返回精确 provider 批准项，缺失时拒绝发布。"""
        approval = self._approved_sources.get(source.provider_id)
        if approval is None:
            raise ValueError("equity workspace source provider is not approved")
        return approval

    def _record_source(
        self,
        session: Session,
        *,
        approval: EquityWorkspaceSourceApproval,
        source: EquityWorkspaceSourceObservation,
        native_grain: str,
        dataset_id: UUID,
        partition_key: str,
        mapping_version: str,
        now: datetime,
    ) -> tuple[UUID, UUID]:
        """登记来源产品和批次；标准化运行在完成身份解析后单独建立。"""
        source_dataset_id = ensure_source_dataset(
            session,
            approval=approval,
            capability=source.capability,
            native_grain=native_grain,
        )
        source_batch_id = record_source_batch(
            session,
            source=source,
            source_dataset_id=source_dataset_id,
            now=now,
        )
        del dataset_id, partition_key, mapping_version
        return source_batch_id, source_dataset_id

    def _normalization_run(
        self,
        session: Session,
        *,
        dataset_id: UUID,
        partition_key: str,
        source: EquityWorkspaceSourceObservation,
        source_batch_id: UUID,
        mapping_version: str,
        now: datetime,
    ) -> UUID:
        """创建或复用当前输入和映射的标准化运行。"""
        return record_normalization_run(
            session,
            dataset_id=dataset_id,
            partition_key=partition_key,
            source=source,
            source_batch_id=source_batch_id,
            mapping_version=mapping_version,
            now=now,
        )


def _resolve_equity_identity(
    session: Session, *, identifier: EquityIdentifier, fact_date: date
) -> int:
    """按交易所、代码、事实日和当前知识解析唯一已确认永久证券。"""
    rows = (
        session.execute(
            select(EquityIdentifierVersion.security_id).where(
                EquityIdentifierVersion.exchange == identifier.exchange.value,
                EquityIdentifierVersion.symbol == identifier.symbol,
                EquityIdentifierVersion.identity_state == "CONFIRMED",
                EquityIdentifierVersion.effective_from <= fact_date,
                (EquityIdentifierVersion.effective_to.is_(None))
                | (EquityIdentifierVersion.effective_to > fact_date),
                EquityIdentifierVersion.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    values = {int(row) for row in rows}
    if len(values) != 1:
        raise ValueError("equity identity is missing or ambiguous")
    return values.pop()


def _resolve_frozen_equity_identity(
    session: Session,
    *,
    identifier: EquityIdentifier,
    instrument_id: UUID,
    identity_as_of: date,
) -> int:
    """按冻结 UUID 与当日代码版本解析唯一证券，禁止内容日期把代码复用映射到另一身份。"""
    rows = (
        session.execute(
            select(EquityInstrument.security_id)
            .join(
                EquityIdentifierVersion,
                EquityIdentifierVersion.security_id == EquityInstrument.security_id,
            )
            .where(
                EquityInstrument.instrument_id == instrument_id,
                EquityIdentifierVersion.exchange == identifier.exchange.value,
                EquityIdentifierVersion.symbol == identifier.symbol,
                EquityIdentifierVersion.identity_state == "CONFIRMED",
                EquityIdentifierVersion.effective_from <= identity_as_of,
                (EquityIdentifierVersion.effective_to.is_(None))
                | (EquityIdentifierVersion.effective_to > identity_as_of),
                EquityIdentifierVersion.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    values = {int(row) for row in rows}
    if len(values) != 1:
        raise ValueError("frozen equity identity is missing or ambiguous")
    return values.pop()


def _resolve_symbol_without_exchange(session: Session, *, symbol: str, fact_date: date) -> int:
    """仅在六位代码跨交易所唯一时解析申万来源身份；不按代码前缀猜场所。"""
    rows = (
        session.execute(
            select(EquityIdentifierVersion.security_id).where(
                EquityIdentifierVersion.symbol == symbol,
                EquityIdentifierVersion.identity_state == "CONFIRMED",
                EquityIdentifierVersion.effective_from <= fact_date,
                (EquityIdentifierVersion.effective_to.is_(None))
                | (EquityIdentifierVersion.effective_to > fact_date),
                EquityIdentifierVersion.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    values = {int(row) for row in rows}
    if len(values) != 1:
        raise ValueError("SW membership security identity is missing or ambiguous")
    return values.pop()


def _checkpoint_token(session: Session, *, dataset_id: UUID, partition_key: str, kind: str) -> int:
    """锁定 canonical 分区检查点并返回发布 CAS 期望 token。"""
    token = session.execute(
        select(CanonicalCheckpoint.fencing_token)
        .where(
            CanonicalCheckpoint.dataset_id == dataset_id,
            CanonicalCheckpoint.partition_key == partition_key,
            CanonicalCheckpoint.checkpoint_kind == kind,
        )
        .with_for_update()
    ).scalar_one_or_none()
    return 0 if token is None else int(token)


def _lineage(
    *,
    record_key: str,
    content_hash: str,
    source_batch_id: UUID,
    mapping_version: str,
) -> CanonicalLineageRecord:
    """构造一条直接来源血缘。"""
    return CanonicalLineageRecord(
        record_key_hash=_sha256(record_key),
        content_hash=content_hash,
        source_batch_id=source_batch_id,
        transform_hash=_sha256(mapping_version),
    )


def _trading_content_hash(value: EquityTradingStatus) -> str:
    """计算普通交易状态业务内容摘要。"""
    return _json_hash(
        {
            "exchange": value.identifier.exchange.value,
            "symbol": value.identifier.symbol,
            "tradeDate": value.trade_date.isoformat(),
            "status": value.status,
            "reason": value.reason,
        }
    )


def _share_capital_content_hash(value: EquityShareCapital) -> str:
    """计算股本结构业务内容摘要。"""
    return _json_hash(
        {
            "exchange": value.identifier.exchange.value,
            "symbol": value.identifier.symbol,
            "effectiveOn": value.effective_on.isoformat(),
            "totalShares": str(value.total_shares),
            "listedTradableAShares": _decimal_text(value.listed_tradable_a_shares),
            "restrictedShares": _decimal_text(value.restricted_shares),
            "changeReason": value.change_reason,
        }
    )


def _sw_membership_content_hash(value: SwEquityMembership, security_id: int) -> str:
    """计算申万成分业务内容摘要，名称变化不改变证券归属身份但仍进入版本内容。"""
    return _json_hash(
        {
            "nodeCode": value.node_code,
            "securityId": security_id,
            "symbol": value.symbol,
            "name": value.name,
            "observedOn": value.observed_on.isoformat(),
            "sourceIncludedOn": (
                None if value.source_included_on is None else value.source_included_on.isoformat()
            ),
        }
    )


def _json_hash(value: dict[str, object]) -> str:
    """对规范字段排序后计算 SHA-256。"""
    return _sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _sha256(value: str) -> str:
    """计算小写十六进制 SHA-256。"""
    return hashlib.sha256(value.encode()).hexdigest()


def _decimal_text(value: Decimal | None) -> str | None:
    """稳定投影可选十进制，空值保持为空。"""
    return None if value is None else str(value)
