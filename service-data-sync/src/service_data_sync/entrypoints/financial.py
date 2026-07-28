"""财务报表、供应商指标和历史估值的受控手工同步入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence

from service_data_sync.application.financial.sync import FinancialSyncService
from service_data_sync.application.ports.financial_sync import FinancialPublicationResult
from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import Exchange
from service_data_sync.infrastructure.object_storage.raw_payload_store import S3RawPayloadStore
from service_data_sync.infrastructure.persistence.financial_sync_repository import (
    SqlAlchemyFinancialSyncRepository,
)

_STATEMENT_CAPABILITY = "financial.statement.raw"


def main(argv: Sequence[str] | None = None) -> int:
    """同步一只已存在 A 股证券的三类财务能力，并输出各能力独立发布版本。"""
    arguments = _parse_arguments(argv)
    settings = load_settings()
    if not settings.financial_enabled:
        raise SystemExit("DATA_SYNC_FINANCIAL_ENABLED must be true")
    configure_logging(settings, process_role="financial-cli")
    container = build_container(settings)
    try:
        providers = container.source_registry.for_capability(_STATEMENT_CAPABILITY)
        if len(providers) != 1:
            raise SystemExit("exactly one financial provider must be enabled")
        result = asyncio.run(
            FinancialSyncService(
                source=providers[0],
                repository=SqlAlchemyFinancialSyncRepository(container.database),
                raw_payload_store=S3RawPayloadStore(container.object_storage),
            ).sync_security(exchange=Exchange(arguments.exchange), symbol=arguments.symbol)
        )
    finally:
        container.close()
    print(
        json.dumps(
            {
                "exchange": arguments.exchange,
                "symbol": arguments.symbol,
                "reports": _result(result.reports),
                "providerMetrics": _result(result.provider_metrics),
                "valuations": _result(result.valuations),
            },
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


def _result(value: FinancialPublicationResult) -> dict[str, object]:
    """投影发布结果为 CLI 稳定 JSON，不暴露数据库键或来源原始字段。"""
    return {
        "dataVersion": str(value.data_version),
        "inserted": value.inserted_count,
        "unchanged": value.unchanged_count,
    }


if __name__ == "__main__":
    raise SystemExit(main())
