"""回收板块 EOD 过期租约的受控运维入口。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime

from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.persistence.sector_eod_repository import (
    SqlAlchemySectorEodRepository,
)


def main(argv: Sequence[str] | None = None) -> int:
    """显式回收已过期 lease；不抓取来源、不推断交易日也不创建 publication。"""
    _parse_arguments(argv)
    settings = load_settings()
    configure_logging(settings, process_role="sector-eod-reaper")
    container = build_container(settings)
    try:
        requeued = SqlAlchemySectorEodRepository(container.database).requeue_expired_leases(
            now=datetime.now(UTC)
        )
    finally:
        container.close()
    print(json.dumps({"requeued": requeued}, separators=(",", ":")))
    return 0


def _parse_arguments(argv: Sequence[str] | None) -> None:
    """拒绝未设计的筛选与强制参数，避免 reaper 越权处理非 EOD 任务。"""
    parser = argparse.ArgumentParser(prog="data-sync-sector-eod-reaper")
    parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
