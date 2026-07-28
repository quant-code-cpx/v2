"""平台派生财务指标的受控单证券手工与回补入口。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from uuid import uuid4

from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.financial_derived import run_financial_derivation
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import Exchange


def main(argv: Sequence[str] | None = None) -> int:
    """从已发布报表为一只证券计算派生指标，并输出独立 publication 摘要。"""
    arguments = _parse_arguments(argv)
    settings = load_settings()
    if not settings.financial_enabled:
        raise SystemExit("DATA_SYNC_FINANCIAL_ENABLED must be true")
    configure_logging(settings, process_role="financial-derived-cli")
    container = build_container(settings)
    try:
        result = run_financial_derivation(
            database=container.database,
            exchange=Exchange(arguments.exchange),
            symbol=arguments.symbol,
            mode=arguments.mode,
            request_key=f"cli:{uuid4()}",
        )
    finally:
        container.close()
    print(
        json.dumps(
            {
                "exchange": arguments.exchange,
                "symbol": arguments.symbol,
                "dataVersion": str(result.publication.data_version),
                "computed": result.computed_count,
                "skipped": result.skipped_count,
                "rowCount": result.publication.row_count,
            },
            separators=(",", ":"),
        )
    )
    return 0


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """定义单证券派生与回补参数；全市场编排由外部任务分片负责。"""
    parser = argparse.ArgumentParser(prog="data-sync-financial-derived")
    parser.add_argument(
        "--exchange", choices=[exchange.value for exchange in Exchange], required=True
    )
    parser.add_argument("--symbol", required=True, metavar="NNNNNN")
    parser.add_argument("--mode", choices=["manual", "backfill"], default="manual")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
