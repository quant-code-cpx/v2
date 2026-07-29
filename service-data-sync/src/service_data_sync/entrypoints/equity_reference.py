"""同步单只 A 股复权因子、公司行动或公司概况的有界运维 CLI。

三个 reference 数据集各自拥有来源、版本和 publication，因子不由价格反算，公司行动
不由公司概况推断；调用方以明确证券和日期边界避免一次任务无意扩大回填范围。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import date, timedelta

from service_data_sync.application.equity.market_extension_sync import (
    EquityAdjustmentFactorSyncService,
    EquityCompanyProfileSyncService,
    EquityCorporateActionSyncService,
)
from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import EquityIdentifier
from service_data_sync.infrastructure.object_storage.raw_payload_store import (
    FailureEvidenceDataSource,
    S3RawPayloadStore,
    retain_failure_evidence,
)
from service_data_sync.infrastructure.persistence.equity_market_data_repository import (
    SqlAlchemyEquityMarketDataRepository,
)

_CAPABILITIES = (
    "equity.adjustment_factor",
    "equity.corporate_action",
    "equity.profile",
)
_HISTORY_START = date(1990, 12, 19)


def main(argv: Sequence[str] | None = None) -> int:
    """同步一个明确证券能力，并输出机器可读 publication 摘要。"""
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
        sources = container.source_registry.for_capability(arguments.capability)
        if len(sources) != 1:
            raise SystemExit("exactly one approved provider must be enabled for the capability")
        repository = SqlAlchemyEquityMarketDataRepository(container.database)
        raw_payload_store = S3RawPayloadStore(container.object_storage)
        if arguments.capability == "equity.adjustment_factor":
            result = retain_failure_evidence(
                raw_payload_store,
                # 同一执行边界仅在同步异常时将暂存来源字节固化为排障证据。
                lambda: asyncio.run(
                    EquityAdjustmentFactorSyncService(
                        source=FailureEvidenceDataSource(sources[0], raw_payload_store),
                        repository=repository,
                        raw_payload_store=raw_payload_store,
                    ).sync(identifier=identifier, start=start, end=end)
                ),
            )
        elif arguments.capability == "equity.corporate_action":
            result = retain_failure_evidence(
                raw_payload_store,
                # 同一执行边界仅在同步异常时将暂存来源字节固化为排障证据。
                lambda: asyncio.run(
                    EquityCorporateActionSyncService(
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
                    EquityCompanyProfileSyncService(
                        source=FailureEvidenceDataSource(sources[0], raw_payload_store),
                        repository=repository,
                        raw_payload_store=raw_payload_store,
                    ).sync(identifier=identifier)
                ),
            )
    finally:
        container.close()
    print(
        json.dumps(
            {
                "instrument": result.instrument.qualified_symbol,
                "capability": result.capability,
                "data_version": str(result.data_version),
                "inserted_count": result.inserted_count,
                "unchanged_count": result.unchanged_count,
            },
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
