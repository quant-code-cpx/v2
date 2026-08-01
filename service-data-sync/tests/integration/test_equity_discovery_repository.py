"""股票发现冻结横截面的 PostgreSQL 全链路集成测试。"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import or_, select, text

from service_data_sync.application.ports.equity_workspace import (
    EquityWorkspaceSourceObservation,
    PublishedEquityWorkspaceDataset,
)
from service_data_sync.application.ports.financial_sync import (
    FinancialPublicationResult,
    FinancialSourceObservation,
    FinancialValuationInput,
)
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
from service_data_sync.domain.sw_sector import (
    SwIndustryLevel,
    SwIndustryNode,
    SwIndustrySnapshot,
    SwIndustryValuation,
    SwMethodology,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.equity.identity import (
    equity_identifier_version as identifier_version_model,
)
from service_data_sync.infrastructure.persistence.equity_discovery_repository import (
    DiscoveryBuildUnavailable,
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
from service_data_sync.infrastructure.persistence.financial_sync_repository import (
    SqlAlchemyFinancialSyncRepository,
)
from service_data_sync.infrastructure.persistence.sw_sector_repository import (
    SqlAlchemySwSectorRepository,
)

_AS_OF = date(2026, 7, 30)


@pytest.mark.integration
def test_discovery_build_publishes_truthful_partial_and_preserves_last_good() -> None:
    """真实 BASE 全链路应发布 PARTIAL、固化事实血缘并安全处理重放和缺组件。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    catalog = SqlAlchemyEquityMasterRepository(database)
    lifecycle = SqlAlchemyEquityLifecycleRepository(database)
    workspace = SqlAlchemyEquityWorkspaceRepository(
        database,
        approved_sources={"akshare": _workspace_approval()},
    )
    financial = SqlAlchemyFinancialSyncRepository(database)
    discovery = SqlAlchemyEquityDiscoveryRepository(database)
    identifiers = _reserve_fixture_identifiers(database)
    current = {
        Exchange.SSE: identifiers[0],
        Exchange.SZSE: identifiers[1],
        Exchange.BSE: identifiers[2],
    }
    historical = {
        Exchange.SSE: identifiers[3],
        Exchange.SZSE: identifiers[4],
    }
    try:
        _publish_catalogs(catalog, current=current)
        aggregate = catalog.publish_cn_a_aggregate()
        lifecycle_versions = _publish_lifecycles(
            lifecycle,
            current=current,
            historical=historical,
        )
        first_trading = workspace.publish_trading_statuses(
            observation_date=_AS_OF,
            statuses=(
                EquityTradingStatus(
                    identifier=current[Exchange.SSE],
                    trade_date=_AS_OF,
                    status="SUSPENDED",
                    reason="临时停牌",
                ),
            ),
            source=_workspace_source("trading-first"),
        )
        sw_taxonomy_version, first_sw_membership = _publish_sw_fixture(
            database,
            workspace=workspace,
            identifier=current[Exchange.SSE],
        )
        szse_valuation, _ = _publish_valuation_fixture(
            financial,
            current=current,
        )

        first = discovery.build(as_of=_AS_OF)
        replay = discovery.build(as_of=_AS_OF)
        first_state = _read_discovery_state(
            database,
            data_version=first.data_version,
        )
        revised_trading = workspace.publish_trading_statuses(
            observation_date=_AS_OF,
            statuses=(
                EquityTradingStatus(
                    identifier=current[Exchange.SSE],
                    trade_date=_AS_OF,
                    status="SUSPENDED",
                    reason="连续停牌",
                ),
            ),
            source=_workspace_source(
                "trading-revised",
                upstream_source="replacement-trading-feed",
            ),
        )
        revised_sw_membership = _publish_sw_membership(
            workspace,
            identifier=current[Exchange.SSE],
            suffix="sw-membership-revised",
            upstream_source="replacement-sw-feed",
            source_included_on=_AS_OF - timedelta(days=1),
        )
        revised = discovery.build(as_of=_AS_OF)
        revised_state = _read_discovery_state(
            database,
            data_version=revised.data_version,
        )

        with pytest.raises(DiscoveryBuildUnavailable) as raised:
            discovery.build(as_of=_AS_OF + timedelta(days=1))
        current_discovery = _current_discovery_version(database)
    finally:
        database.close()

    assert first.completeness == "PARTIAL"
    assert first.row_count == 5
    assert replay.data_version == first.data_version
    assert first_state["publication"]["quality_status"] == "warned"
    assert first_state["release_quality"] == "warned"
    assert first_state["snapshot_count"] == 5
    assert first_state["manifest"] == {
        f"equity.lifecycle.explicit|{exchange.value}": lifecycle_versions[exchange]
        for exchange in Exchange
    } | {
        f"equity.master.catalog|{exchange.value}": first_state["publication_versions"][
            ("equity.master.catalog", exchange.value)
        ]
        for exchange in Exchange
    } | {
        "equity.master.cn-a|CN_A_STABLE": aggregate.data_version,
        f"equity.trading_status.1d|date:{_AS_OF.isoformat()}": first_trading.data_version,
        f"sector.sw.taxonomy|sw.industry:{_AS_OF.isoformat()}": sw_taxonomy_version,
        "sector.sw2021.membership.snapshot|SW2021:850111": first_sw_membership.data_version,
        "sector.sw2021.membership.aggregate|SW2021": first_state["publication_versions"][
            ("sector.sw2021.membership.aggregate", "SW2021")
        ],
        (
            f"financial.valuation|security:{_security_id(first_state, current[Exchange.SZSE])}"
        ): szse_valuation.data_version,
    }
    assert not any(
        key.startswith(("equity.bar.1d.raw|", "equity.share_capital.reported|"))
        for key in first_state["manifest"]
    )
    _assert_snapshot_semantics(
        first_state,
        current=current,
        historical=historical,
    )
    _assert_fact_availability_lineage(first_state)
    _assert_sw_hierarchy_and_versions(
        first_state,
        identifier=current[Exchange.SSE],
        membership_version=first_sw_membership.data_version,
    )
    _assert_valuation_common_date(
        first_state,
        complete_identifier=current[Exchange.SZSE],
        incomplete_identifier=current[Exchange.BSE],
        valuation_version=szse_valuation.data_version,
    )
    assert {
        str(row["source_label"])
        for row in first_state["availability"]
        if row["family"] == "trading_status"
    } == {"Eastmoney"}
    assert {
        str(row["source_label"])
        for row in revised_state["availability"]
        if row["family"] == "trading_status"
    } == {"replacement-trading-feed"}
    revised_sw_rows = [row for row in revised_state["availability"] if row["family"] == "sw"]
    assert {str(row["source_label"]) for row in revised_sw_rows} == {"replacement-sw-feed"}
    assert any(
        UUID(str(row["component_data_version"])) == revised_sw_membership.data_version
        for row in revised_sw_rows
    )
    assert revised_trading.data_version != first_trading.data_version
    assert revised.data_version != first.data_version
    assert raised.value.reason_code == "TRADING_STATUS_PUBLICATION_UNAVAILABLE"
    assert current_discovery == revised.data_version


def _reserve_fixture_identifiers(database: DatabaseClient) -> tuple[EquityIdentifier, ...]:
    """在共享集成库中有界挑选从未出现过的五个代码，避免随机四位尾码复用历史身份。"""
    for _ in range(128):
        suffix = f"{uuid4().int % 10_000:04d}"
        candidates = (
            EquityIdentifier.parse(f"SSE.68{suffix}"),
            EquityIdentifier.parse(f"SZSE.30{suffix}"),
            EquityIdentifier.parse(f"BSE.92{suffix}"),
            EquityIdentifier.parse(f"SSE.60{suffix}"),
            EquityIdentifier.parse(f"SZSE.00{suffix}"),
        )
        with database.session() as session:
            existing = session.scalar(
                select(identifier_version_model.EquityIdentifierVersion.version_id)
                .where(
                    or_(
                        *(
                            (
                                identifier_version_model.EquityIdentifierVersion.exchange
                                == identifier.exchange.value
                            )
                            & (
                                identifier_version_model.EquityIdentifierVersion.symbol
                                == identifier.symbol
                            )
                            for identifier in candidates
                        )
                    )
                )
                .limit(1)
            )
        if existing is None:
            return candidates
    raise RuntimeError("unable to reserve collision-free discovery integration identifiers")


def _publish_catalogs(
    repository: SqlAlchemyEquityMasterRepository,
    *,
    current: dict[Exchange, EquityIdentifier],
) -> None:
    """发布同一观察日三所完整目录，使 CN_A 聚合可原子冻结。"""
    names = {
        Exchange.SSE: "沪市发现集成样本",
        Exchange.SZSE: "深市发现集成样本",
        Exchange.BSE: "北市发现集成样本",
    }
    for exchange in Exchange:
        repository.publish_catalog(
            exchange=exchange,
            target_date=_AS_OF,
            entries=(
                EquityCatalogEntry(
                    identifier=current[exchange],
                    name=names[exchange],
                    listed_on=None if exchange is Exchange.BSE else date(2020, 1, 2),
                ),
            ),
            provider_id=f"integration-discovery-catalog-{exchange.value.lower()}",
            source_payload_sha256=_hash_character(exchange.value),
            raw_uri=f"s3://integration-fixture/discovery-{exchange.value.lower()}-catalog.json",
            observed_at=datetime(2026, 7, 30, 1, tzinfo=UTC),
            upstream_source="integration-fixture",
            adapter_version="test-v1",
            schema_fingerprint=_hash_character(f"schema-{exchange.value}"),
        )


def _publish_lifecycles(
    repository: SqlAlchemyEquityLifecycleRepository,
    *,
    current: dict[Exchange, EquityIdentifier],
    historical: dict[Exchange, EquityIdentifier],
) -> dict[Exchange, UUID]:
    """发布两只历史退市证券和北交所官方上市日，覆盖三所生命周期 BASE。"""
    entries = {
        Exchange.SSE: EquityLifecycleEntry(
            identifier=historical[Exchange.SSE],
            name="沪市历史退市样本",
            status=EquityLifecycleStatus.DELISTED,
            effective_on=date(2012, 6, 30),
            evidence_kind=EquityLifecycleEvidenceKind.EXPLICIT_DELISTING,
            listed_on=date(2001, 1, 2),
            delisted_on=date(2012, 6, 30),
        ),
        Exchange.SZSE: EquityLifecycleEntry(
            identifier=historical[Exchange.SZSE],
            name="深市历史退市样本",
            status=EquityLifecycleStatus.DELISTED,
            effective_on=date(2013, 7, 1),
            evidence_kind=EquityLifecycleEvidenceKind.EXPLICIT_DELISTING,
            listed_on=date(2002, 2, 3),
            delisted_on=date(2013, 7, 1),
        ),
        Exchange.BSE: EquityLifecycleEntry(
            identifier=current[Exchange.BSE],
            name="北市发现集成样本",
            status=EquityLifecycleStatus.LISTED,
            effective_on=date(2022, 11, 15),
            evidence_kind=EquityLifecycleEvidenceKind.EXPLICIT_LISTING,
            listed_on=date(2022, 11, 15),
        ),
    }
    versions: dict[Exchange, UUID] = {}
    for exchange in Exchange:
        published = repository.publish_lifecycle(
            exchange=exchange,
            target_date=_AS_OF,
            entries=(entries[exchange],),
            provider_id=f"integration-discovery-lifecycle-{exchange.value.lower()}",
            source_payload_sha256=_hash_character(f"lifecycle-{exchange.value}"),
            raw_uri=f"s3://integration-fixture/discovery-{exchange.value.lower()}-lifecycle.json",
            normalized_uri=(
                f"s3://integration-fixture/"
                f"discovery-{exchange.value.lower()}-lifecycle.normalized.json"
            ),
            observed_at=datetime(2026, 7, 30, 2, tzinfo=UTC),
            upstream_source="integration-fixture",
            adapter_version="test-v2",
            schema_fingerprint=_hash_character(f"lifecycle-schema-{exchange.value}"),
        )
        versions[exchange] = published.data_version
    return versions


def _publish_sw_fixture(
    database: DatabaseClient,
    *,
    workspace: SqlAlchemyEquityWorkspaceRepository,
    identifier: EquityIdentifier,
) -> tuple[UUID, PublishedEquityWorkspaceDataset]:
    """发布一个真实三层 taxonomy 与节点成分，使发现构建可验证完整父链。"""
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
            code="integration-discovery-sw2021",
            version=1,
            status="source_reported",
            upstream_source="integration.sw.taxonomy",
            semantic_spec_sha256=_hash_character("sw-semantic"),
        ),
    )
    taxonomy = SqlAlchemySwSectorRepository(database).publish_snapshot(
        snapshot=snapshot,
        source=SwSourceObservation(
            provider_id="integration-sw-taxonomy",
            capability="sector.sw.snapshot.raw",
            source_payload_sha256=_hash_character("sw-taxonomy-raw"),
            raw_uri="s3://integration-fixture/sw-taxonomy.json",
            normalized_payload_sha256=_hash_character("sw-taxonomy-normalized"),
            normalized_uri="s3://integration-fixture/sw-taxonomy.normalized.json",
            observed_at=datetime(2026, 7, 30, 3, 10, tzinfo=UTC),
            upstream_source="integration.sw.taxonomy",
            adapter_version="test-v1",
            schema_fingerprint=_hash_character("sw-taxonomy-schema"),
        ),
    )
    membership = _publish_sw_membership(
        workspace,
        identifier=identifier,
        suffix="sw-membership-first",
        upstream_source="integration.sw.membership",
    )
    return taxonomy.taxonomy.data_version, membership


def _publish_valuation_fixture(
    repository: SqlAlchemyFinancialSyncRepository,
    *,
    current: dict[Exchange, EquityIdentifier],
) -> tuple[FinancialPublicationResult, FinancialPublicationResult]:
    """模拟估值异步到达：完整旧日、仅 PE 新日和从未完整的证券。"""
    complete_date = _AS_OF - timedelta(days=2)
    repository.publish_valuations(
        exchange=Exchange.SZSE,
        symbol=current[Exchange.SZSE].symbol,
        valuations=(
            _valuation("pe_ttm", "市盈率 TTM", complete_date, "18.2"),
            _valuation("pb", "市净率", complete_date, "1.8"),
            _valuation("ps_ttm", "市销率 TTM", complete_date, "2.4"),
        ),
        source=_financial_source("complete-date"),
    )
    current_complete = repository.publish_valuations(
        exchange=Exchange.SZSE,
        symbol=current[Exchange.SZSE].symbol,
        valuations=(
            _valuation(
                "pe_ttm",
                "市盈率 TTM",
                _AS_OF - timedelta(days=1),
                "99.9",
            ),
        ),
        source=_financial_source("newer-pe-only"),
    )
    incomplete = repository.publish_valuations(
        exchange=Exchange.BSE,
        symbol=current[Exchange.BSE].symbol,
        valuations=(_valuation("pe_ttm", "市盈率 TTM", _AS_OF, "33.3"),),
        source=_financial_source("never-complete"),
    )
    return current_complete, incomplete


def _valuation(
    code: str,
    label: str,
    observation_date: date,
    value: str,
) -> FinancialValuationInput:
    """构造一条供应商日频估值事实，并保留不可用币种的明确原因。"""
    return FinancialValuationInput(
        code=code,
        label=label,
        observation_date=observation_date,
        value=Decimal(value),
        value_domain="ratio",
        unit="ratio",
        currency=None,
        currency_null_reason="NOT_APPLICABLE",
    )


def _financial_source(suffix: str) -> FinancialSourceObservation:
    """构造一批真实持久化的财务来源观察，供异步到达场景使用。"""
    return FinancialSourceObservation(
        provider_id="integration-financial",
        capability="financial.valuation.raw",
        source_payload_sha256=_hash_character(f"financial-{suffix}"),
        raw_uri=f"s3://integration-fixture/financial-{suffix}.json",
        observed_at=datetime(2026, 7, 30, 2, tzinfo=UTC),
        upstream_source="integration.financial.valuation",
        adapter_version="test-v1",
        schema_fingerprint=_hash_character("financial-valuation-schema"),
    )


def _publish_sw_membership(
    workspace: SqlAlchemyEquityWorkspaceRepository,
    *,
    identifier: EquityIdentifier,
    suffix: str,
    upstream_source: str,
    source_included_on: date | None = None,
) -> PublishedEquityWorkspaceDataset:
    """发布一条节点完整成分，并允许测试替换真实来源标签。"""
    return workspace.publish_sw_memberships(
        node_code="850111",
        observation_date=_AS_OF,
        memberships=(
            SwEquityMembership(
                node_code="850111",
                symbol=identifier.symbol,
                name="沪市发现集成样本",
                observed_on=_AS_OF,
                source_included_on=source_included_on,
                level1_name="农林牧渔",
                level2_name="种植业",
                level3_name="种子",
            ),
        ),
        source=_workspace_source(
            suffix,
            upstream_source=upstream_source,
            capability="sector.sw2021.membership.snapshot",
        ),
    )


def _workspace_approval() -> EquityWorkspaceSourceApproval:
    """构造与生产批准边界同形的 AKShare 内部研究授权。"""
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
    upstream_source: str = "Eastmoney",
    capability: str = "equity.trading_status.1d",
) -> EquityWorkspaceSourceObservation:
    """构造完整可复验的普通停牌来源观察。"""
    return EquityWorkspaceSourceObservation(
        provider_id="akshare",
        capability=capability,
        raw_payload_sha256=_hash_character(f"raw-{suffix}"),
        raw_uri=f"s3://integration-fixture/{suffix}.json",
        raw_content_type="application/json",
        raw_byte_size=128,
        normalized_payload_sha256=_hash_character(f"normalized-{suffix}"),
        normalized_uri=f"s3://integration-fixture/{suffix}.normalized.json",
        normalized_content_type="application/json",
        normalized_byte_size=96,
        observed_at=datetime(2026, 7, 30, 3, tzinfo=UTC),
        upstream_source=upstream_source,
        adapter_version="test-v1",
        schema_fingerprint=_hash_character("trading-status-schema"),
    )


def _read_discovery_state(
    database: DatabaseClient,
    *,
    data_version: UUID,
) -> dict[str, Any]:
    """读取指定不可变发现版本的 publication、主行、可用性和组件清单。"""
    with database.engine.connect() as connection:
        publication = (
            connection.execute(
                text(
                    """
                    SELECT publication_id, release_id, data_version, quality_status
                    FROM dataset_publication
                    WHERE dataset = 'equity.discovery.eod'
                      AND partition_key = 'CN_A'
                      AND data_version = :data_version
                    """
                ),
                {"data_version": data_version},
            )
            .mappings()
            .one()
        )
        release_quality = connection.execute(
            text(
                """
                SELECT quality_status
                FROM dataset_release
                WHERE release_id = :release_id
                """
            ),
            {"release_id": publication["release_id"]},
        ).scalar_one()
        snapshots = (
            connection.execute(
                text(
                    """
                    SELECT security_id, exchange, symbol, name, lifecycle_status,
                           trading_status, trading_status_reason, listed_on, delisted_on,
                           valuation_date, pe_ttm, pb, ps_ttm,
                           valuation_source_label
                    FROM equity_discovery_snapshot
                    WHERE release_id = :release_id
                    ORDER BY exchange, symbol, security_id
                    """
                ),
                {"release_id": publication["release_id"]},
            )
            .mappings()
            .all()
        )
        availability = (
            connection.execute(
                text(
                    """
                    SELECT security_id, family, availability, null_reason,
                           component_data_version, source_label
                    FROM equity_discovery_availability
                    WHERE release_id = :release_id
                    ORDER BY security_id, family
                    """
                ),
                {"release_id": publication["release_id"]},
            )
            .mappings()
            .all()
        )
        memberships = (
            connection.execute(
                text(
                    """
                    SELECT security_id, scheme, code, name, level, observed_on
                    FROM equity_discovery_membership
                    WHERE release_id = :release_id
                    ORDER BY security_id, scheme, level, code
                    """
                ),
                {"release_id": publication["release_id"]},
            )
            .mappings()
            .all()
        )
        manifest_rows = (
            connection.execute(
                text(
                    """
                    SELECT component_partition_key, component_data_version
                    FROM dataset_publication_component
                    WHERE aggregate_publication_id = :publication_id
                    ORDER BY component_partition_key
                    """
                ),
                {"publication_id": publication["publication_id"]},
            )
            .mappings()
            .all()
        )
        publication_versions = {
            (str(row["dataset"]), str(row["partition_key"])): UUID(str(row["data_version"]))
            for row in connection.execute(
                text(
                    """
                    SELECT dataset, partition_key, data_version
                    FROM dataset_publication
                    WHERE superseded_at IS NULL
                      AND dataset IN (
                        'equity.master.catalog',
                        'equity.lifecycle.explicit',
                        'equity.trading_status.1d',
                        'sector.sw.taxonomy',
                        'sector.sw2021.membership.snapshot',
                        'sector.sw2021.membership.aggregate'
                      )
                    """
                )
            )
            .mappings()
            .all()
        }
        fact_sources = (
            connection.execute(
                text(
                    """
                    WITH selected AS (
                      SELECT DISTINCT ON (version.security_id, version.family)
                             version.security_id, version.exchange, version.family,
                             source.capability, source.upstream_source
                      FROM (
                        SELECT security_id, exchange, 'identity' AS family,
                               effective_from, source_batch_id
                        FROM equity_identifier_version
                        WHERE known_to IS NULL AND identity_state = 'CONFIRMED'
                        UNION ALL
                        SELECT name.security_id, instrument.exchange, 'name' AS family,
                               name.effective_from, name.source_batch_id
                        FROM equity_name_version name
                        JOIN equity_instrument instrument
                          ON instrument.security_id = name.security_id
                        WHERE name.known_to IS NULL
                        UNION ALL
                        SELECT lifecycle.security_id, instrument.exchange,
                               'lifecycle' AS family, lifecycle.effective_from,
                               lifecycle.source_batch_id
                        FROM equity_listing_status_version lifecycle
                        JOIN equity_instrument instrument
                          ON instrument.security_id = lifecycle.security_id
                        WHERE lifecycle.known_to IS NULL
                          AND lifecycle.effective_range @> :as_of
                      ) version
                      JOIN source_batch source
                        ON source.source_batch_id = version.source_batch_id
                      WHERE version.security_id IN (
                        SELECT security_id
                        FROM equity_discovery_snapshot
                        WHERE release_id = :release_id
                      )
                      ORDER BY version.security_id, version.family,
                               version.effective_from DESC
                    )
                    SELECT security_id, exchange, family, capability,
                           upstream_source
                    FROM selected
                    ORDER BY security_id, family
                    """
                ),
                {
                    "as_of": _AS_OF,
                    "release_id": publication["release_id"],
                },
            )
            .mappings()
            .all()
        )
    return {
        "publication": publication,
        "release_quality": str(release_quality),
        "snapshots": snapshots,
        "snapshot_count": len(snapshots),
        "availability": availability,
        "memberships": memberships,
        "manifest": {
            str(row["component_partition_key"]): UUID(str(row["component_data_version"]))
            for row in manifest_rows
        },
        "publication_versions": publication_versions,
        "fact_sources": fact_sources,
    }


def _assert_snapshot_semantics(
    state: dict[str, Any],
    *,
    current: dict[Exchange, EquityIdentifier],
    historical: dict[Exchange, EquityIdentifier],
) -> None:
    """验证生命周期、普通交易状态和缺行情列保持真实空值语义。"""
    snapshots = {(str(row["exchange"]), str(row["symbol"])): row for row in state["snapshots"]}
    assert snapshots[("SSE", current[Exchange.SSE].symbol)]["trading_status"] == ("TRADE_SUSPENDED")
    assert snapshots[("SZSE", current[Exchange.SZSE].symbol)]["trading_status"] == ("UNKNOWN")
    assert snapshots[("BSE", current[Exchange.BSE].symbol)]["lifecycle_status"] == ("LISTED")
    for exchange, identifier in historical.items():
        row = snapshots[(exchange.value, identifier.symbol)]
        assert row["lifecycle_status"] == "DELISTED"
        assert row["trading_status"] == "NOT_APPLICABLE"
        assert row["delisted_on"] is not None


def _assert_fact_availability_lineage(state: dict[str, Any]) -> None:
    """验证身份、名称、生命周期逐行 dataVersion 与其真实来源 capability 一致。"""
    availability = {
        (int(row["security_id"]), str(row["family"])): row for row in state["availability"]
    }
    manifest = state["manifest"]
    assert isinstance(manifest, dict)
    for source in state["fact_sources"]:
        capability = str(source["capability"])
        exchange = str(source["exchange"])
        expected_key = (
            f"equity.master.catalog|{exchange}"
            if capability == "equity.master.catalog"
            else f"equity.lifecycle.explicit|{exchange}"
        )
        row = availability[(int(source["security_id"]), str(source["family"]))]
        assert row["availability"] == "DATA"
        assert UUID(str(row["component_data_version"])) == manifest[expected_key]
        assert row["source_label"] == source["upstream_source"]
    for (security_id, family), row in availability.items():
        if family in {"market", "capitalization"}:
            assert row["availability"] == "SOURCE_UNAVAILABLE"
            assert row["component_data_version"] is None
        if family == "trading_status":
            assert row["component_data_version"] is not None
        del security_id


def _assert_sw_hierarchy_and_versions(
    state: dict[str, Any],
    *,
    identifier: EquityIdentifier,
    membership_version: UUID,
) -> None:
    """验证 L1/L2/L3 来自父链，成员和合法空集分别绑定节点与 aggregate 版本。"""
    security_id = next(
        int(row["security_id"])
        for row in state["snapshots"]
        if row["exchange"] == identifier.exchange.value and row["symbol"] == identifier.symbol
    )
    path = [
        (str(row["level"]), str(row["code"]), str(row["name"]))
        for row in state["memberships"]
        if int(row["security_id"]) == security_id and row["scheme"] == "sw.industry"
    ]
    assert path == [
        ("1", "801010", "农林牧渔"),
        ("2", "801016", "种植业"),
        ("3", "850111", "种子"),
    ]
    sw_rows = {
        int(row["security_id"]): row for row in state["availability"] if row["family"] == "sw"
    }
    assert sw_rows[security_id]["availability"] == "DATA"
    assert UUID(str(sw_rows[security_id]["component_data_version"])) == (membership_version)
    assert sw_rows[security_id]["source_label"] == "integration.sw.membership"
    aggregate_version = state["manifest"]["sector.sw2021.membership.aggregate|SW2021"]
    empty_rows = [row for key, row in sw_rows.items() if key != security_id]
    assert empty_rows
    assert all(row["availability"] == "LEGITIMATE_EMPTY" for row in empty_rows)
    assert all(UUID(str(row["component_data_version"])) == aggregate_version for row in empty_rows)


def _assert_valuation_common_date(
    state: dict[str, Any],
    *,
    complete_identifier: EquityIdentifier,
    incomplete_identifier: EquityIdentifier,
    valuation_version: UUID,
) -> None:
    """验证三项估值只能取同一完整日期，且不完整日期不会拼成 DATA。"""
    complete_security_id = _security_id(state, complete_identifier)
    incomplete_security_id = _security_id(state, incomplete_identifier)
    snapshots = {int(row["security_id"]): row for row in state["snapshots"]}
    complete = snapshots[complete_security_id]
    assert complete["valuation_date"] == _AS_OF - timedelta(days=2)
    assert Decimal(str(complete["pe_ttm"])) == Decimal("18.2")
    assert Decimal(str(complete["pb"])) == Decimal("1.8")
    assert Decimal(str(complete["ps_ttm"])) == Decimal("2.4")
    assert complete["valuation_source_label"] == "integration.financial.valuation"
    incomplete = snapshots[incomplete_security_id]
    assert incomplete["valuation_date"] is None
    assert incomplete["pe_ttm"] is None
    assert incomplete["pb"] is None
    assert incomplete["ps_ttm"] is None
    valuation_states = {
        int(row["security_id"]): row
        for row in state["availability"]
        if row["family"] == "valuation"
    }
    assert valuation_states[complete_security_id]["availability"] == "DATA"
    assert (
        UUID(str(valuation_states[complete_security_id]["component_data_version"]))
        == valuation_version
    )
    assert (
        valuation_states[complete_security_id]["source_label"] == "integration.financial.valuation"
    )
    assert valuation_states[incomplete_security_id]["availability"] == "SOURCE_UNAVAILABLE"
    assert (
        valuation_states[incomplete_security_id]["null_reason"]
        == "VALUATION_COMMON_DATE_INCOMPLETE"
    )


def _security_id(state: dict[str, Any], identifier: EquityIdentifier) -> int:
    """按交易所和代码读取冻结发现横截面的永久证券键。"""
    return next(
        int(row["security_id"])
        for row in state["snapshots"]
        if row["exchange"] == identifier.exchange.value and row["symbol"] == identifier.symbol
    )


def _current_discovery_version(database: DatabaseClient) -> UUID:
    """读取失败构建后仍可见的发现 publication 版本。"""
    with database.engine.connect() as connection:
        value = connection.execute(
            text(
                """
                SELECT data_version
                FROM dataset_publication
                WHERE dataset = 'equity.discovery.eod'
                  AND partition_key = 'CN_A'
                  AND superseded_at IS NULL
                """
            )
        ).scalar_one()
    return UUID(str(value))


def _hash_character(value: str) -> str:
    """把任意短测试标签稳定扩展为合法六十四位十六进制摘要。"""
    return (value.encode().hex() * 64)[:64].ljust(64, "0")
