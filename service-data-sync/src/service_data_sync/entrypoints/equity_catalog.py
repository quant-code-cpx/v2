"""同步一所交易所当前完整 A 股证券目录的运维 CLI。

目录快照只建立证券身份、名称和市场归属；供应商缺席或空响应不会被解释为退市，上市、
暂停和恢复等生命周期结论必须由独立的显式事实链路发布。
"""

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
from service_data_sync.application.ports.equity_master import PublishedCnAAggregate
from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import Exchange
from service_data_sync.infrastructure.object_storage.raw_payload_store import (
    FailureEvidenceDataSource,
    S3RawPayloadStore,
    retain_failure_evidence,
)
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
        raw_payload_store = S3RawPayloadStore(container.object_storage)
        service = EquityCatalogSyncService(
            source=FailureEvidenceDataSource(sources[0], raw_payload_store),
            repository=repository,
            raw_payload_store=raw_payload_store,
        )
        results, aggregate = retain_failure_evidence(
            raw_payload_store,
            lambda: _sync_and_publish(
                service,
                repository=repository,
                exchanges=exchanges,
                target_date=target_date,
                publish_aggregate=arguments.all_exchanges,
            ),
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
    service: EquityCatalogSyncService, *, exchanges: tuple[Exchange, ...], target_date: date
) -> tuple[EquityCatalogSyncResult, ...]:
    """顺序同步三所目录，确保每个 AKShare 现货请求独立保留证据且避免上游并发压力。"""
    results: list[EquityCatalogSyncResult] = []
    for exchange in exchanges:
        results.append(await service.sync(exchange=exchange, target_date=target_date))
    return tuple(results)


def _sync_and_publish(
    service: EquityCatalogSyncService,
    *,
    repository: SqlAlchemyEquityMasterRepository,
    exchanges: tuple[Exchange, ...],
    target_date: date,
    publish_aggregate: bool,
) -> tuple[tuple[EquityCatalogSyncResult, ...], PublishedCnAAggregate | None]:
    """在同一失败证据边界内顺序同步目录并可选发布全市场聚合。"""
    results = asyncio.run(_sync_exchanges(service, exchanges=exchanges, target_date=target_date))
    aggregate = repository.publish_cn_a_aggregate() if publish_aggregate else None
    return results, aggregate


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
