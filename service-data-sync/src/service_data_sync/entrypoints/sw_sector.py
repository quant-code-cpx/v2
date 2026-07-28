"""申万三级 taxonomy、估值同步与按日 raw replay 的受控 CLI。"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import date, datetime
from zoneinfo import ZoneInfo

from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.bootstrap.sw_sector import build_sw_sync_service

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def main(argv: Sequence[str] | None = None) -> int:
    """执行当天上游同步或精确历史日期 replay，并输出两个独立版本。"""
    arguments = _parse_arguments(argv)
    settings = load_settings()
    configure_logging(settings, process_role="sw-sector-cli")
    container = build_container(settings)
    snapshot_date = arguments.snapshot_date or datetime.now(_SHANGHAI).date()
    try:
        service = build_sw_sync_service(
            settings,
            database=container.database,
            object_storage=container.object_storage,
            replay_only=arguments.replay_raw,
        )
        result = (
            service.replay(snapshot_date=snapshot_date)
            if arguments.replay_raw
            else asyncio.run(service.sync(snapshot_date=snapshot_date))
        )
    finally:
        container.close()
    print(
        json.dumps(
            {
                "snapshotDate": snapshot_date.isoformat(),
                "replayed": result.replayed,
                "taxonomy": _publication(result.publications.taxonomy),
                "valuation": _publication(result.publications.valuation),
            },
            separators=(",", ":"),
        )
    )
    return 0


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """解析单观测日边界；历史日期只能与 `--replay-raw` 一起使用。"""
    parser = argparse.ArgumentParser(prog="data-sync-sw-sector")
    parser.add_argument("--snapshot-date", type=date.fromisoformat)
    parser.add_argument(
        "--replay-raw",
        action="store_true",
        help="从该日期已成功 checkpoint 的中立载荷重放，不访问上游",
    )
    arguments = parser.parse_args(argv)
    today = datetime.now(_SHANGHAI).date()
    if (
        not arguments.replay_raw
        and arguments.snapshot_date is not None
        and arguments.snapshot_date != today
    ):
        parser.error("historical snapshot dates require --replay-raw")
    return arguments


def _publication(value: object) -> dict[str, object]:
    """把发布结果投影为不含数据库键和对象存储 URI 的稳定 JSON。"""
    from service_data_sync.application.ports.sw_sector import SwPublishedCapability

    if not isinstance(value, SwPublishedCapability):
        raise TypeError("value must be SwPublishedCapability")
    return {
        "capability": value.capability,
        "dataVersion": str(value.data_version),
        "inserted": value.inserted_count,
        "unchanged": value.unchanged_count,
        "rowCount": value.row_count,
    }


if __name__ == "__main__":
    raise SystemExit(main())
