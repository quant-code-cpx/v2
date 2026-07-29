"""用于同步 A 股上游直取日、周、月未复权行情的有界运维 CLI。"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import date, timedelta

from service_data_sync.application.equity.daily_bar_sync import EquityDailyBarSyncService
from service_data_sync.application.equity.market_extension_sync import EquityPeriodBarSyncService
from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import EquityBarPeriod, EquityIdentifier
from service_data_sync.infrastructure.object_storage.raw_payload_store import (
    FailureEvidenceDataSource,
    S3RawPayloadStore,
    retain_failure_evidence,
)
from service_data_sync.infrastructure.persistence.equity_market_data_repository import (
    SqlAlchemyEquityMarketDataRepository,
)

_HISTORY_START = date(1990, 12, 19)


def main(argv: Sequence[str] | None = None) -> int:
    """同步一只证券的包含端日期窗口；可显式执行首轮完整历史回填。"""
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
        sources = container.source_registry.for_capability(period.capability)
        # 一次运行使用多个供应商会使同一发布版本的来源归属不明确。
        if len(sources) != 1:
            raise SystemExit("exactly one approved provider must be enabled for the period")
        repository = SqlAlchemyEquityMarketDataRepository(container.database)
        raw_payload_store = S3RawPayloadStore(container.object_storage)
        if period is EquityBarPeriod.DAY_1:
            result = retain_failure_evidence(
                raw_payload_store,
                # 同一执行边界仅在同步异常时将暂存来源字节固化为排障证据。
                lambda: asyncio.run(
                    EquityDailyBarSyncService(
                        source=FailureEvidenceDataSource(sources[0], raw_payload_store),
                        repository=repository,
                        raw_payload_store=raw_payload_store,
                    ).sync(identifier=identifier, start=start, end=end)
                ),
            )
        else:
            result = retain_failure_evidence(
                raw_payload_store,
                # 同一执行边界仅在同步异常时将暂存来源字节固化为排障证据。
                lambda: asyncio.run(
                    EquityPeriodBarSyncService(
                        source=FailureEvidenceDataSource(sources[0], raw_payload_store),
                        repository=repository,
                        raw_payload_store=raw_payload_store,
                    ).sync(identifier=identifier, period=period, start=start, end=end)
                ),
            )
    finally:
        container.close()
    print(
        json.dumps(
            {
                "instrument": result.instrument.qualified_symbol,
                "period": period.value,
                "data_version": None if result.data_version is None else str(result.data_version),
                "inserted_count": result.inserted_count,
                "unchanged_count": result.unchanged_count,
                "availability": result.availability,
            },
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
