"""同步一所交易所显式上市生命周期证据的运维 CLI。"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import date, datetime
from zoneinfo import ZoneInfo

from service_data_sync.application.equity.lifecycle_sync import (
    EquityLifecycleSyncResult,
    EquityLifecycleSyncService,
)
from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import Exchange
from service_data_sync.infrastructure.object_storage.raw_payload_store import S3RawPayloadStore
from service_data_sync.infrastructure.persistence.equity_lifecycle_repository import (
    SqlAlchemyEquityLifecycleRepository,
)
from service_data_sync.infrastructure.persistence.equity_master_repository import (
    SqlAlchemyEquityMasterRepository,
)

_CAPABILITY = "equity.lifecycle.explicit"
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def main(argv: Sequence[str] | None = None) -> int:
    """运行获准来源的一所或三所显式生命周期批次，不接受目录缺席输入。"""
    arguments = _parse_args(argv)
    exchanges = tuple(Exchange) if arguments.all_exchanges else (Exchange(arguments.exchange),)
    target_date = (
        date.fromisoformat(arguments.target_date)
        if arguments.target_date is not None
        else datetime.now(_SHANGHAI).date()
    )
    settings = load_settings()
    configure_logging(settings, process_role="equity-lifecycle-cli")
    container = build_container(settings)
    try:
        sources = container.source_registry.for_capability(_CAPABILITY)
        if len(sources) != 1:
            raise SystemExit("exactly one approved equity lifecycle provider must be enabled")
        service = EquityLifecycleSyncService(
            source=sources[0],
            repository=SqlAlchemyEquityLifecycleRepository(container.database),
            raw_payload_store=S3RawPayloadStore(container.object_storage),
        )
        results = asyncio.run(
            _sync_exchanges(service, exchanges=exchanges, target_date=target_date)
        )
        aggregate = (
            SqlAlchemyEquityMasterRepository(container.database).publish_cn_a_aggregate()
            if arguments.all_exchanges
            else None
        )
    finally:
        container.close()
    print(
        json.dumps(
            {
                "items": [
                    {
                        "exchange": result.exchange.value,
                        "snapshot_id": str(result.snapshot_id),
                        "data_version": str(result.data_version),
                        "inserted_count": result.inserted_count,
                        "unchanged_count": result.unchanged_count,
                    }
                    for result in results
                ],
                "aggregate_data_version": None
                if aggregate is None
                else str(aggregate.data_version),
            },
            separators=(",", ":"),
        )
    )
    return 0


async def _sync_exchanges(
    service: EquityLifecycleSyncService,
    *,
    exchanges: tuple[Exchange, ...],
    target_date: date,
) -> tuple[EquityLifecycleSyncResult, ...]:
    """顺序同步三所生命周期来源，避免未定许可下的并发上游访问。"""
    results: list[EquityLifecycleSyncResult] = []
    for exchange in exchanges:
        results.append(await service.sync(exchange=exchange, target_date=target_date))
    return tuple(results)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """定义显式交易所、可复现目标日和三所协调发布参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--exchange", choices=[exchange.value for exchange in Exchange])
    group.add_argument("--all-exchanges", action="store_true", help="顺序同步三所并发布稳定聚合")
    parser.add_argument("--target-date", help="来源语义对应的目标市场日，格式 YYYY-MM-DD")
    return parser.parse_args(argv)
