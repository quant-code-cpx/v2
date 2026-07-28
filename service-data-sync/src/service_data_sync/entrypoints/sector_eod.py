"""板块 EOD 横截面手工同步入口；生产定时启用前仅支持显式目标日。"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import date

from service_data_sync.application.ports.sector_eod import SectorEodExecutionMode
from service_data_sync.application.sector.eod_schedule import sector_eod_source_cutoff_at
from service_data_sync.application.sector.eod_snapshot_sync import SectorEodSnapshotSyncService
from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.sector import SectorScheme
from service_data_sync.infrastructure.object_storage.raw_payload_store import S3RawPayloadStore
from service_data_sync.infrastructure.persistence.sector_eod_repository import (
    SqlAlchemySectorEodRepository,
)

_CAPABILITY = "sector.quote.eod.snapshot.raw"


def main(argv: Sequence[str] | None = None) -> int:
    """解析显式 scheme/date，执行一次受策略开关保护的 EOD 横截面同步。"""
    arguments = _parse_arguments(argv)
    target_date = date.fromisoformat(arguments.trade_date)
    scheme = SectorScheme(arguments.scheme)
    settings = load_settings()
    if not settings.sector_eod_enabled:
        raise SystemExit("DATA_SYNC_SECTOR_EOD_ENABLED must be true")
    if arguments.publish and not settings.sector_eod_publish_enabled:
        raise SystemExit("DATA_SYNC_SECTOR_EOD_PUBLISH_ENABLED must be true for --publish")
    configure_logging(settings, process_role="sector-eod-cli")
    container = build_container(settings)
    try:
        providers = container.source_registry.for_capability(_CAPABILITY)
        if len(providers) != 1:
            raise SystemExit("exactly one approved sector-eod provider must be enabled")
        service = SectorEodSnapshotSyncService(
            source=providers[0],
            repository=SqlAlchemySectorEodRepository(container.database),
            raw_payload_store=S3RawPayloadStore(container.object_storage),
            trading_calendar=container.trading_calendar,
        )
        operation = service.replay if arguments.replay_raw else service.sync
        execution_mode = (
            SectorEodExecutionMode.PUBLISH if arguments.publish else SectorEodExecutionMode.SHADOW
        )
        result = asyncio.run(
            operation(
                scheme=scheme,
                trade_date=target_date,
                source_cutoff_at=sector_eod_source_cutoff_at(target_date),
                execution_mode=execution_mode,
            )
        )
    finally:
        container.close()
    print(
        json.dumps(
            {
                "scheme": result.snapshot.scheme.value,
                "tradeDate": result.snapshot.trade_date.isoformat(),
                "dataVersion": str(result.snapshot.data_version),
                "inserted": result.inserted,
                "finality": result.snapshot.finality.value,
                "state": "published" if result.snapshot.published_at is not None else "candidate",
                "executionMode": result.execution_mode.value,
            },
            separators=(",", ":"),
        )
    )
    return 0


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """定义仅允许行业或概念、目标日及受控 raw replay 的手工回补接口。"""
    parser = argparse.ArgumentParser(prog="data-sync-sector-eod")
    parser.add_argument(
        "--scheme", choices=[scheme.value for scheme in SectorScheme], required=True
    )
    parser.add_argument("--trade-date", required=True, metavar="YYYY-MM-DD")
    parser.add_argument(
        "--replay-raw",
        action="store_true",
        help="只从该分区 checkpoint 指向的 raw evidence 重放，绝不访问 provider",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="推进 consumer publication；默认仅保存 shadow candidate",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
