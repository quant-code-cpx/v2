"""同步一所交易所当前完整 A 股证券目录的运维 CLI。"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import date, datetime
from zoneinfo import ZoneInfo

from service_data_sync.application.equity.master_catalog_sync import (
    EquityCatalogSyncResult,
    EquityCatalogSyncService,
)
from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import Exchange
from service_data_sync.infrastructure.object_storage.raw_payload_store import S3RawPayloadStore
from service_data_sync.infrastructure.persistence.equity_master_repository import (
    SqlAlchemyEquityMasterRepository,
)

_CAPABILITY = "equity.master.catalog"
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def main(argv: Sequence[str] | None = None) -> int:
    """同步一所交易所当前完整目录；三所协调发布将在后续 worker 任务接管。"""
    arguments = _parse_args(argv)
    exchanges = tuple(Exchange) if arguments.all_exchanges else (Exchange(arguments.exchange),)
    target_date = (
        date.fromisoformat(arguments.target_date)
        if arguments.target_date is not None
        else datetime.now(_SHANGHAI).date()
    )
    settings = load_settings()
    configure_logging(settings, process_role="equity-catalog-cli")
    container = build_container(settings)
    try:
        sources = container.source_registry.for_capability(_CAPABILITY)
        if len(sources) != 1:
            raise SystemExit("exactly one approved equity catalog provider must be enabled")
        repository = SqlAlchemyEquityMasterRepository(container.database)
        service = EquityCatalogSyncService(
            source=sources[0],
            repository=repository,
            raw_payload_store=S3RawPayloadStore(container.object_storage),
        )
        results = asyncio.run(
            _sync_exchanges(service, exchanges=exchanges, target_date=target_date)
        )
        aggregate = repository.publish_cn_a_aggregate() if arguments.all_exchanges else None
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
    service: EquityCatalogSyncService, *, exchanges: tuple[Exchange, ...], target_date: date
) -> tuple[EquityCatalogSyncResult, ...]:
    """顺序同步三所目录，确保每个 AKShare 现货请求独立保留证据且避免上游并发压力。"""
    results: list[EquityCatalogSyncResult] = []
    for exchange in exchanges:
        results.append(await service.sync(exchange=exchange, target_date=target_date))
    return tuple(results)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """定义单交易所、有明确日期边界的目录同步参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--exchange", choices=[exchange.value for exchange in Exchange])
    group.add_argument("--all-exchanges", action="store_true", help="顺序同步三所并发布稳定聚合")
    parser.add_argument("--target-date", help="ISO 日期；默认 Asia/Shanghai 当天")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
