"""执行一个已批准来源的龙虎榜或大宗交易 P0 同步。"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime

from service_data_sync.application.ports.data_source import DataSourcePort, ProviderError
from service_data_sync.application.trading_events.sync import (
    BlockTradeSyncService,
    DragonTigerSyncService,
)
from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.entrypoints._p0 import (
    P0UnavailableSyncResult,
    add_source_approval_arguments,
    build_source_approval,
    is_source_unavailable_error,
    require_window,
    select_single_source,
)
from service_data_sync.infrastructure.object_storage.raw_payload_store import (
    FailureEvidenceDataSource,
    S3RawPayloadStore,
    retain_failure_evidence,
)
from service_data_sync.infrastructure.persistence.dataset_availability_repository import (
    SqlAlchemyDatasetAvailabilityRepository,
)
from service_data_sync.infrastructure.persistence.trading_events_repository import (
    SqlAlchemyTradingEventsRepository,
    TradingEventsSourceApproval,
)

_CAPABILITIES = {
    "dragon-tiger": "market.dragon_tiger.disclosure.1d",
    "block-trade": "market.block_trade.execution.1d",
}


def main(argv: Sequence[str] | None = None) -> int:
    """同步一个独立交易公开信息 capability，禁止用另一事实集补齐缺失字段。"""
    arguments = _parse_args(argv)
    settings = load_settings()
    configure_logging(settings, process_role="trading-events-p0-cli")
    container = build_container(settings)
    try:
        capability = _CAPABILITIES[arguments.operation]
        source = select_single_source(
            sources=container.source_registry.for_capability(capability),
            provider_id=arguments.provider_id,
            capability=capability,
        )
        raw_payload_store = S3RawPayloadStore(container.object_storage)
        availability_repository = SqlAlchemyDatasetAvailabilityRepository(container.database)
        if source is None:
            result = _unavailable_result(
                arguments=arguments,
                availability_repository=availability_repository,
            )
        else:
            repository = SqlAlchemyTradingEventsRepository(
                container.database,
                approved_sources={
                    arguments.provider_id: build_source_approval(
                        arguments, TradingEventsSourceApproval
                    )
                },
            )
            try:
                result = _sync_selected_source(
                    arguments=arguments,
                    source=source,
                    repository=repository,
                    raw_payload_store=raw_payload_store,
                )
                if result.availability == "empty":
                    _empty_result(
                        arguments=arguments,
                        availability_repository=availability_repository,
                        provider_id=source.provider_id,
                    )
            except ProviderError as error:
                if not is_source_unavailable_error(error):
                    raise
                result = _unavailable_result(
                    arguments=arguments,
                    availability_repository=availability_repository,
                    reason_code=error.code.value,
                )
    finally:
        container.close()
    print(
        json.dumps(
            {
                "operation": arguments.operation,
                "dataVersion": None if result.data_version is None else str(result.data_version),
                "insertedCount": result.inserted_count,
                "unchangedCount": result.unchanged_count,
                "availability": getattr(result, "availability", "available"),
            },
            separators=(",", ":"),
        )
    )
    return 0


def _sync_selected_source(
    *,
    arguments: argparse.Namespace,
    source: DataSourcePort,
    repository: SqlAlchemyTradingEventsRepository,
    raw_payload_store: S3RawPayloadStore,
):
    """使用唯一已注册来源同步独立公开交易事实，失败时按既有策略留证。"""
    if arguments.operation == "dragon-tiger":
        return retain_failure_evidence(
            raw_payload_store,
            # 失败时才归档 AKShare 来源字节，成功发布不保留副本。
            lambda: asyncio.run(
                DragonTigerSyncService(
                    source=FailureEvidenceDataSource(source, raw_payload_store),
                    repository=repository,
                    raw_payload_store=raw_payload_store,
                ).sync(start=arguments.start, end=arguments.end)
            ),
        )
    return retain_failure_evidence(
        raw_payload_store,
        # 失败时才归档 AKShare 来源字节，成功发布不保留副本。
        lambda: asyncio.run(
            BlockTradeSyncService(
                source=FailureEvidenceDataSource(source, raw_payload_store),
                repository=repository,
                raw_payload_store=raw_payload_store,
            ).sync(start=arguments.start, end=arguments.end)
        ),
    )


def _unavailable_result(
    *,
    arguments: argparse.Namespace,
    availability_repository: SqlAlchemyDatasetAvailabilityRepository,
    reason_code: str = "provider_not_registered",
) -> P0UnavailableSyncResult:
    """记录未注册交易公开信息来源，读取端可返回空数组而无需等待 adapter。"""
    dataset = {
        "dragon-tiger": "market.dragon_tiger.disclosure.1d",
        "block-trade": "market.block_trade.execution.1d",
    }[arguments.operation]
    availability_repository.record(
        dataset=dataset,
        partition_key=f"{arguments.start.isoformat()}:{arguments.end.isoformat()}",
        availability="source_unavailable",
        reason_code=reason_code,
        provider_id=arguments.provider_id,
        observed_at=datetime.now(UTC),
    )
    return P0UnavailableSyncResult()


def _empty_result(
    *,
    arguments: argparse.Namespace,
    availability_repository: SqlAlchemyDatasetAvailabilityRepository,
    provider_id: str,
) -> None:
    """记录来源成功响应但无公开交易事件的日期窗，保持 API 返回空数组。"""
    dataset = {
        "dragon-tiger": "market.dragon_tiger.disclosure.1d",
        "block-trade": "market.block_trade.execution.1d",
    }[arguments.operation]
    availability_repository.record(
        dataset=dataset,
        partition_key=f"{arguments.start.isoformat()}:{arguments.end.isoformat()}",
        availability="empty",
        reason_code="no_matching_facts",
        provider_id=provider_id,
        observed_at=datetime.now(UTC),
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """定义互斥交易事实任务的有界窗口与显式来源批准信息。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=tuple(_CAPABILITIES), required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    add_source_approval_arguments(parser)
    arguments = parser.parse_args(argv)
    require_window(arguments, parser)
    return arguments


if __name__ == "__main__":
    raise SystemExit(main())
