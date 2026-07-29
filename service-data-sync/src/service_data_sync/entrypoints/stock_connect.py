"""执行一个已批准来源的沪深港通 P0 通道统计或活跃榜同步。

通道、方向、市场统计和活跃排行均是明确 capability；入口保留来源的报告日期与排行
位置，不会用持仓变化或另一方向的榜单推导“官方成交活跃”结论。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime

from service_data_sync.application.ports.data_source import DataSourcePort, ProviderError
from service_data_sync.application.stock_connect.active_security_sync import (
    StockConnectActiveSecuritySyncService,
)
from service_data_sync.application.stock_connect.market_daily_sync import (
    StockConnectMarketDailySyncService,
)
from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.stock_connect import StockConnectChannel
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
from service_data_sync.infrastructure.persistence.stock_connect_market_data_repository import (
    SqlAlchemyStockConnectMarketDataRepository,
    StockConnectSourceApproval,
)

_CAPABILITIES = {
    "market": "market.stock_connect.market_stat.reported",
    "active-security": "market.stock_connect.active_security.snapshot",
}


def main(argv: Sequence[str] | None = None) -> int:
    """同步一个通道方向的受控日期窗口，并输出对应独立 release 摘要。"""
    arguments = _parse_args(argv)
    settings = load_settings()
    configure_logging(settings, process_role="stock-connect-p0-cli")
    container = build_container(settings)
    try:
        capability = _CAPABILITIES[arguments.operation]
        source = select_single_source(
            sources=container.source_registry.for_capability(capability),
            provider_id=arguments.provider_id,
            capability=capability,
        )
        channel = StockConnectChannel(arguments.channel, arguments.direction)
        raw_payload_store = S3RawPayloadStore(container.object_storage)
        availability_repository = SqlAlchemyDatasetAvailabilityRepository(container.database)
        if source is None:
            result = _unavailable_result(
                arguments=arguments,
                channel=channel,
                availability_repository=availability_repository,
            )
        else:
            repository = SqlAlchemyStockConnectMarketDataRepository(
                container.database,
                approved_sources={
                    arguments.provider_id: build_source_approval(
                        arguments, StockConnectSourceApproval
                    )
                },
            )
            try:
                result = _sync_selected_source(
                    arguments=arguments,
                    source=source,
                    repository=repository,
                    raw_payload_store=raw_payload_store,
                    channel=channel,
                )
                if result.availability == "empty":
                    _empty_result(
                        arguments=arguments,
                        channel=channel,
                        availability_repository=availability_repository,
                        provider_id=source.provider_id,
                    )
            except ProviderError as error:
                if not is_source_unavailable_error(error):
                    raise
                result = _unavailable_result(
                    arguments=arguments,
                    channel=channel,
                    availability_repository=availability_repository,
                    reason_code=error.code.value,
                )
    finally:
        container.close()
    print(
        json.dumps(
            {
                "operation": arguments.operation,
                "channel": channel.channel,
                "direction": channel.direction,
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
    repository: SqlAlchemyStockConnectMarketDataRepository,
    raw_payload_store: S3RawPayloadStore,
    channel: StockConnectChannel,
):
    """用唯一注册来源同步港通事实，失败路径仍由留证包装器处理。"""
    if arguments.operation == "market":
        return retain_failure_evidence(
            raw_payload_store,
            # 失败时才归档 AKShare 来源字节，成功发布不保留副本。
            lambda: asyncio.run(
                StockConnectMarketDailySyncService(
                    source=FailureEvidenceDataSource(source, raw_payload_store),
                    repository=repository,
                    raw_payload_store=raw_payload_store,
                ).sync(channel=channel, start=arguments.start, end=arguments.end)
            ),
        )
    return retain_failure_evidence(
        raw_payload_store,
        # 失败时才归档 AKShare 来源字节，成功发布不保留副本。
        lambda: asyncio.run(
            StockConnectActiveSecuritySyncService(
                source=FailureEvidenceDataSource(source, raw_payload_store),
                repository=repository,
                raw_payload_store=raw_payload_store,
            ).sync(channel=channel, start=arguments.start, end=arguments.end)
        ),
    )


def _unavailable_result(
    *,
    arguments: argparse.Namespace,
    channel: StockConnectChannel,
    availability_repository: SqlAlchemyDatasetAvailabilityRepository,
    reason_code: str = "provider_not_registered",
) -> P0UnavailableSyncResult:
    """记录未注册港通来源，并保持消费者对该请求分区的空结果可观察。"""
    dataset = {
        "market": "market.stock_connect.market_stat.reported",
        "active-security": "market.stock_connect.active_security.snapshot",
    }[arguments.operation]
    availability_repository.record(
        dataset=dataset,
        partition_key=(
            f"{channel.channel}:{channel.direction}:"
            f"{arguments.start.isoformat()}:{arguments.end.isoformat()}"
        ),
        availability="source_unavailable",
        reason_code=reason_code,
        provider_id=arguments.provider_id,
        observed_at=datetime.now(UTC),
    )
    return P0UnavailableSyncResult()


def _empty_result(
    *,
    arguments: argparse.Namespace,
    channel: StockConnectChannel,
    availability_repository: SqlAlchemyDatasetAvailabilityRepository,
    provider_id: str,
) -> None:
    """记录来源成功响应但无港通事实的窗口，消费者可稳定显示为空。"""
    dataset = {
        "market": "market.stock_connect.market_stat.reported",
        "active-security": "market.stock_connect.active_security.snapshot",
    }[arguments.operation]
    availability_repository.record(
        dataset=dataset,
        partition_key=(
            f"{channel.channel}:{channel.direction}:"
            f"{arguments.start.isoformat()}:{arguments.end.isoformat()}"
        ),
        availability="empty",
        reason_code="no_matching_facts",
        provider_id=provider_id,
        observed_at=datetime.now(UTC),
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """定义单通道方向窗口，避免把沪深或南北向披露混合成同一分区。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=tuple(_CAPABILITIES), required=True)
    parser.add_argument("--channel", choices=("SH", "SZ"), required=True)
    parser.add_argument("--direction", choices=("NORTHBOUND", "SOUTHBOUND"), required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    add_source_approval_arguments(parser)
    arguments = parser.parse_args(argv)
    require_window(arguments, parser)
    return arguments


if __name__ == "__main__":
    raise SystemExit(main())
