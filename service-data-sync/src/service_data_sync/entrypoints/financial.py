"""财务报表、供应商指标和历史估值 command 的受控手工提交入口。

三表、报告期指标和估值历史分别采集、质检和发布；此入口仅在具名来源策略与流控参数
都已批准时工作；CLI 不直接同步或发布，而是提交统一 command。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import Exchange
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    build_catalog,
)
from service_data_sync.infrastructure.data_operations.legacy_submission import submit_system_command


def main(argv: Sequence[str] | None = None) -> int:
    """提交一只已存在 A 股证券的财务 command，并输出受理收据。"""
    arguments = _parse_arguments(argv)
    settings = load_settings()
    if not settings.financial_enabled:
        raise SystemExit("DATA_SYNC_FINANCIAL_ENABLED must be true")
    configure_logging(settings, process_role="financial-cli")
    container = build_container(settings)
    try:
        control_plane = DataOperationsControlPlane(
            database=container.database,
            catalog=build_catalog(settings, container.source_registry),
            source_registry=container.source_registry,
            trading_calendar=container.trading_calendar,
        )
        receipt = submit_system_command(
            control_plane,
            target={
                "datasetCode": "financial.report",
                "mode": "INCREMENTAL",
                "selector": {
                    "kind": "INSTRUMENT",
                    "exchange": arguments.exchange,
                    "symbol": arguments.symbol,
                },
                "dateFrom": None,
                "dateTo": None,
                "observationDate": None,
            },
            reason="兼容财务 CLI 提交",
            request_prefix="legacy-financial-cli",
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


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """定义单证券、单来源的最小手工回补边界，不提供未经调度治理的全市场扫描。"""
    parser = argparse.ArgumentParser(prog="data-sync-financial")
    parser.add_argument(
        "--exchange", choices=[exchange.value for exchange in Exchange], required=True
    )
    parser.add_argument("--symbol", required=True, metavar="NNNNNN")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
