"""执行一个已批准来源的融资融券 P0 市场、证券或资格同步。"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime

from service_data_sync.application.margin.market_daily_sync import MarginMarketDailySyncService
from service_data_sync.application.margin.security_sync import (
    MarginEligibilitySyncService,
    MarginSecurityDailySyncService,
)
from service_data_sync.application.ports.data_source import DataSourcePort, ProviderError
from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.margin import MarginVenue
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
from service_data_sync.infrastructure.persistence.margin_market_data_repository import (
    MarginSourceApproval,
    SqlAlchemyMarginMarketDataRepository,
)

_CAPABILITIES = {
    "market": "market.margin.market.1d.reported",
    "security": "market.margin.security.1d.reported",
    "eligibility": "market.margin.eligibility.reported",
}


def main(argv: Sequence[str] | None = None) -> int:
    """同步一个沪深两融 P0 分区，并输出机器可读的发布版本摘要。"""
    arguments = _parse_args(argv)
    settings = load_settings()
    configure_logging(settings, process_role="margin-p0-cli")
    container = build_container(settings)
    try:
        capability = _CAPABILITIES[arguments.operation]
        source = select_single_source(
            sources=container.source_registry.for_capability(capability),
            provider_id=arguments.provider_id,
            capability=capability,
        )
        venue = MarginVenue(arguments.venue)
        availability_repository = SqlAlchemyDatasetAvailabilityRepository(container.database)
        raw_payload_store = S3RawPayloadStore(container.object_storage)
        if source is None:
            result = _unavailable_result(
                arguments=arguments,
                venue=venue,
                availability_repository=availability_repository,
            )
        else:
            repository = SqlAlchemyMarginMarketDataRepository(
                container.database,
                approved_sources={
                    arguments.provider_id: build_source_approval(arguments, MarginSourceApproval)
                },
            )
            try:
                result = _sync_selected_source(
                    arguments=arguments,
                    source=source,
                    repository=repository,
                    raw_payload_store=raw_payload_store,
                    venue=venue,
                )
                if result.availability == "empty":
                    _empty_result(
                        arguments=arguments,
                        venue=venue,
                        availability_repository=availability_repository,
                        provider_id=source.provider_id,
                    )
            except ProviderError as error:
                if not is_source_unavailable_error(error):
                    raise
                result = _unavailable_result(
                    arguments=arguments,
                    venue=venue,
                    availability_repository=availability_repository,
                    reason_code=error.code.value,
                )
    finally:
        container.close()
    print(
        json.dumps(
            {
                "operation": arguments.operation,
                "venue": venue.code,
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
    repository: SqlAlchemyMarginMarketDataRepository,
    raw_payload_store: S3RawPayloadStore,
    venue: MarginVenue,
):
    """用唯一已注册来源同步两融事实；异常时仍由失败留证包装器负责固化字节。"""
    if arguments.operation == "market":
        return retain_failure_evidence(
            raw_payload_store,
            # 失败时才归档 AKShare 来源字节，成功发布不保留副本。
            lambda: asyncio.run(
                MarginMarketDailySyncService(
                    source=FailureEvidenceDataSource(source, raw_payload_store),
                    repository=repository,
                    raw_payload_store=raw_payload_store,
                ).sync(venue=venue, start=arguments.start, end=arguments.end)
            ),
        )
    if arguments.operation == "security":
        return retain_failure_evidence(
            raw_payload_store,
            # 失败时才归档 AKShare 来源字节，成功发布不保留副本。
            lambda: asyncio.run(
                MarginSecurityDailySyncService(
                    source=FailureEvidenceDataSource(source, raw_payload_store),
                    repository=repository,
                    raw_payload_store=raw_payload_store,
                ).sync(venue=venue, start=arguments.start, end=arguments.end)
            ),
        )
    return retain_failure_evidence(
        raw_payload_store,
        # 失败时才归档 AKShare 来源字节，成功发布不保留副本。
        lambda: asyncio.run(
            MarginEligibilitySyncService(
                source=FailureEvidenceDataSource(source, raw_payload_store),
                repository=repository,
                raw_payload_store=raw_payload_store,
            ).sync(venue=venue, start=arguments.start, end=arguments.end)
        ),
    )


def _unavailable_result(
    *,
    arguments: argparse.Namespace,
    venue: MarginVenue,
    availability_repository: SqlAlchemyDatasetAvailabilityRepository,
    reason_code: str = "provider_not_registered",
) -> P0UnavailableSyncResult:
    """记录未注册两融来源，让同一数据读取合同返回稳定空结果。"""
    dataset = {
        "market": "market.margin.market.1d.reported",
        "security": "market.margin.security.1d.reported",
        "eligibility": "market.margin.eligibility.reported",
    }[arguments.operation]
    availability_repository.record(
        dataset=dataset,
        partition_key=f"{venue.code}:{arguments.start.isoformat()}:{arguments.end.isoformat()}",
        availability="source_unavailable",
        reason_code=reason_code,
        provider_id=arguments.provider_id,
        observed_at=datetime.now(UTC),
    )
    return P0UnavailableSyncResult()


def _empty_result(
    *,
    arguments: argparse.Namespace,
    venue: MarginVenue,
    availability_repository: SqlAlchemyDatasetAvailabilityRepository,
    provider_id: str,
) -> None:
    """记录来源已响应但窗口无两融事实，避免将合法空列表误当同步失败。"""
    dataset = {
        "market": "market.margin.market.1d.reported",
        "security": "market.margin.security.1d.reported",
        "eligibility": "market.margin.eligibility.reported",
    }[arguments.operation]
    availability_repository.record(
        dataset=dataset,
        partition_key=f"{venue.code}:{arguments.start.isoformat()}:{arguments.end.isoformat()}",
        availability="empty",
        reason_code="no_matching_facts",
        provider_id=provider_id,
        observed_at=datetime.now(UTC),
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """定义单场所、单 capability 的两融窗口和来源批准参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=tuple(_CAPABILITIES), required=True)
    parser.add_argument("--venue", choices=("SSE", "SZSE"), required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    add_source_approval_arguments(parser)
    arguments = parser.parse_args(argv)
    require_window(arguments, parser)
    return arguments


if __name__ == "__main__":
    raise SystemExit(main())
