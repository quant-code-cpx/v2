"""市场概览组件、完整包和原子 current pointer 的 PostgreSQL 仓储。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from service_data_sync.application.ports.market_overview import (
    MarketBundlePointerResult,
    MarketComponentCandidate,
    MarketOverviewRepository,
    PublishedMarketBundle,
    StoredMarketBundle,
    StoredMarketComponent,
    StoredMarketSnapshot,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.market import (
    MarketOverviewActiveBundle,
    MarketOverviewBundle,
    MarketOverviewBundleComponent,
    MarketOverviewComponentRelease,
    MarketOverviewCurrentPointer,
    MarketOverviewDerivationInputPointer,
    MarketOverviewPointerTransition,
)

_MARKET = "CN-A-SSE-SZSE"


def _validate_overview_quality(
    *,
    overview: dict[str, Any],
    components: tuple[MarketComponentCandidate, ...],
) -> None:
    """在落库前验证首页质量证据与组件 manifest 一一对应，拒绝不可解释的通过状态。"""
    quality = overview.get("quality")
    if not isinstance(quality, dict):
        raise ValueError("market overview quality evidence is required")
    checks = quality.get("checks")
    if (
        not isinstance(checks, list)
        or not checks
        or any(not isinstance(item, dict) or item.get("status") != "passed" for item in checks)
    ):
        raise ValueError("market overview requires passed quality checks")
    expected_count = len(components)
    if (
        quality.get("componentCount") != expected_count
        or quality.get("passedCount") != expected_count
    ):
        raise ValueError("market overview quality component count does not match manifest")
    bindings = quality.get("sourceBindings")
    if not isinstance(bindings, list) or len(bindings) != expected_count:
        raise ValueError("market overview source bindings do not match manifest")
    expected_datasets = {component.dataset_code for component in components}
    actual_datasets = {
        str(binding.get("component"))
        for binding in bindings
        if isinstance(binding, dict) and binding.get("component") is not None
    }
    if actual_datasets != expected_datasets:
        raise ValueError("market overview source binding datasets do not match manifest")


class SqlAlchemyMarketOverviewRepository(MarketOverviewRepository):
    """在一个事务内保存完整组件 manifest、bundle 和 current pointer。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存短生命周期数据库访问入口，不跨调用持有 Session。"""
        self._database = database

    def publish_complete_bundle(
        self,
        *,
        trade_date: date,
        components: tuple[MarketComponentCandidate, ...],
        overview: dict[str, Any],
    ) -> PublishedMarketBundle:
        """幂等保存完整包；只有全部组件成功后才比较并切换 latest 指针。"""
        if not components:
            raise ValueError("market overview bundle requires components")
        _validate_overview_quality(overview=overview, components=components)
        dataset_codes = [component.dataset_code for component in components]
        if len(dataset_codes) != len(set(dataset_codes)):
            raise ValueError("market overview bundle has duplicate component datasets")
        if any(
            component.trade_date is not None and component.trade_date > trade_date
            for component in components
        ):
            raise ValueError("market overview bundle cannot depend on a future component")
        now = datetime.now(UTC)
        with self._database.transaction() as session:
            # market 级事务锁覆盖首次发布和同日修订，防止并发任务让晚到旧 revision 覆盖指针。
            session.execute(
                select(func.pg_advisory_xact_lock(func.hashtext(f"market-overview:{_MARKET}")))
            )
            stored_components = tuple(
                self._upsert_component(session, candidate=component, published_at=now)
                for component in sorted(components, key=lambda item: item.dataset_code)
            )
            candidates_by_dataset = {candidate.dataset_code: candidate for candidate in components}
            manifest = {
                "market": _MARKET,
                "tradeDate": trade_date.isoformat(),
                "components": [
                    {
                        "datasetCode": row.dataset_code,
                        "dataVersion": str(row.data_version),
                        "contentHash": row.content_hash,
                        "normalizedSha256": candidates_by_dataset[row.dataset_code].source.get(
                            "normalizedSha256"
                        ),
                        "requestEvidence": candidates_by_dataset[row.dataset_code].source.get(
                            "requestEvidence", []
                        ),
                    }
                    for row in stored_components
                ],
            }
            bundle_content = {"manifest": manifest, "overview": overview}
            content_hash = _content_hash(bundle_content)
            existing = session.execute(
                select(MarketOverviewBundle).where(
                    MarketOverviewBundle.trade_date == trade_date,
                    MarketOverviewBundle.content_hash == content_hash,
                )
            ).scalar_one_or_none()
            inserted = existing is None
            data_version = uuid4()
            final_overview = {
                **overview,
                "dataVersion": str(data_version),
                "publishedAt": now.isoformat().replace("+00:00", "Z"),
            }
            bundle = (
                MarketOverviewBundle(
                    bundle_id=uuid4(),
                    trade_date=trade_date,
                    data_version=data_version,
                    content_hash=content_hash,
                    payload_json=final_overview,
                    manifest_json=manifest,
                    quality_status="passed",
                    finality="final",
                    published_at=now,
                )
                if existing is None
                else existing
            )
            if existing is None:
                session.add(bundle)
                session.flush()
                session.add_all(
                    [
                        MarketOverviewBundleComponent(
                            bundle_id=bundle.bundle_id,
                            dataset_code=row.dataset_code,
                            component_release_id=row.component_release_id,
                            verified_at=now,
                            verification_json={
                                "source": candidates_by_dataset[row.dataset_code].source,
                                "normalizedSha256": candidates_by_dataset[
                                    row.dataset_code
                                ].source.get("normalizedSha256"),
                                "requestEvidenceSha256": _content_hash(
                                    candidates_by_dataset[row.dataset_code].source.get(
                                        "requestEvidence", []
                                    )
                                ),
                            },
                        )
                        for row in stored_components
                    ]
                )
            active = session.get(
                MarketOverviewActiveBundle,
                {"market": _MARKET, "trade_date": trade_date},
            )
            current = session.get(MarketOverviewCurrentPointer, _MARKET)
            current_bundle = (
                None if current is None else session.get(MarketOverviewBundle, current.bundle_id)
            )
            # 只允许顺序推进 tip；缺失历史日和已有历史日修订都须先受控重放后继链。
            is_tip_or_forward = current_bundle is None or trade_date >= current_bundle.trade_date
            should_activate = is_tip_or_forward and (inserted or active is None)
            if should_activate and (active is None or active.bundle_id != bundle.bundle_id):
                previous_bundle_id = None if active is None else active.bundle_id
                active_statement = postgresql_insert(MarketOverviewActiveBundle).values(
                    market=_MARKET,
                    trade_date=trade_date,
                    bundle_id=bundle.bundle_id,
                    updated_at=now,
                )
                session.execute(
                    active_statement.on_conflict_do_update(
                        index_elements=[
                            MarketOverviewActiveBundle.market,
                            MarketOverviewActiveBundle.trade_date,
                        ],
                        set_={"bundle_id": bundle.bundle_id, "updated_at": now},
                    )
                )
                session.add(
                    MarketOverviewPointerTransition(
                        transition_id=uuid4(),
                        market=_MARKET,
                        trade_date=trade_date,
                        from_bundle_id=previous_bundle_id,
                        to_bundle_id=bundle.bundle_id,
                        action="publish",
                        reason="complete_bundle_quality_gate_passed",
                        actor_ref="market-overview-sync",
                        changed_at=now,
                    )
                )
                self._activate_derivation_inputs(
                    session,
                    stored_components=stored_components,
                    updated_at=now,
                )
            # 旧日期候选不能把首页或日期指针倒退，避免后继强弱与周期 lineage 失配。
            if should_activate and (
                current_bundle is None or bundle.trade_date >= current_bundle.trade_date
            ):
                statement = postgresql_insert(MarketOverviewCurrentPointer).values(
                    market=_MARKET,
                    bundle_id=bundle.bundle_id,
                    updated_at=now,
                )
                session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[MarketOverviewCurrentPointer.market],
                        set_={"bundle_id": bundle.bundle_id, "updated_at": now},
                    )
                )
        return PublishedMarketBundle(
            data_version=UUID(str(bundle.data_version)),
            trade_date=bundle.trade_date,
            published_at=_utc(bundle.published_at),
            payload=dict(bundle.payload_json),
            inserted=inserted,
        )

    def publish_derivation_inputs(
        self,
        *,
        components: tuple[MarketComponentCandidate, ...],
    ) -> int:
        """保存近期 sector/SW 日线 seed；它们不创建 bundle、active date 或 current。"""
        if not components:
            raise ValueError("market derivation seed requires components")
        allowed = {"sector.quote.eod.dc", "sw.market-data"}
        if any(
            component.dataset_code not in allowed or component.trade_date is None
            for component in components
        ) or len({(item.dataset_code, item.trade_date) for item in components}) != len(components):
            raise ValueError("market derivation seed contains an invalid component")
        now = datetime.now(UTC)
        with self._database.transaction() as session:
            session.execute(
                select(func.pg_advisory_xact_lock(func.hashtext(f"market-overview:{_MARKET}")))
            )
            rows = tuple(
                self._upsert_component(
                    session,
                    candidate=component,
                    published_at=now,
                )
                for component in sorted(
                    components,
                    key=lambda item: (item.trade_date or date.min, item.dataset_code),
                )
            )
            self._activate_derivation_inputs(
                session,
                stored_components=rows,
                updated_at=now,
            )
        return len(rows)

    def get_bundle(self, *, trade_date: date | None) -> StoredMarketBundle | None:
        """返回原子快照中的 bundle；保留旧端口供单资源调用方兼容。"""
        snapshot = self.get_snapshot(trade_date=trade_date)
        return None if snapshot is None else snapshot.bundle

    def get_snapshot(self, *, trade_date: date | None) -> StoredMarketSnapshot | None:
        """在同一 Session 快照内解析 active bundle 及其 manifest，禁止 A/B 混读。"""
        with self._database.session() as session:
            if trade_date is None:
                row = session.execute(
                    select(MarketOverviewBundle, MarketOverviewActiveBundle.updated_at)
                    .join(
                        MarketOverviewCurrentPointer,
                        MarketOverviewCurrentPointer.bundle_id == MarketOverviewBundle.bundle_id,
                    )
                    .join(
                        MarketOverviewActiveBundle,
                        MarketOverviewActiveBundle.bundle_id == MarketOverviewBundle.bundle_id,
                    )
                    .where(MarketOverviewCurrentPointer.market == _MARKET)
                ).one_or_none()
            else:
                row = session.execute(
                    select(MarketOverviewBundle, MarketOverviewActiveBundle.updated_at)
                    .join(
                        MarketOverviewActiveBundle,
                        MarketOverviewActiveBundle.bundle_id == MarketOverviewBundle.bundle_id,
                    )
                    .where(
                        MarketOverviewActiveBundle.market == _MARKET,
                        MarketOverviewActiveBundle.trade_date == trade_date,
                    )
                ).one_or_none()
            if row is None:
                return None
            bundle, active_changed_at = row
            active_action = session.execute(
                select(MarketOverviewPointerTransition.action)
                .where(
                    MarketOverviewPointerTransition.market == _MARKET,
                    MarketOverviewPointerTransition.trade_date == bundle.trade_date,
                    MarketOverviewPointerTransition.to_bundle_id == bundle.bundle_id,
                )
                .order_by(MarketOverviewPointerTransition.changed_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            component_rows = tuple(
                session.execute(
                    select(
                        MarketOverviewComponentRelease,
                        MarketOverviewBundleComponent,
                    )
                    .join(
                        MarketOverviewBundleComponent,
                        MarketOverviewBundleComponent.component_release_id
                        == MarketOverviewComponentRelease.component_release_id,
                    )
                    .where(MarketOverviewBundleComponent.bundle_id == bundle.bundle_id)
                    .order_by(MarketOverviewBundleComponent.dataset_code)
                )
            )
            stored_bundle = StoredMarketBundle(
                data_version=UUID(str(bundle.data_version)),
                trade_date=bundle.trade_date,
                published_at=_utc(bundle.published_at),
                payload=dict(bundle.payload_json),
                active_action=active_action or "publish",
                active_changed_at=_utc(active_changed_at),
            )
            stored_components = tuple(
                _stored_component(component, association)
                for component, association in component_rows
            )
        return StoredMarketSnapshot(
            bundle=stored_bundle,
            components=stored_components,
        )

    def list_components(
        self,
        *,
        dataset_code: str,
        start: date | None,
        end: date | None,
    ) -> tuple[StoredMarketComponent, ...]:
        """只经每个交易日 active bundle manifest 读取公开组件，撤回 revision 不外泄。"""
        if start is not None and end is not None and start > end:
            raise ValueError("start must not be after end")
        with self._database.session() as session:
            statement = (
                select(
                    MarketOverviewComponentRelease,
                    MarketOverviewBundleComponent,
                )
                .join(
                    MarketOverviewBundleComponent,
                    MarketOverviewBundleComponent.component_release_id
                    == MarketOverviewComponentRelease.component_release_id,
                )
                .join(
                    MarketOverviewActiveBundle,
                    MarketOverviewActiveBundle.bundle_id == MarketOverviewBundleComponent.bundle_id,
                )
                .where(
                    MarketOverviewActiveBundle.market == _MARKET,
                    MarketOverviewBundleComponent.dataset_code == dataset_code,
                )
            )
            if start is not None:
                statement = statement.where(MarketOverviewComponentRelease.trade_date >= start)
            if end is not None:
                statement = statement.where(MarketOverviewComponentRelease.trade_date <= end)
            rows = tuple(
                session.execute(
                    statement.order_by(
                        MarketOverviewComponentRelease.trade_date,
                        MarketOverviewComponentRelease.published_at,
                    )
                )
            )
        return tuple(
            _stored_component(row, association)
            for row, association in sorted(
                rows,
                key=lambda item: (
                    item[0].trade_date or date.min,
                    item[0].partition_key,
                ),
            )
        )

    def get_bundle_components(
        self,
        *,
        trade_date: date | None,
    ) -> tuple[StoredMarketComponent, ...]:
        """返回单事务快照中的 manifest 组件；reader 应直接调用 get_snapshot。"""
        snapshot = self.get_snapshot(trade_date=trade_date)
        return () if snapshot is None else snapshot.components

    def list_derivation_inputs(
        self,
        *,
        dataset_code: str,
        start: date,
        end: date,
    ) -> tuple[StoredMarketComponent, ...]:
        """读取内部 bootstrap 指针固定的日线输入，不经公开 active bundle 解析。"""
        if dataset_code not in {"sector.quote.eod.dc", "sw.market-data"} or start > end:
            raise ValueError("market derivation input query is invalid")
        with self._database.session() as session:
            rows = tuple(
                session.execute(
                    select(MarketOverviewComponentRelease)
                    .join(
                        MarketOverviewDerivationInputPointer,
                        MarketOverviewDerivationInputPointer.component_release_id
                        == MarketOverviewComponentRelease.component_release_id,
                    )
                    .where(
                        MarketOverviewDerivationInputPointer.dataset_code == dataset_code,
                        MarketOverviewDerivationInputPointer.trade_date >= start,
                        MarketOverviewDerivationInputPointer.trade_date <= end,
                    )
                    .order_by(MarketOverviewDerivationInputPointer.trade_date)
                ).scalars()
            )
        return tuple(_stored_component(row) for row in rows)

    def move_active_bundle(
        self,
        *,
        trade_date: date,
        target_data_version: UUID,
        action: str,
        reason: str,
        actor_ref: str,
    ) -> MarketBundlePointerResult:
        """在 market 锁内显式回滚或前滚交易日可见指针，并同步维护 latest 指针。"""
        if action not in {"rollback", "forward"}:
            raise ValueError("market bundle pointer action must be rollback or forward")
        if not reason.strip() or not actor_ref.strip():
            raise ValueError("market bundle pointer reason and actor_ref are required")
        now = datetime.now(UTC)
        with self._database.transaction() as session:
            session.execute(
                select(func.pg_advisory_xact_lock(func.hashtext(f"market-overview:{_MARKET}")))
            )
            target = session.execute(
                select(MarketOverviewBundle).where(
                    MarketOverviewBundle.trade_date == trade_date,
                    MarketOverviewBundle.data_version == target_data_version,
                    MarketOverviewBundle.quality_status == "passed",
                    MarketOverviewBundle.finality == "final",
                )
            ).scalar_one_or_none()
            if target is None:
                raise ValueError("target market bundle revision is unavailable")
            active = session.get(
                MarketOverviewActiveBundle,
                {"market": _MARKET, "trade_date": trade_date},
            )
            if active is None:
                raise ValueError("market bundle active pointer is unavailable")
            current_active = session.get(MarketOverviewBundle, active.bundle_id)
            if current_active is None or active.bundle_id == target.bundle_id:
                raise ValueError("target market bundle is already active")
            if action == "rollback" and target.published_at >= current_active.published_at:
                raise ValueError("rollback target must precede the active revision")
            if action == "forward" and target.published_at <= current_active.published_at:
                raise ValueError("forward target must follow the active revision")
            previous_bundle_id = active.bundle_id
            active.bundle_id = target.bundle_id
            active.updated_at = now
            session.add(
                MarketOverviewPointerTransition(
                    transition_id=uuid4(),
                    market=_MARKET,
                    trade_date=trade_date,
                    from_bundle_id=previous_bundle_id,
                    to_bundle_id=target.bundle_id,
                    action=action,
                    reason=reason.strip(),
                    actor_ref=actor_ref.strip(),
                    changed_at=now,
                )
            )
            current = session.get(MarketOverviewCurrentPointer, _MARKET)
            current_bundle = (
                None if current is None else session.get(MarketOverviewBundle, current.bundle_id)
            )
            if current is None or current_bundle is None or current_bundle.trade_date != trade_date:
                raise ValueError("only the current market tip can be rolled back or forwarded")
            current.bundle_id = target.bundle_id
            current.updated_at = now
            target_inputs = tuple(
                session.execute(
                    select(MarketOverviewComponentRelease)
                    .join(
                        MarketOverviewBundleComponent,
                        MarketOverviewBundleComponent.component_release_id
                        == MarketOverviewComponentRelease.component_release_id,
                    )
                    .where(
                        MarketOverviewBundleComponent.bundle_id == target.bundle_id,
                        MarketOverviewBundleComponent.dataset_code.in_(
                            ("sector.quote.eod.dc", "sw.market-data")
                        ),
                    )
                ).scalars()
            )
            self._activate_derivation_inputs(
                session,
                stored_components=target_inputs,
                updated_at=now,
            )
            result_data_version = UUID(str(target.data_version))
            result_trade_date = target.trade_date
        return MarketBundlePointerResult(
            data_version=result_data_version,
            trade_date=result_trade_date,
            action=action,
            changed_at=now,
        )

    @staticmethod
    def _upsert_component(
        session: Any,
        *,
        candidate: MarketComponentCandidate,
        published_at: datetime,
    ) -> MarketOverviewComponentRelease:
        """按 dataset、分区和内容摘要复用组件版本，不制造无意义 revision。"""
        content_hash = _content_hash(candidate.payload)
        existing = session.execute(
            select(MarketOverviewComponentRelease).where(
                MarketOverviewComponentRelease.dataset_code == candidate.dataset_code,
                MarketOverviewComponentRelease.partition_key == candidate.partition_key,
                MarketOverviewComponentRelease.content_hash == content_hash,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = MarketOverviewComponentRelease(
            component_release_id=uuid4(),
            dataset_code=candidate.dataset_code,
            partition_key=candidate.partition_key,
            trade_date=candidate.trade_date,
            data_version=candidate.data_version,
            content_hash=content_hash,
            payload_json=candidate.payload,
            source_json=candidate.source,
            methodology_json=candidate.methodology,
            quality_json=candidate.quality,
            quality_status="passed",
            finality="final",
            observed_at=candidate.observed_at,
            published_at=published_at,
        )
        session.add(row)
        session.flush()
        return row

    @staticmethod
    def _activate_derivation_inputs(
        session: Any,
        *,
        stored_components: tuple[MarketOverviewComponentRelease, ...],
        updated_at: datetime,
    ) -> None:
        """切换允许参与后续写时聚合的日线指针，不影响任何公开 bundle 可见性。"""
        for row in stored_components:
            if (
                row.dataset_code not in {"sector.quote.eod.dc", "sw.market-data"}
                or row.trade_date is None
            ):
                continue
            statement = postgresql_insert(MarketOverviewDerivationInputPointer).values(
                dataset_code=row.dataset_code,
                trade_date=row.trade_date,
                component_release_id=row.component_release_id,
                updated_at=updated_at,
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        MarketOverviewDerivationInputPointer.dataset_code,
                        MarketOverviewDerivationInputPointer.trade_date,
                    ],
                    set_={
                        "component_release_id": row.component_release_id,
                        "updated_at": updated_at,
                    },
                )
            )


def _content_hash(value: object) -> str:
    """计算稳定 JSON SHA-256，使重跑幂等且修订可审计。"""
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _stored_component(
    row: MarketOverviewComponentRelease,
    association: MarketOverviewBundleComponent | None = None,
) -> StoredMarketComponent:
    """将 release 与 bundle 复核证据合成公开 active view，不改写不可变内容。"""
    source = dict(row.source_json)
    published_at = row.published_at
    if association is not None:
        verification = dict(association.verification_json)
        verified_source = verification.get("source")
        if isinstance(verified_source, dict):
            source = dict(verified_source)
        published_at = association.verified_at
    return StoredMarketComponent(
        data_version=UUID(str(row.data_version)),
        dataset_code=row.dataset_code,
        partition_key=row.partition_key,
        trade_date=row.trade_date,
        published_at=_utc(published_at),
        payload=dict(row.payload_json),
        source=source,
        methodology=dict(row.methodology_json),
        quality=dict(row.quality_json),
    )


def _utc(value: datetime) -> datetime:
    """把驱动可能返回的 naive UTC 时间统一标记为 UTC。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = ["SqlAlchemyMarketOverviewRepository"]
