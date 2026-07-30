"""提交 A 股日、周、月未复权行情 command 的兼容运维 CLI。

CLI 只把明确证券、周期与日期窗口转换为严格 selector 和 SyncTarget；实际抓取、发布、
checkpoint 与终态均由统一 dispatcher 在 PostgreSQL fencing 边界内完成。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import date, timedelta

from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import EquityBarPeriod, EquityIdentifier
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    build_catalog,
)
from service_data_sync.infrastructure.data_operations.legacy_submission import submit_system_command

_HISTORY_START = date(1990, 12, 19)


def main(argv: Sequence[str] | None = None) -> int:
    """提交一只证券的 command；可显式请求首轮完整历史回填但不在当前进程执行。"""
    arguments = _parse_args(argv)
    identifier = EquityIdentifier.parse(arguments.instrument)
    period = EquityBarPeriod(arguments.period)
    end = date.fromisoformat(arguments.end) if arguments.end is not None else date.today()
    start = (
        _HISTORY_START
        if arguments.full_history
        else (
            date.fromisoformat(arguments.start)
            if arguments.start is not None
            else end - timedelta(days=31)
        )
    )
    if start > end:
        raise SystemExit("--start must not be after --end")
    settings = load_settings()
    configure_logging(settings, process_role="equity-bars-cli")
    container = build_container(settings)
    try:
        control_plane = DataOperationsControlPlane(
            database=container.database,
            catalog=build_catalog(settings, container.source_registry),
            source_registry=container.source_registry,
            trading_calendar=container.trading_calendar,
        )
        target = {
            "datasetCode": period.capability,
            "mode": "FULL" if arguments.full_history else "DATE_RANGE",
            "selector": {
                "kind": "INSTRUMENT",
                "exchange": identifier.exchange.value,
                "symbol": identifier.symbol,
            },
            "dateFrom": None if arguments.full_history else start.isoformat(),
            "dateTo": None if arguments.full_history else end.isoformat(),
            "observationDate": None,
        }
        receipt = submit_system_command(
            control_plane,
            target=target,
            reason="兼容个股行情 CLI 提交",
            request_prefix="legacy-equity-bars-cli",
        )
    finally:
        container.close()
    print(
        json.dumps(
            receipt,
            separators=(",", ":"),
        )
    )
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """定义严格有界的运维参数，不提供无上限的历史默认值。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instrument", required=True, help="标准 EXCHANGE.SYMBOL，例如 SSE.600519")
    parser.add_argument(
        "--period",
        choices=[period.value for period in EquityBarPeriod],
        default=EquityBarPeriod.DAY_1.value,
        help="上游直取周期；默认 1d",
    )
    parser.add_argument("--start", help="包含端 ISO 日期；默认比结束日早 31 天")
    parser.add_argument("--end", help="包含端 ISO 日期；默认当天")
    parser.add_argument(
        "--full-history",
        action="store_true",
        help="从 1990-12-19 开始执行有界历史回填",
    )
    arguments = parser.parse_args(argv)
    if arguments.full_history and arguments.start is not None:
        parser.error("--full-history and --start cannot be used together")
    return arguments


if __name__ == "__main__":
    raise SystemExit(main())
