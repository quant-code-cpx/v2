"""显式资金流 capability 的手工或回补 CLI。

个股、板块、市场和供应商排行分别保留方法学、单位和发布日期；入口要求调用方选定
分区，绝不通过证券成分聚合、排行倒推或跨来源拼接来制造看似完整的资金流。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence

from service_data_sync.application.money_flow.sync import (
    MoneyFlowSyncResult,
    MoneyFlowSyncService,
)
from service_data_sync.application.ports.data_source import ProviderError
from service_data_sync.bootstrap.container import build_source_registry
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.object_storage.client import (
    ObjectStorageClient,
)
from service_data_sync.infrastructure.object_storage.raw_payload_store import (
    FailureEvidenceDataSource,
    S3RawPayloadStore,
    retain_failure_evidence_async,
)
from service_data_sync.infrastructure.persistence.money_flow_repository import (
    SqlAlchemyMoneyFlowRepository,
)
from service_data_sync.infrastructure.persistence.money_flow_run_ledger import (
    SqlAlchemyMoneyFlowRunLedger,
)

_CAPABILITIES = (
    "money_flow.order_size.daily.equity.raw",
    "money_flow.order_size.daily.sector.raw",
    "money_flow.order_size.daily.market.raw",
    "money_flow.order_size.ranking.equity.raw",
    "money_flow.order_size.ranking.sector.raw",
    "money_flow.trade_direction.ranking.equity.raw",
    "money_flow.trade_direction.ranking.industry.raw",
    "money_flow.trade_direction.ranking.concept.raw",
)


def main(argv: Sequence[str] | None = None) -> int:
    """解析明确能力和参数，执行带共享 checkpoint 的单分区同步。"""
    parser = argparse.ArgumentParser(description="同步一个明确的日频资金流来源分区")
    parser.add_argument("--capability", choices=_CAPABILITIES, required=True)
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="重复传入 adapter 的显式中立参数",
    )
    parser.add_argument(
        "--mode",
        choices=("manual", "backfill"),
        default="manual",
    )
    arguments = parser.parse_args(argv)
    parameters = _parameters(arguments.param)
    result = asyncio.run(
        _run(
            capability=arguments.capability,
            parameters=parameters,
            mode=arguments.mode,
        )
    )
    print(
        json.dumps(
            {
                "capability": result.capability,
                "rawUri": result.raw_uri,
                "sourcePayloadSha256": result.source_payload_sha256,
                "dataVersion": (
                    None
                    if result.publication.data_version is None
                    else str(result.publication.data_version)
                ),
                "published": result.publication.published,
                "qualityStatus": result.publication.quality_status,
                "insertedCount": result.publication.inserted_count,
                "revisedCount": result.publication.revised_count,
                "unchangedCount": result.publication.unchanged_count,
            },
            ensure_ascii=False,
        )
    )
    return 0


async def _run(
    *,
    capability: str,
    parameters: tuple[tuple[str, str], ...],
    mode: str,
) -> MoneyFlowSyncResult:
    """组合短生命周期依赖，并以同一请求键支持失败后的安全接管。"""
    settings = load_settings()
    if not settings.money_flow_enabled:
        raise RuntimeError("money-flow sync is disabled")
    registry = build_source_registry(settings)
    providers = registry.for_capability(capability)
    if len(providers) != 1:
        raise RuntimeError("exactly one money-flow provider must be enabled")
    database = DatabaseClient.from_settings(settings)
    object_storage = ObjectStorageClient.from_settings(settings)
    ledger = SqlAlchemyMoneyFlowRunLedger(database)
    run = ledger.start(
        capability=capability,
        parameters=parameters,
        mode=mode,
    )
    try:
        raw_payload_store = S3RawPayloadStore(object_storage)
        result = await retain_failure_evidence_async(
            raw_payload_store,
            # 同一执行边界仅在同步异常时将暂存来源字节固化为排障证据。
            lambda: MoneyFlowSyncService(
                source=FailureEvidenceDataSource(providers[0], raw_payload_store),
                repository=SqlAlchemyMoneyFlowRepository(database),
                raw_payload_store=raw_payload_store,
            ).sync(
                capability=capability,
                parameters=parameters,
                run_id=run.run_id,
                partition_key=run.partition_key,
            ),
        )
        ledger.finish(run=run, result=result)
        return result
    except ProviderError as error:
        ledger.fail(
            run=run,
            error_code=f"provider-{error.code.value}",
            retryable=error.retryable,
        )
        raise
    except Exception:
        ledger.fail(
            run=run,
            error_code="money-flow-sync-failed",
            retryable=False,
        )
        raise
    finally:
        object_storage.close()
        database.close()


def _parameters(values: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """解析 KEY=VALUE 参数，拒绝空键、空值和重复键。"""
    parsed: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        key = key.strip()
        item = item.strip()
        if not separator or not key or not item:
            raise ValueError("money-flow parameter must be KEY=VALUE")
        if key in parsed:
            raise ValueError(f"duplicate money-flow parameter: {key}")
        parsed[key] = item
    return tuple(sorted(parsed.items()))


if __name__ == "__main__":
    raise SystemExit(main())
