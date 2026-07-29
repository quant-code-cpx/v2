"""执行一个已批准来源的公司公告业绩事件 P0 同步。"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime

from service_data_sync.application.corporate_events.sync import CorporateEventsSyncService
from service_data_sync.application.ports.data_source import DataSourcePort, ProviderError
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
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.object_storage.raw_payload_store import (
    FailureEvidenceDataSource,
    S3RawPayloadStore,
    retain_failure_evidence,
)
from service_data_sync.infrastructure.persistence.corporate_events_repository import (
    CorporateSourceApproval,
    SqlAlchemyCorporateEventsRepository,
)
from service_data_sync.infrastructure.persistence.dataset_availability_repository import (
    SqlAlchemyDatasetAvailabilityRepository,
)

_CAPABILITY = "corporate.disclosure.earnings.p0"


def main(argv: Sequence[str] | None = None) -> int:
    """同步一个有界公告窗口，并输出不含公告 URL 或 Provider 字段的发布摘要。"""
    arguments = _parse_args(argv)
    settings = load_settings()
    configure_logging(settings, process_role="corporate-events-p0-cli")
    container = build_container(settings)
    try:
        source = select_single_source(
            sources=container.source_registry.for_capability(_CAPABILITY),
            provider_id=arguments.provider_id,
            capability=_CAPABILITY,
        )
        raw_payload_store = S3RawPayloadStore(container.object_storage)
        availability_repository = SqlAlchemyDatasetAvailabilityRepository(container.database)
        if source is None:
            result = _unavailable_result(
                arguments=arguments,
                availability_repository=availability_repository,
            )
        else:
            try:
                result = _sync_selected_source(
                    arguments=arguments,
                    source=source,
                    database=container.database,
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
    database: DatabaseClient,
    raw_payload_store: S3RawPayloadStore,
):
    """使用唯一已注册来源同步公告；解析或发布失败仍按失败留证策略处理。"""
    return retain_failure_evidence(
        raw_payload_store,
        # 失败时才归档 AKShare 来源字节，成功发布不保留副本。
        lambda: asyncio.run(
            CorporateEventsSyncService(
                source=FailureEvidenceDataSource(source, raw_payload_store),
                repository=SqlAlchemyCorporateEventsRepository(
                    database,
                    approved_sources={
                        arguments.provider_id: build_source_approval(
                            arguments, CorporateSourceApproval
                        )
                    },
                ),
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
    """记录公告来源未注册，前端和 API 可按空列表继续而非等待来源接线。"""
    availability_repository.record(
        dataset="corporate.disclosure.earnings.p0",
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
    """记录来源已完成但无公告的窗口，避免前端把无披露误显示为服务故障。"""
    availability_repository.record(
        dataset="corporate.disclosure.earnings.p0",
        partition_key=f"{arguments.start.isoformat()}:{arguments.end.isoformat()}",
        availability="empty",
        reason_code="no_matching_facts",
        provider_id=provider_id,
        observed_at=datetime.now(UTC),
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """定义公告同步的有限日期窗与不可省略的来源权利批准字段。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    add_source_approval_arguments(parser)
    arguments = parser.parse_args(argv)
    require_window(arguments, parser)
    return arguments


if __name__ == "__main__":
    raise SystemExit(main())
