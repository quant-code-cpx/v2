"""执行一个已批准来源的 ETF P0 产品资料、状态、日线或净值同步。

每项能力独立选择仓储和发布边界，资料快照不能补齐状态或净值；默认来源仅限个人内部
研究、禁止再分发，未注册来源会被记录为安全空结果而非伪造业务数据。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime

from service_data_sync.application.etf.daily_bar_sync import EtfDailyBarSyncService
from service_data_sync.application.etf.nav_sync import EtfNavSyncService
from service_data_sync.application.etf.reference_sync import (
    EtfMasterSyncService,
    EtfStatusSyncService,
)
from service_data_sync.application.ports.data_source import DataSourcePort
from service_data_sync.application.ports.market_data import RawPayloadStore
from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.etf import EtfIdentifier
from service_data_sync.entrypoints._p0 import (
    P0UnavailableSyncResult,
    add_source_approval_arguments,
    build_source_approval,
    require_window,
    select_single_source,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.object_storage.raw_payload_store import (
    FailureEvidenceDataSource,
    S3RawPayloadStore,
    retain_failure_evidence,
)
from service_data_sync.infrastructure.persistence.dataset_availability_repository import (
    SqlAlchemyDatasetAvailabilityRepository,
)
from service_data_sync.infrastructure.persistence.etf_market_data_repository import (
    EtfSourceApproval,
    SqlAlchemyEtfMarketDataRepository,
)
from service_data_sync.infrastructure.persistence.etf_reference_repository import (
    EtfReferenceSourceApproval,
    SqlAlchemyEtfReferenceRepository,
)

_CAPABILITIES = {
    "master": "fund.etf.master",
    "status": "fund.etf.trading_state",
    "bars": "fund.etf.bar.1d.raw",
    "nav": "fund.etf.nav.1d.reported",
}


def main(argv: Sequence[str] | None = None) -> int:
    """同步一个明确 ETF capability，并输出不含 Provider 细节的发布摘要。"""
    arguments = _parse_args(argv)
    settings = load_settings()
    configure_logging(settings, process_role="etf-p0-cli")
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
        elif arguments.operation == "master":
            result = retain_failure_evidence(
                raw_payload_store,
                # 失败时才归档 AKShare 来源字节，成功发布不保留副本。
                lambda: asyncio.run(
                    EtfMasterSyncService(
                        source=FailureEvidenceDataSource(source, raw_payload_store),
                        repository=SqlAlchemyEtfReferenceRepository(
                            container.database,
                            approved_sources={
                                arguments.provider_id: build_source_approval(
                                    arguments, EtfReferenceSourceApproval
                                )
                            },
                        ),
                        raw_payload_store=raw_payload_store,
                        availability_repository=availability_repository,
                    ).sync(venue=arguments.venue, observation_date=arguments.observation_date)
                ),
            )
        else:
            etf = EtfIdentifier.parse(arguments.etf)
            result = retain_failure_evidence(
                raw_payload_store,
                # 失败时才归档 AKShare 来源字节，成功发布不保留副本。
                lambda: _sync_listing_operation(
                    arguments=arguments,
                    source=FailureEvidenceDataSource(source, raw_payload_store),
                    database=container.database,
                    raw_payload_store=raw_payload_store,
                    etf=etf,
                    availability_repository=availability_repository,
                ),
            )
    finally:
        container.close()
    print(json.dumps(_result_payload(arguments.operation, result), separators=(",", ":")))
    return 0


def _sync_listing_operation(
    *,
    arguments: argparse.Namespace,
    source: DataSourcePort,
    database: DatabaseClient,
    raw_payload_store: RawPayloadStore,
    etf: EtfIdentifier,
    availability_repository: SqlAlchemyDatasetAvailabilityRepository,
) -> object:
    """按显式 ETF 上市工具调用状态、日线或净值任务，三者不共享发布仓储。"""
    if arguments.operation == "status":
        return asyncio.run(
            EtfStatusSyncService(
                source=source,
                repository=SqlAlchemyEtfReferenceRepository(
                    database,
                    approved_sources={
                        arguments.provider_id: build_source_approval(
                            arguments, EtfReferenceSourceApproval
                        )
                    },
                ),
                raw_payload_store=raw_payload_store,
                availability_repository=availability_repository,
            ).sync(etf=etf, start=arguments.start, end=arguments.end)
        )
    repository = SqlAlchemyEtfMarketDataRepository(
        database,
        approved_sources={arguments.provider_id: _build_market_approval(arguments)},
    )
    if arguments.operation == "bars":
        return asyncio.run(
            EtfDailyBarSyncService(
                source=source,
                repository=repository,
                raw_payload_store=raw_payload_store,
                availability_repository=availability_repository,
            ).sync(etf=etf, start=arguments.start, end=arguments.end)
        )
    return asyncio.run(
        EtfNavSyncService(
            source=source,
            repository=repository,
            raw_payload_store=raw_payload_store,
            availability_repository=availability_repository,
        ).sync(etf=etf, start=arguments.start, end=arguments.end)
    )


def _unavailable_result(
    *,
    arguments: argparse.Namespace,
    availability_repository: SqlAlchemyDatasetAvailabilityRepository,
) -> P0UnavailableSyncResult:
    """记录未注册或歧义 ETF adapter，并让个人调用链以空结果继续。"""
    if arguments.operation == "master":
        dataset = "fund.etf.profile.reported"
        partition_key = f"{arguments.venue}:{arguments.observation_date.isoformat()}"
    else:
        etf = EtfIdentifier.parse(arguments.etf)
        dataset = {
            "status": "fund.etf.trading_state.reported",
            "bars": "fund.etf.bar.1d.reported",
            "nav": "fund.etf.nav.1d.reported",
        }[arguments.operation]
        partition_key = (
            f"{etf.qualified_key}:{arguments.start.isoformat()}:{arguments.end.isoformat()}"
        )
    availability_repository.record(
        dataset=dataset,
        partition_key=partition_key,
        availability="source_unavailable",
        reason_code="provider_not_registered",
        provider_id=arguments.provider_id,
        observed_at=datetime.now(UTC),
    )
    return P0UnavailableSyncResult()


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """定义 ETF 分能力输入，目录快照和上市工具窗口互不混用。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=tuple(_CAPABILITIES), required=True)
    parser.add_argument("--venue", choices=("SSE", "SZSE"))
    parser.add_argument("--observation-date", type=date.fromisoformat)
    parser.add_argument("--etf", help="格式为 SSE.510300")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    add_source_approval_arguments(parser)
    arguments = parser.parse_args(argv)
    if arguments.operation == "master":
        if arguments.venue is None or arguments.observation_date is None:
            parser.error("master requires --venue and --observation-date")
    else:
        if arguments.etf is None or arguments.start is None or arguments.end is None:
            parser.error(f"{arguments.operation} requires --etf, --start and --end")
        require_window(arguments, parser)
    return arguments


def _build_market_approval(arguments: argparse.Namespace) -> EtfSourceApproval:
    """把个人内部研究来源批准映射为 ETF 日线和净值仓储的专用值对象。"""
    approval = build_source_approval(arguments, EtfReferenceSourceApproval)
    return EtfSourceApproval(
        provider_id=approval.provider_id,
        source_code=approval.source_code,
        legal_name=approval.legal_name,
        source_kind=approval.source_kind,
        rights_status=approval.rights_status,
        license_scope=approval.license_scope,
    )


def _result_payload(operation: str, result: object) -> dict[str, object]:
    """将四种 ETF 任务结果投影为统一且不泄露来源细节的 CLI 输出。"""
    etf = getattr(result, "etf", None)
    return {
        "operation": operation,
        "etf": etf.qualified_key if etf is not None else None,
        "dataVersion": (
            None
            if getattr(result, "data_version") is None  # noqa: B009
            else str(getattr(result, "data_version"))  # noqa: B009
        ),
        "insertedCount": getattr(result, "inserted_count"),  # noqa: B009
        "unchangedCount": getattr(result, "unchanged_count"),  # noqa: B009
        "availability": getattr(result, "availability", "available"),  # noqa: B009
    }


if __name__ == "__main__":
    raise SystemExit(main())
