"""用于同步近期 A 股未复权日线窗口的有界运维 CLI。"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import date, timedelta

from service_data_sync.application.equity.daily_bar_sync import EquityDailyBarSyncService
from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import EquityIdentifier
from service_data_sync.infrastructure.object_storage.raw_payload_store import S3RawPayloadStore
from service_data_sync.infrastructure.persistence.equity_market_data_repository import (
    SqlAlchemyEquityMarketDataRepository,
)

_CAPABILITY = "equity.bar.1d.raw"


def main(argv: Sequence[str] | None = None) -> int:
    """同步一只证券的包含端日期窗口；默认实时测试跨度为 31 天。"""
    arguments = _parse_args(argv)
    identifier = EquityIdentifier.parse(arguments.instrument)
    end = date.fromisoformat(arguments.end) if arguments.end is not None else date.today()
    start = (
        date.fromisoformat(arguments.start)
        if arguments.start is not None
        else end - timedelta(days=31)
    )
    if start > end:
        raise SystemExit("--start must not be after --end")
    settings = load_settings()
    configure_logging(settings, process_role="equity-bars-cli")
    container = build_container(settings)
    try:
        sources = container.source_registry.for_capability(_CAPABILITY)
        # 一次运行使用多个供应商会使同一发布版本的来源归属不明确。
        if len(sources) != 1:
            raise SystemExit("exactly one approved daily-bar provider must be enabled")
        result = asyncio.run(
            EquityDailyBarSyncService(
                source=sources[0],
                repository=SqlAlchemyEquityMarketDataRepository(container.database),
                raw_payload_store=S3RawPayloadStore(container.object_storage),
            ).sync(identifier=identifier, start=start, end=end)
        )
    finally:
        container.close()
    print(
        json.dumps(
            {
                "instrument": result.instrument.qualified_symbol,
                "data_version": str(result.data_version),
                "inserted_count": result.inserted_count,
                "unchanged_count": result.unchanged_count,
            },
            separators=(",", ":"),
        )
    )
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """定义严格有界的运维参数，不提供无上限的历史默认值。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instrument", required=True, help="标准 EXCHANGE.SYMBOL，例如 SSE.600519")
    parser.add_argument("--start", help="包含端 ISO 日期；默认比结束日早 31 天")
    parser.add_argument("--end", help="包含端 ISO 日期；默认当天")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
