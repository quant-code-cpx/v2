"""市场完整包 active 指针、历史候选与回滚前滚集成测试。"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete

from service_data_sync.application.ports.market_overview import MarketComponentCandidate
from service_data_sync.bootstrap.settings import load_settings
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
from service_data_sync.infrastructure.persistence.market_overview_repository import (
    SqlAlchemyMarketOverviewRepository,
)

_DATASET = "sector.quote.eod.dc"


@pytest.mark.integration
def test_active_bundle_replay_historical_candidate_and_tip_rollback_are_consistent() -> None:
    """验证旧重放不回退、历史修订不激活、缺日可补和 tip 回滚同步派生指针。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    repository = SqlAlchemyMarketOverviewRepository(database)
    previous_day = date(2098, 6, 29)
    trade_day = date(2098, 6, 30)
    next_day = date(2098, 7, 1)
    try:
        _clean_market_overview(database)
        trade_a_candidate = _candidate(trade_day, "trade-a")
        trade_b_candidate = _candidate(trade_day, "trade-b")
        trade_c_candidate = _candidate(trade_day, "trade-c")
        trade_a = repository.publish_complete_bundle(
            trade_date=trade_day,
            components=(trade_a_candidate,),
            overview=_overview(trade_day, "trade-a"),
        )
        trade_b = repository.publish_complete_bundle(
            trade_date=trade_day,
            components=(trade_b_candidate,),
            overview=_overview(trade_day, "trade-b"),
        )

        replayed_trade_a = repository.publish_complete_bundle(
            trade_date=trade_day,
            components=(trade_a_candidate,),
            overview=_overview(trade_day, "trade-a"),
        )

        assert replayed_trade_a.data_version == trade_a.data_version
        assert repository.get_bundle(trade_date=trade_day).data_version == trade_b.data_version  # type: ignore[union-attr]

        next_a_candidate = _candidate(next_day, "next-a")
        next_b_candidate = _candidate(next_day, "next-b")
        next_a = repository.publish_complete_bundle(
            trade_date=next_day,
            components=(next_a_candidate,),
            overview=_overview(next_day, "next-a"),
        )
        next_b = repository.publish_complete_bundle(
            trade_date=next_day,
            components=(next_b_candidate,),
            overview=_overview(next_day, "next-b"),
        )
        historical_candidate = repository.publish_complete_bundle(
            trade_date=trade_day,
            components=(trade_c_candidate,),
            overview=_overview(trade_day, "trade-c"),
        )

        assert historical_candidate.data_version not in {
            trade_a.data_version,
            trade_b.data_version,
        }
        assert repository.get_bundle(trade_date=trade_day).data_version == trade_b.data_version  # type: ignore[union-attr]
        assert repository.get_bundle(trade_date=None).data_version == next_b.data_version  # type: ignore[union-attr]

        previous = repository.publish_complete_bundle(
            trade_date=previous_day,
            components=(_candidate(previous_day, "previous"),),
            overview=_overview(previous_day, "previous"),
        )

        assert previous.inserted is True
        assert repository.get_bundle(trade_date=previous_day) is None
        assert repository.get_bundle(trade_date=None).data_version == next_b.data_version  # type: ignore[union-attr]
        with pytest.raises(ValueError, match="only the current market tip"):
            repository.move_active_bundle(
                trade_date=trade_day,
                target_data_version=trade_a.data_version,
                action="rollback",
                reason="integration non-tip guard",
                actor_ref="pytest",
            )

        rolled_back = repository.move_active_bundle(
            trade_date=next_day,
            target_data_version=next_a.data_version,
            action="rollback",
            reason="integration rollback",
            actor_ref="pytest",
        )
        rollback_snapshot = repository.get_snapshot(trade_date=None)
        derivation = repository.list_derivation_inputs(
            dataset_code=_DATASET,
            start=next_day,
            end=next_day,
        )

        assert rolled_back.data_version == next_a.data_version
        assert rollback_snapshot is not None
        assert rollback_snapshot.bundle.data_version == next_a.data_version
        assert rollback_snapshot.bundle.active_action == "rollback"
        assert [component.data_version for component in derivation] == [
            next_a_candidate.data_version
        ]
        assert trade_c_candidate.data_version not in {
            component.data_version
            for component in repository.list_components(
                dataset_code=_DATASET,
                start=trade_day,
                end=trade_day,
            )
        }

        forwarded = repository.move_active_bundle(
            trade_date=next_day,
            target_data_version=next_b.data_version,
            action="forward",
            reason="integration forward",
            actor_ref="pytest",
        )

        assert forwarded.data_version == next_b.data_version
        assert repository.get_bundle(trade_date=None).active_action == "forward"  # type: ignore[union-attr]
    finally:
        _clean_market_overview(database)
        database.close()


def _candidate(trade_date: date, revision: str) -> MarketComponentCandidate:
    """构造参与派生指针的确定性单组件候选。"""
    data_version = uuid4()
    observed_at = datetime.combine(trade_date, datetime.min.time(), tzinfo=UTC)
    return MarketComponentCandidate(
        data_version=data_version,
        dataset_code=_DATASET,
        partition_key=f"{trade_date.isoformat()}:{revision}",
        trade_date=trade_date,
        payload={
            "tradeDate": trade_date.isoformat(),
            "revision": revision,
            "records": [],
        },
        source={
            "provider": "integration-provider",
            "upstreamSource": "integration-source",
            "sourceDataset": "integration-dataset",
            "observedAt": observed_at.isoformat(),
            "adapterVersion": "integration-v1",
            "schemaFingerprint": "a" * 64,
        },
        methodology={"id": "integration-method", "version": "1"},
        quality={"status": "passed"},
        observed_at=observed_at,
    )


def _overview(trade_date: date, revision: str) -> dict[str, object]:
    """构造参与 bundle 内容寻址的最小首页载荷。"""
    return {
        "tradeDate": trade_date.isoformat(),
        "finality": "final",
        "revision": revision,
        "quality": {
            "componentCount": 1,
            "passedCount": 1,
            "sourceBindings": [
                {
                    "role": "external",
                    "component": _DATASET,
                    "provider": "integration-provider",
                }
            ],
            "checks": [
                {
                    "code": "integration-component-coverage",
                    "status": "passed",
                    "actual": "1",
                    "expected": "1",
                }
            ],
        },
    }


def _clean_market_overview(database: DatabaseClient) -> None:
    """按外键反序清空测试库中的市场完整包表。"""
    with database.transaction() as session:
        session.execute(delete(MarketOverviewPointerTransition))
        session.execute(delete(MarketOverviewCurrentPointer))
        session.execute(delete(MarketOverviewActiveBundle))
        session.execute(delete(MarketOverviewBundleComponent))
        session.execute(delete(MarketOverviewBundle))
        session.execute(delete(MarketOverviewDerivationInputPointer))
        session.execute(delete(MarketOverviewComponentRelease))
