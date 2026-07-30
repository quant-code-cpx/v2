"""提交单只 A 股复权因子、公司行动或公司概况 command 的兼容 CLI。

三个 reference 数据集各自拥有来源、版本和 publication。CLI 只生成严格 INSTRUMENT
selector 与有界 SyncTarget，实际同步由统一 dispatcher 的 fenced executor 执行。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import date, timedelta

from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import EquityIdentifier
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    build_catalog,
)
from service_data_sync.infrastructure.data_operations.legacy_submission import submit_system_command

_CAPABILITIES = (
    "equity.adjustment_factor",
    "equity.corporate_action",
    "equity.profile",
)
_HISTORY_START = date(1990, 12, 19)


def main(argv: Sequence[str] | None = None) -> int:
    """提交一个明确证券能力，并输出机器可读 command 受理收据。"""
    arguments = _parse_args(argv)
    identifier = EquityIdentifier.parse(arguments.instrument)
    end = date.fromisoformat(arguments.end) if arguments.end is not None else date.today()
    start = (
        _HISTORY_START
        if arguments.full_history
        else (
            date.fromisoformat(arguments.start)
            if arguments.start is not None
            else end - timedelta(days=3 * 366)
        )
    )
    if start > end:
        raise SystemExit("--start must not be after --end")
    settings = load_settings()
    configure_logging(settings, process_role="equity-reference-cli")
    container = build_container(settings)
    try:
        control_plane = DataOperationsControlPlane(
            database=container.database,
            catalog=build_catalog(settings, container.source_registry),
            source_registry=container.source_registry,
            trading_calendar=container.trading_calendar,
        )
        target = {
            "datasetCode": arguments.capability,
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
        if arguments.capability == "equity.profile":
            target["mode"] = "INCREMENTAL"
            target["dateFrom"] = None
            target["dateTo"] = None
        receipt = submit_system_command(
            control_plane,
            target=target,
            reason="兼容个股参考数据 CLI 提交",
            request_prefix="legacy-equity-reference-cli",
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
    """定义能力、证券和有界日期参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instrument", required=True, help="标准 EXCHANGE.SYMBOL，例如 SSE.600519")
    parser.add_argument("--capability", choices=_CAPABILITIES, required=True)
    parser.add_argument("--start", help="包含端 ISO 日期；默认回看三年")
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
