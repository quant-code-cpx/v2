"""PostgreSQL 申万三级 taxonomy、估值、双时间修订与恢复集成测试。"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from service_data_sync.application.ports.sw_sector import SwSourceObservation
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.sw_sector import (
    SwIndustryLevel,
    SwIndustryNode,
    SwIndustrySnapshot,
    SwIndustryValuation,
    SwMethodology,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.sw_sector_repository import (
    SqlAlchemySwSectorRepository,
)

_DATE = date(2026, 7, 28)


@pytest.mark.integration
def test_repository_publishes_closure_valuations_and_closes_removed_snapshot_rows() -> None:
    """完整快照修订应关闭已消失节点，且 replay checkpoint 只指向成功双发布。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    repository = SqlAlchemySwSectorRepository(database)
    try:
        first = repository.publish_snapshot(
            snapshot=_snapshot(include_extra_leaf=True),
            source=_source("1", datetime(2026, 7, 28, 10, tzinfo=UTC)),
        )
        first_nodes = repository.list_nodes(
            snapshot_date=_DATE,
            level=None,
            parent_code=None,
            after_level=None,
            after_code=None,
            limit=10,
        )
        ancestors = repository.list_ancestors(
            data_version=first.taxonomy.data_version,
            snapshot_date=_DATE,
            descendant_code="850111.SI",
        )
        corrected = repository.publish_snapshot(
            snapshot=_snapshot(include_extra_leaf=False),
            source=_source("2", datetime(2026, 7, 28, 10, 5, tzinfo=UTC)),
        )
        corrected_nodes = repository.list_nodes(
            snapshot_date=_DATE,
            level=None,
            parent_code=None,
            after_level=None,
            after_code=None,
            limit=10,
        )
        valuations = repository.list_valuations(
            snapshot_date=_DATE,
            level=None,
            after_code=None,
            limit=10,
        )
        checkpoint = repository.get_checkpoint(snapshot_date=_DATE)
        repeated = repository.publish_snapshot(
            snapshot=_snapshot(include_extra_leaf=False),
            source=_source("3", datetime(2026, 7, 28, 10, 10, tzinfo=UTC)),
        )
        changed_methodology = repository.publish_snapshot(
            snapshot=_snapshot(include_extra_leaf=False, methodology_version=2),
            source=_source("4", datetime(2026, 7, 28, 10, 15, tzinfo=UTC)),
        )
        changed_methodology_valuations = repository.list_valuations(
            snapshot_date=_DATE,
            level=None,
            after_code=None,
            limit=10,
        )
    finally:
        database.close()

    assert len(first_nodes) == 4
    assert [row.node.code for row in ancestors] == ["801010.SI", "801016.SI"]
    assert [row.node.code for row in corrected_nodes] == [
        "801010.SI",
        "801016.SI",
        "850111.SI",
    ]
    assert len(valuations) == 3
    assert corrected.taxonomy.data_version != first.taxonomy.data_version
    assert corrected.taxonomy.data_version != corrected.valuation.data_version
    assert checkpoint is not None
    assert checkpoint.raw_sha256 == "2" * 64
    assert checkpoint.last_data_version == corrected.taxonomy.data_version
    assert repeated.taxonomy.data_version == corrected.taxonomy.data_version
    assert repeated.valuation.data_version == corrected.valuation.data_version
    assert changed_methodology.valuation.data_version != repeated.valuation.data_version
    assert len(changed_methodology_valuations) == 3


def _snapshot(*, include_extra_leaf: bool, methodology_version: int = 1) -> SwIndustrySnapshot:
    """构造有一条可移除三级叶子的完整 taxonomy 与一一估值覆盖。"""
    nodes = [
        SwIndustryNode(
            code="801010.SI",
            name="农林牧渔",
            level=SwIndustryLevel.LEVEL_1,
            parent_code=None,
            component_count=100,
        ),
        SwIndustryNode(
            code="801016.SI",
            name="种植业",
            level=SwIndustryLevel.LEVEL_2,
            parent_code="801010.SI",
            component_count=20,
        ),
        SwIndustryNode(
            code="850111.SI",
            name="种子",
            level=SwIndustryLevel.LEVEL_3,
            parent_code="801016.SI",
            component_count=8,
        ),
    ]
    if include_extra_leaf:
        nodes.append(
            SwIndustryNode(
                code="850112.SI",
                name="粮食种植",
                level=SwIndustryLevel.LEVEL_3,
                parent_code="801016.SI",
                component_count=12,
            )
        )
    return SwIndustrySnapshot(
        snapshot_date=_DATE,
        nodes=tuple(nodes),
        valuations=tuple(
            SwIndustryValuation(
                code=node.code,
                snapshot_date=_DATE,
                static_pe=Decimal("10"),
                ttm_pe=Decimal("11"),
                pb=Decimal("2"),
                dividend_yield_ratio=Decimal("0.01"),
            )
            for node in nodes
        ),
        methodology=SwMethodology(
            code="integration-sw-overview",
            version=methodology_version,
            status="source_reported",
            upstream_source="integration.sw",
            semantic_spec_sha256=f"{methodology_version:x}" * 64,
        ),
    )


def _source(seed: str, observed_at: datetime) -> SwSourceObservation:
    """构造可区分每次观察且包含 raw 与中立 replay 位置的来源证据。"""
    return SwSourceObservation(
        provider_id="integration-sw",
        capability="sector.sw.snapshot.raw",
        source_payload_sha256=seed * 64,
        raw_uri=f"s3://integration-fixture/sw-{seed}-raw.json",
        normalized_payload_sha256=seed * 64,
        normalized_uri=f"s3://integration-fixture/sw-{seed}-normalized.json",
        observed_at=observed_at,
        upstream_source="integration.sw",
        adapter_version="integration-v1",
        schema_fingerprint="f" * 64,
    )
