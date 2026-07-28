"""将板块 EOD consumer publication 回滚到已通过历史 revision 的受控运维入口。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import date

from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.sector import SectorScheme
from service_data_sync.infrastructure.persistence.sector_eod_repository import (
    SqlAlchemySectorEodRepository,
)


def main(argv: Sequence[str] | None = None) -> int:
    """校验显式目标后恢复旧 publication；不访问 provider、日历或对象存储。"""
    arguments = _parse_arguments(argv)
    settings = load_settings()
    if not settings.sector_eod_publish_enabled:
        raise SystemExit("DATA_SYNC_SECTOR_EOD_PUBLISH_ENABLED must be true for rollback")
    configure_logging(settings, process_role="sector-eod-rollback")
    container = build_container(settings)
    try:
        snapshot = SqlAlchemySectorEodRepository(container.database).rollback_published_snapshot(
            scheme=SectorScheme(arguments.scheme),
            trade_date=date.fromisoformat(arguments.trade_date),
            revision=arguments.revision,
        )
    finally:
        container.close()
    print(
        json.dumps(
            {
                "scheme": snapshot.scheme.value,
                "tradeDate": snapshot.trade_date.isoformat(),
                "dataVersion": str(snapshot.data_version),
                "revision": arguments.revision,
                "state": "published",
            },
            separators=(",", ":"),
        )
    )
    return 0


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """只接受 scheme、精确交易日和正 revision，避免无目标或 candidate 回滚。"""
    parser = argparse.ArgumentParser(prog="data-sync-sector-eod-rollback")
    parser.add_argument(
        "--scheme", choices=[scheme.value for scheme in SectorScheme], required=True
    )
    parser.add_argument("--trade-date", required=True, metavar="YYYY-MM-DD")
    parser.add_argument("--revision", type=int, required=True)
    arguments = parser.parse_args(argv)
    if arguments.revision < 1:
        parser.error("--revision must be positive")
    return arguments


if __name__ == "__main__":
    raise SystemExit(main())
