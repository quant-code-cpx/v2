"""股票全量回填封存引用的 PostgreSQL 恢复与真实 release 血缘集成测试。"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from service_data_sync.application.ports.equity_workspace import (
    EquityWorkspaceSourceObservation,
)
from service_data_sync.application.ports.sector_market_data import StoredSector
from service_data_sync.application.ports.sw_sector import SwSourceObservation
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import EquityIdentifier, Exchange
from service_data_sync.domain.equity_master import (
    EquityCatalogEntry,
    EquityLifecycleEntry,
    EquityLifecycleEvidenceKind,
    EquityLifecycleStatus,
)
from service_data_sync.domain.equity_workspace import (
    EquityTradingStatus,
    SwEquityMembership,
)
from service_data_sync.domain.sector import (
    SectorCatalogEntry,
    SectorIdentifier,
    SectorMembershipCandidate,
    SectorScheme,
)
from service_data_sync.domain.sw_sector import (
    SwIndustryLevel,
    SwIndustryNode,
    SwIndustrySnapshot,
    SwIndustryValuation,
    SwMethodology,
)
from service_data_sync.infrastructure.data_operations import (
    equity_backfill,
    equity_reference_bundle,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical.release import (
    CanonicalRecordLineage,
)
from service_data_sync.infrastructure.database.models.equity.backfill import (
    EquityReferenceGenerationAttempt,
    EquityReferenceGenerationStep,
)
from service_data_sync.infrastructure.database.models.equity.workspace import (
    equity_discovery_snapshot as discovery_snapshot_model,
)
from service_data_sync.infrastructure.database.models.publication import (
    dataset_publication_component as publication_component_model,
)
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.persistence.equity_discovery_repository import (
    SqlAlchemyEquityDiscoveryRepository,
)
from service_data_sync.infrastructure.persistence.equity_lifecycle_repository import (
    SqlAlchemyEquityLifecycleRepository,
)
from service_data_sync.infrastructure.persistence.equity_master_repository import (
    SqlAlchemyEquityMasterRepository,
)
from service_data_sync.infrastructure.persistence.equity_workspace_repository import (
    EquityWorkspaceSourceApproval,
    SqlAlchemyEquityWorkspaceRepository,
)
from service_data_sync.infrastructure.persistence.sector_market_data_repository import (
    SqlAlchemySectorMarketDataRepository,
)
from service_data_sync.infrastructure.persistence.sector_membership_repository import (
    SqlAlchemySectorMembershipRepository,
)
from service_data_sync.infrastructure.persistence.sw_sector_repository import (
    SqlAlchemySwSectorRepository,
)

_AS_OF = date(2026, 7, 30)
_INITIAL_NAME = "贵州茅台（冻结初版）"
_REVISED_NAME = "贵州茅台（当前订正）"
_INITIAL_SZSE_NAME = "平安银行（冻结初版）"
_SAME_DAY_SZSE_CORRECTION = "平安银行（同日知识订正）"
_INITIAL_REASON = "初始停牌原因"
_REVISED_REASON = "当前订正停牌原因"
_STEP_DATASETS = (
    "equity.master.cn-a",
    "equity.lifecycle.explicit",
    "sector.catalog.raw",
    "sector.membership.release",
    "sector.sw.taxonomy",
    "equity.trading_status.1d",
    "sector.sw2021.membership.snapshot",
)


@pytest.mark.integration
def test_building_reference_attempt_persists_sql_null_seal_fields() -> None:
    """构建态 attempt 与 held 步骤必须把未封印 JSON 写成 SQL NULL。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting isolated infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    try:
        orchestrator = equity_reference_bundle.EquityReferenceBundleOrchestrator(
            database=database,
            control_plane=cast(Any, None),
            trading_calendar=cast(Any, None),
            # 固定时钟只验证构建态账本，不触发交易日历或外部来源调用。
            now=lambda: datetime(2026, 7, 31, 12, tzinfo=UTC),
        )
        attempt = orchestrator._ensure_attempt(
            campaign_key=f"reference-null-{uuid4().hex}",
            snapshot_observed_on=_AS_OF,
            market_as_of=_AS_OF,
        )

        with database.session() as session:
            stored = session.get(EquityReferenceGenerationAttempt, attempt.attempt_id)
            steps = list(
                session.scalars(
                    select(EquityReferenceGenerationStep)
                    .where(EquityReferenceGenerationStep.attempt_id == attempt.attempt_id)
                    .order_by(EquityReferenceGenerationStep.ordinal)
                ).all()
            )

        assert stored is not None
        assert stored.status == "BUILDING"
        assert stored.manifest_json is None
        assert stored.source_batch_ids_json is None
        assert stored.last_error_json is None
        assert stored.sealed_at is None
        assert len(steps) == 7
        assert all(step.status == "HELD" for step in steps)
        assert all(step.output_publications_json is None for step in steps)
        assert all(step.source_batch_ids_json is None for step in steps)
    finally:
        database.close()


@pytest.mark.integration
def test_sealed_reference_bundle_recovers_superseded_inputs_without_current_reads() -> None:
    """真实 publisher 的封存 release 在指针推进后仍按旧知识边界恢复，不读取 current。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting isolated infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    catalog = SqlAlchemyEquityMasterRepository(database)
    lifecycle = SqlAlchemyEquityLifecycleRepository(database)
    workspace = SqlAlchemyEquityWorkspaceRepository(
        database,
        approved_sources={"akshare": _workspace_approval()},
    )
    sector_catalog = SqlAlchemySectorMarketDataRepository(database)
    sector_membership = SqlAlchemySectorMembershipRepository(database)
    discovery = SqlAlchemyEquityDiscoveryRepository(database)
    current = {
        Exchange.SSE: EquityIdentifier.parse("SSE.600519"),
        Exchange.SZSE: EquityIdentifier.parse("SZSE.000001"),
        Exchange.BSE: EquityIdentifier.parse("BSE.920001"),
    }
    try:
        _publish_catalogs(catalog, current=current, sse_name=_INITIAL_NAME)
        catalog.publish_cn_a_aggregate()
        _publish_lifecycles(lifecycle, current=current)
        sectors = _publish_sector_catalogs(sector_catalog)
        _publish_sector_memberships(
            sector_membership,
            sectors=sectors,
            identifier=current[Exchange.SSE],
            observation_date=_AS_OF,
            source_suffix="initial",
        )
        _publish_sw_reference(database, workspace=workspace, identifier=current[Exchange.SSE])
        workspace.publish_trading_statuses(
            observation_date=_AS_OF,
            statuses=(
                EquityTradingStatus(
                    identifier=current[Exchange.SSE],
                    trade_date=_AS_OF,
                    status="SUSPENDED",
                    reason=_INITIAL_REASON,
                ),
            ),
            source=_workspace_source("trading-initial"),
        )
        manifest = _capture_and_validate_reference_bundle(database)
        _assert_real_release_lineage(database, manifest=manifest)
        initial_master_child = _master_child_data_version(
            database,
            aggregate_publication_id=UUID(
                str(_component(manifest, "equity.master.cn-a", "CN_A_STABLE")["publicationId"])
            ),
            exchange=Exchange.SSE,
        )
        initial_szse_master_child = _master_child_data_version(
            database,
            aggregate_publication_id=UUID(
                str(_component(manifest, "equity.master.cn-a", "CN_A_STABLE")["publicationId"])
            ),
            exchange=Exchange.SZSE,
        )

        with pytest.raises(equity_reference_bundle.EquityReferenceGenerationError):
            _capture_lifecycle_components(database, run_source_ids=())

        revised_master = catalog.publish_catalog(
            exchange=Exchange.SSE,
            target_date=_AS_OF,
            entries=(
                EquityCatalogEntry(
                    identifier=current[Exchange.SSE],
                    name=_REVISED_NAME,
                    listed_on=date(2001, 8, 27),
                ),
            ),
            provider_id="integration-frozen-reference-master-revised",
            source_payload_sha256=_hash("master-revised"),
            raw_uri="s3://integration-fixture/frozen-reference-master-revised.json",
            observed_at=datetime(2026, 7, 31, 1, tzinfo=UTC),
            upstream_source="integration.fixture.master.revised",
            adapter_version="test-v1",
            schema_fingerprint=_hash("master-schema-revised"),
        )
        same_day_correction = catalog.publish_catalog(
            exchange=Exchange.SZSE,
            target_date=_AS_OF,
            entries=(
                EquityCatalogEntry(
                    identifier=current[Exchange.SZSE],
                    name=_SAME_DAY_SZSE_CORRECTION,
                    listed_on=None,
                ),
            ),
            provider_id="integration-frozen-reference-master-same-day-correction",
            source_payload_sha256=_hash("master-same-day-correction"),
            raw_uri="s3://integration-fixture/frozen-reference-master-same-day-correction.json",
            observed_at=datetime(2026, 7, 31, 2, tzinfo=UTC),
            upstream_source="integration.fixture.master.same-day-correction",
            adapter_version="test-v1",
            schema_fingerprint=_hash("master-schema-same-day-correction"),
        )
        revised_trading = workspace.publish_trading_statuses(
            observation_date=_AS_OF,
            statuses=(
                EquityTradingStatus(
                    identifier=current[Exchange.SSE],
                    trade_date=_AS_OF,
                    status="SUSPENDED",
                    reason=_REVISED_REASON,
                ),
            ),
            source=_workspace_source(
                "trading-revised",
                upstream_source="integration.fixture.revised",
            ),
        )
        revised_memberships = _publish_sector_memberships(
            sector_membership,
            sectors=sectors,
            identifier=current[Exchange.SSE],
            observation_date=_AS_OF + timedelta(days=1),
            source_suffix="revised",
        )
        recovered = discovery.build(as_of=_AS_OF, reference_manifest=manifest)
        row = _discovery_row(database, data_version=recovered.data_version, symbol="600519")
        same_day_row = _discovery_row(
            database,
            data_version=recovered.data_version,
            symbol="000001",
        )
        master_pointer_advanced = revised_master.data_version != initial_master_child
        same_day_correction_advanced = same_day_correction.data_version != initial_szse_master_child
        sealed_trading = _component(
            manifest,
            "equity.trading_status.1d",
            f"date:{_AS_OF.isoformat()}",
        )
        trading_pointer_advanced = revised_trading.data_version != UUID(
            str(sealed_trading["dataVersion"])
        )
        membership_pointers_advanced = all(
            revised_memberships[scheme]
            != UUID(
                str(_component(manifest, "sector.membership.release", scheme.value)["dataVersion"])
            )
            for scheme in SectorScheme
        )
        sealed_inputs_superseded = all(
            _is_superseded(
                database,
                publication_id=UUID(str(component["publicationId"])),
            )
            for component in (
                sealed_trading,
                *(
                    _component(manifest, "sector.membership.release", scheme.value)
                    for scheme in SectorScheme
                ),
            )
        )
    finally:
        database.close()

    assert recovered.completeness == "PARTIAL"
    assert recovered.row_count == 3
    assert row.name == _INITIAL_NAME
    assert row.trading_status_reason == _INITIAL_REASON
    assert same_day_row.name == _INITIAL_SZSE_NAME
    assert master_pointer_advanced
    assert same_day_correction_advanced
    assert trading_pointer_advanced
    assert membership_pointers_advanced
    assert sealed_inputs_superseded


def _publish_catalogs(
    repository: SqlAlchemyEquityMasterRepository,
    *,
    current: dict[Exchange, EquityIdentifier],
    sse_name: str,
) -> None:
    """发布三所完整目录，使真实 CN_A 聚合 release 能冻结 child publication。"""
    names = {
        Exchange.SSE: sse_name,
        Exchange.SZSE: _INITIAL_SZSE_NAME,
        Exchange.BSE: "北交样本（冻结初版）",
    }
    listed_on = {
        Exchange.SSE: date(2001, 8, 27),
        # 目录未给上市日时，同日订正应只切换 knowledge interval 而不切有效区间。
        Exchange.SZSE: None,
        Exchange.BSE: None,
    }
    for exchange in Exchange:
        repository.publish_catalog(
            exchange=exchange,
            target_date=_AS_OF,
            entries=(
                EquityCatalogEntry(
                    identifier=current[exchange],
                    name=names[exchange],
                    listed_on=listed_on[exchange],
                ),
            ),
            provider_id=f"integration-frozen-reference-catalog-{exchange.value.lower()}",
            source_payload_sha256=_hash(f"catalog-{exchange.value}"),
            raw_uri=f"s3://integration-fixture/frozen-reference-{exchange.value.lower()}.json",
            observed_at=datetime(2026, 7, 30, 1, tzinfo=UTC),
            upstream_source="integration.fixture.catalog",
            adapter_version="test-v1",
            schema_fingerprint=_hash(f"catalog-schema-{exchange.value}"),
        )


def _publish_lifecycles(
    repository: SqlAlchemyEquityLifecycleRepository,
    *,
    current: dict[Exchange, EquityIdentifier],
) -> None:
    """发布三所显式生命周期事件；沪深事件保留为历史退市，北交使用官方上市事实。"""
    entries = {
        Exchange.SSE: EquityLifecycleEntry(
            identifier=EquityIdentifier.parse("SSE.600000"),
            name="沪市生命周期历史样本",
            status=EquityLifecycleStatus.DELISTED,
            effective_on=date(2012, 6, 30),
            evidence_kind=EquityLifecycleEvidenceKind.EXPLICIT_DELISTING,
            listed_on=date(2001, 1, 2),
            delisted_on=date(2012, 6, 30),
        ),
        Exchange.SZSE: EquityLifecycleEntry(
            identifier=EquityIdentifier.parse("SZSE.000002"),
            name="深市生命周期历史样本",
            status=EquityLifecycleStatus.DELISTED,
            effective_on=date(2013, 7, 1),
            evidence_kind=EquityLifecycleEvidenceKind.EXPLICIT_DELISTING,
            listed_on=date(2002, 2, 3),
            delisted_on=date(2013, 7, 1),
        ),
        Exchange.BSE: EquityLifecycleEntry(
            identifier=current[Exchange.BSE],
            name="北交样本（冻结初版）",
            status=EquityLifecycleStatus.LISTED,
            effective_on=date(2022, 11, 15),
            evidence_kind=EquityLifecycleEvidenceKind.EXPLICIT_LISTING,
            listed_on=date(2022, 11, 15),
        ),
    }
    for exchange in Exchange:
        repository.publish_lifecycle(
            exchange=exchange,
            target_date=_AS_OF,
            entries=(entries[exchange],),
            provider_id=f"integration-frozen-reference-lifecycle-{exchange.value.lower()}",
            source_payload_sha256=_hash(f"lifecycle-{exchange.value}"),
            raw_uri=f"s3://integration-fixture/frozen-reference-lifecycle-{exchange.value}.json",
            normalized_uri=(
                f"s3://integration-fixture/frozen-reference-lifecycle-{exchange.value}.normalized.json"
            ),
            observed_at=datetime(2026, 7, 30, 2, tzinfo=UTC),
            upstream_source="integration.fixture.lifecycle",
            adapter_version="test-v1",
            schema_fingerprint=_hash(f"lifecycle-schema-{exchange.value}"),
        )


def _publish_sector_catalogs(
    repository: SqlAlchemySectorMarketDataRepository,
) -> dict[SectorScheme, StoredSector]:
    """发布行业和概念目录，并返回随后真实成分 publisher 所需的 ACTIVE 板块。"""
    definitions = {
        SectorScheme.EASTMONEY_INDUSTRY: ("BK0475", "白酒"),
        SectorScheme.EASTMONEY_CONCEPT: ("BK9991", "价值投资"),
    }
    result: dict[SectorScheme, StoredSector] = {}
    for scheme, (code, name) in definitions.items():
        identifier = SectorIdentifier(scheme, code)
        repository.publish_catalog(
            scheme=scheme,
            entries=(SectorCatalogEntry(identifier=identifier, name=name),),
            provider_id=f"integration-frozen-reference-sector-catalog-{scheme.value}",
            source_payload_sha256=_hash(f"sector-catalog-{scheme.value}"),
            raw_uri=f"s3://integration-fixture/frozen-reference-sector-catalog-{scheme.value}.json",
            observed_at=datetime(2026, 7, 30, 3, tzinfo=UTC),
        )
        sector = repository.get_sector_by_identifier(identifier)
        assert sector is not None
        result[scheme] = sector
    return result


def _publish_sector_memberships(
    repository: SqlAlchemySectorMembershipRepository,
    *,
    sectors: dict[SectorScheme, StoredSector],
    identifier: EquityIdentifier,
    observation_date: date,
    source_suffix: str,
) -> dict[SectorScheme, UUID]:
    """以真实 run、snapshot 和 release publisher 发布两个分类体系的完整成分清单。"""
    data_versions: dict[SectorScheme, UUID] = {}
    for scheme in SectorScheme:
        sector = sectors[scheme]
        run = repository.start_run(
            scheme=scheme,
            observation_date=observation_date,
            sectors=(sector,),
        )
        publication = repository.publish_snapshot(
            sector=sector,
            observation_date=observation_date,
            candidates=(SectorMembershipCandidate(identifier.symbol, _INITIAL_NAME),),
            provider_id=f"integration-frozen-reference-membership-{scheme.value}",
            source_payload_sha256=_hash(f"membership-{source_suffix}-{scheme.value}"),
            raw_uri=(
                f"s3://integration-fixture/frozen-reference-membership-"
                f"{source_suffix}-{scheme.value}.json"
            ),
            observed_at=datetime.combine(observation_date, datetime.min.time(), tzinfo=UTC),
            upstream_source="integration.fixture.membership",
            adapter_version="test-v1",
            schema_fingerprint=_hash(f"membership-schema-{scheme.value}"),
            run_id=run.run_id,
            partition_key=f"{scheme.value}:{source_suffix}:{observation_date.isoformat()}",
        )
        repository.mark_partition_completed(
            run=run,
            sector=sector,
            publication=publication,
        )
        repository.finish_run(run=run, status="succeeded")
        released = repository.publish_release(
            scheme=scheme,
            observation_date=observation_date,
        )
        assert released is not None
        data_versions[scheme] = released.data_version
    return data_versions


def _publish_sw_reference(
    database: DatabaseClient,
    *,
    workspace: SqlAlchemyEquityWorkspaceRepository,
    identifier: EquityIdentifier,
) -> None:
    """发布真实三层申万 taxonomy 与三级节点成分，使 bundle 可枚举所有节点分区。"""
    nodes = (
        SwIndustryNode(
            code="801010.SI",
            name="农林牧渔",
            level=SwIndustryLevel.LEVEL_1,
            parent_code=None,
            component_count=1,
        ),
        SwIndustryNode(
            code="801016.SI",
            name="种植业",
            level=SwIndustryLevel.LEVEL_2,
            parent_code="801010.SI",
            component_count=1,
        ),
        SwIndustryNode(
            code="850111.SI",
            name="种子",
            level=SwIndustryLevel.LEVEL_3,
            parent_code="801016.SI",
            component_count=1,
        ),
    )
    snapshot = SwIndustrySnapshot(
        snapshot_date=_AS_OF,
        nodes=nodes,
        valuations=tuple(
            SwIndustryValuation(
                code=node.code,
                snapshot_date=_AS_OF,
                static_pe=Decimal("10"),
                ttm_pe=Decimal("11"),
                pb=Decimal("2"),
                dividend_yield_ratio=Decimal("0.01"),
            )
            for node in nodes
        ),
        methodology=SwMethodology(
            code="integration-frozen-reference-sw2021",
            version=1,
            status="source_reported",
            upstream_source="integration.fixture.sw.taxonomy",
            semantic_spec_sha256=_hash("sw-semantic"),
        ),
    )
    SqlAlchemySwSectorRepository(database).publish_snapshot(
        snapshot=snapshot,
        source=SwSourceObservation(
            provider_id="integration-frozen-reference-sw-taxonomy",
            capability="sector.sw.snapshot.raw",
            source_payload_sha256=_hash("sw-taxonomy-raw"),
            raw_uri="s3://integration-fixture/frozen-reference-sw-taxonomy.json",
            normalized_payload_sha256=_hash("sw-taxonomy-normalized"),
            normalized_uri="s3://integration-fixture/frozen-reference-sw-taxonomy.normalized.json",
            observed_at=datetime(2026, 7, 30, 4, tzinfo=UTC),
            upstream_source="integration.fixture.sw.taxonomy",
            adapter_version="test-v1",
            schema_fingerprint=_hash("sw-taxonomy-schema"),
        ),
    )
    workspace.publish_sw_memberships(
        node_code="850111",
        observation_date=_AS_OF,
        memberships=(
            SwEquityMembership(
                node_code="850111",
                symbol=identifier.symbol,
                name=_INITIAL_NAME,
                observed_on=_AS_OF,
                source_included_on=_AS_OF,
                level1_name="农林牧渔",
                level2_name="种植业",
                level3_name="种子",
            ),
        ),
        source=_workspace_source(
            "sw-membership-initial",
            capability="sector.sw2021.membership.snapshot",
            upstream_source="integration.fixture.sw.membership",
        ),
    )


def _capture_and_validate_reference_bundle(database: DatabaseClient) -> tuple[dict[str, Any], ...]:
    """通过真实 capture 逻辑生成全量组件清单，并验证 bundle 拒绝弱 identity。"""
    with database.session() as session:
        attempt = cast(
            Any,
            SimpleNamespace(snapshot_observed_on=_AS_OF, market_as_of=_AS_OF),
        )
        visible_at = datetime.now(UTC) + timedelta(seconds=1)
        manifest: list[dict[str, Any]] = []
        for dataset_code in _STEP_DATASETS:
            step = cast(Any, SimpleNamespace(dataset_code=dataset_code))
            source_ids = _step_source_ids(session, dataset_code=dataset_code)
            manifest.extend(
                equity_reference_bundle._capture_step_publications(
                    session,
                    attempt=attempt,
                    step=step,
                    visible_at=visible_at,
                    run_source_ids=source_ids,
                )
            )
    ordered = tuple(sorted(manifest, key=lambda item: (item["datasetCode"], item["partitionKey"])))
    bundle = equity_backfill.FrozenReferenceBundle(
        publication_id=uuid4(),
        data_version=uuid4(),
        release_id=uuid4(),
        snapshot_observed_on=_AS_OF,
        market_as_of=_AS_OF,
        manifest=ordered,
        manifest_hash=equity_backfill._hash_json(list(ordered)),
    )
    bundle.validate()
    return ordered


def _capture_lifecycle_components(
    database: DatabaseClient,
    *,
    run_source_ids: tuple[UUID, ...],
) -> tuple[dict[str, Any], ...]:
    """捕获三所真实 PARTIAL lifecycle publication，空 command source 必须 fail-closed。"""
    with database.session() as session:
        return tuple(
            equity_reference_bundle._capture_step_publications(
                session,
                attempt=cast(
                    Any,
                    SimpleNamespace(snapshot_observed_on=_AS_OF, market_as_of=_AS_OF),
                ),
                step=cast(Any, SimpleNamespace(dataset_code="equity.lifecycle.explicit")),
                visible_at=datetime.now(UTC) + timedelta(seconds=1),
                run_source_ids=run_source_ids,
            )
        )


def _step_source_ids(session: Any, *, dataset_code: str) -> tuple[UUID, ...]:
    """汇总一个 command 的真实 release lineage，用于模拟控制面绑定过的 command source 集。"""
    partitions = {
        "equity.master.cn-a": ("CN_A_STABLE",),
        "equity.lifecycle.explicit": ("BSE", "SSE", "SZSE"),
        "sector.catalog.raw": ("eastmoney.concept", "eastmoney.industry"),
        "sector.membership.release": ("eastmoney.concept", "eastmoney.industry"),
        "sector.sw.taxonomy": (f"sw.industry:{_AS_OF.isoformat()}",),
        "equity.trading_status.1d": (f"date:{_AS_OF.isoformat()}",),
        "sector.sw2021.membership.snapshot": ("SW2021:850111",),
    }[dataset_code]
    values: set[UUID] = set()
    for partition_key in partitions:
        publication = _current_publication(
            session,
            dataset=dataset_code,
            partition_key=partition_key,
        )
        values.update(_release_source_ids(session, release_id=UUID(str(publication.release_id))))
    return tuple(sorted(values, key=str))


def _assert_real_release_lineage(
    database: DatabaseClient,
    *,
    manifest: tuple[dict[str, Any], ...],
) -> None:
    """验证每个封存组件具有真实 releaseId，且 manifest source 与 canonical lineage 完全一致。"""
    with database.session() as session:
        for component in manifest:
            publication = session.get(
                DatasetPublication,
                UUID(str(component["publicationId"])),
            )
            assert publication is not None
            assert publication.release_id == UUID(str(component["releaseId"]))
            release_sources = set(
                _release_source_ids(session, release_id=UUID(str(publication.release_id)))
            )
            assert release_sources == {UUID(str(value)) for value in component["sourceBatchIds"]}


def _master_child_data_version(
    database: DatabaseClient,
    *,
    aggregate_publication_id: UUID,
    exchange: Exchange,
) -> UUID:
    """读取 CN_A 聚合清单中冻结的交易所 child dataVersion。"""
    with database.session() as session:
        value = session.scalar(
            select(
                publication_component_model.DatasetPublicationComponent.component_data_version
            ).where(
                publication_component_model.DatasetPublicationComponent.aggregate_publication_id
                == aggregate_publication_id,
                publication_component_model.DatasetPublicationComponent.component_partition_key
                == exchange.value,
            )
        )
    assert value is not None
    return UUID(str(value))


def _discovery_row(
    database: DatabaseClient,
    *,
    data_version: UUID,
    symbol: str,
) -> discovery_snapshot_model.EquityDiscoverySnapshot:
    """通过 discovery publication 的真实 releaseId 读取目标证券冻结投影。"""
    with database.session() as session:
        publication = session.execute(
            select(DatasetPublication).where(DatasetPublication.data_version == data_version)
        ).scalar_one()
        row = session.execute(
            select(discovery_snapshot_model.EquityDiscoverySnapshot).where(
                discovery_snapshot_model.EquityDiscoverySnapshot.release_id
                == publication.release_id,
                discovery_snapshot_model.EquityDiscoverySnapshot.symbol == symbol,
            )
        ).scalar_one()
    return row


def _is_superseded(database: DatabaseClient, *, publication_id: UUID) -> bool:
    """确认封存输入已不再是 current，恢复路径因而只能使用其精确历史 publication。"""
    with database.session() as session:
        publication = session.get(DatasetPublication, publication_id)
    assert publication is not None
    return publication.superseded_at is not None


def _current_publication(
    session: Any,
    *,
    dataset: str,
    partition_key: str,
) -> DatasetPublication:
    """读取一个 dataset partition 的唯一当前 publication，并拒绝 legacy null release。"""
    publication = session.execute(
        select(DatasetPublication).where(
            DatasetPublication.dataset == dataset,
            DatasetPublication.partition_key == partition_key,
            DatasetPublication.superseded_at.is_(None),
        )
    ).scalar_one()
    assert publication.release_id is not None
    return publication


def _release_source_ids(session: Any, *, release_id: UUID) -> tuple[UUID, ...]:
    """读取一份 immutable release 的所有真实 canonical source lineage。"""
    values = {
        UUID(str(value))
        for value in session.scalars(
            select(CanonicalRecordLineage.source_batch_id).where(
                CanonicalRecordLineage.release_id == release_id
            )
        ).all()
    }
    assert values
    return tuple(sorted(values, key=str))


def _component(
    manifest: tuple[dict[str, Any], ...],
    dataset_code: str,
    partition_key: str,
) -> dict[str, Any]:
    """按数据集与分区读取唯一封存组件，测试中重复或缺失均应立即失败。"""
    values = [
        value
        for value in manifest
        if value["datasetCode"] == dataset_code and value["partitionKey"] == partition_key
    ]
    assert len(values) == 1
    return values[0]


def _workspace_approval() -> EquityWorkspaceSourceApproval:
    """构造与生产批准边界同形的内部研究来源授权。"""
    return EquityWorkspaceSourceApproval(
        provider_id="akshare",
        source_code="akshare",
        legal_name="AKShare",
        source_kind="community_aggregator",
        rights_status="personal_internal_research",
        license_scope="internal_research_no_redistribution",
    )


def _workspace_source(
    suffix: str,
    *,
    capability: str = "equity.trading_status.1d",
    upstream_source: str = "integration.fixture.workspace",
) -> EquityWorkspaceSourceObservation:
    """构造具备 raw/normalized 留证的真实 workspace publisher 来源观察。"""
    return EquityWorkspaceSourceObservation(
        provider_id="akshare",
        capability=capability,
        raw_payload_sha256=_hash(f"raw-{suffix}"),
        raw_uri=f"s3://integration-fixture/frozen-reference-{suffix}.json",
        raw_content_type="application/json",
        raw_byte_size=128,
        normalized_payload_sha256=_hash(f"normalized-{suffix}"),
        normalized_uri=f"s3://integration-fixture/frozen-reference-{suffix}.normalized.json",
        normalized_content_type="application/json",
        normalized_byte_size=96,
        observed_at=datetime(2026, 7, 30, 5, tzinfo=UTC),
        upstream_source=upstream_source,
        adapter_version="test-v1",
        schema_fingerprint=_hash(f"workspace-schema-{capability}"),
    )


def _hash(value: str) -> str:
    """生成测试来源实际持久化所需的小写 SHA-256 摘要。"""
    return hashlib.sha256(value.encode()).hexdigest()
