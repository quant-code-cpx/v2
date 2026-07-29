"""执行一个经显式来源批准的真实衍生品合约日线同步。

调用方必须指定真实合约和有界业务日期；入口会先验证目录、来源和发布条件，不会用
连续合约、指数或其他品种替代缺失数据，避免对外宣称不存在的合约事实。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime

from service_data_sync.application.derivative.daily_bar_sync import DerivativeDailyBarSyncService
from service_data_sync.application.ports.data_source import DataSourcePort, ProviderError
from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.derivative import DerivativeContractIdentifier
from service_data_sync.entrypoints._p0 import P0UnavailableSyncResult, is_source_unavailable_error
from service_data_sync.infrastructure.object_storage.raw_payload_store import (
    FailureEvidenceDataSource,
    S3RawPayloadStore,
    retain_failure_evidence,
)
from service_data_sync.infrastructure.persistence.dataset_availability_repository import (
    SqlAlchemyDatasetAvailabilityRepository,
)
from service_data_sync.infrastructure.persistence.derivative_market_data_repository import (
    DerivativeSourceApproval,
    SqlAlchemyDerivativeDailyBarRepository,
)


def main(argv: Sequence[str] | None = None) -> int:
    """同步单一真实合约有限日期窗口；未注册来源时写空观测并正常结束。"""
    arguments = _parse_args(argv)
    settings = load_settings()
    configure_logging(settings, process_role="derivative-daily-bar-cli")
    container = build_container(settings)
    try:
        source = _select_source(
            sources=container.source_registry.for_capability("derivative.bar.1d.reported"),
            provider_id=arguments.provider_id,
        )
        contract = DerivativeContractIdentifier.parse(arguments.contract)
        raw_payload_store = S3RawPayloadStore(container.object_storage)
        availability_repository = SqlAlchemyDatasetAvailabilityRepository(container.database)
        if source is None:
            result = _unavailable_result(
                arguments=arguments,
                contract=contract,
                availability_repository=availability_repository,
            )
        else:
            approval = DerivativeSourceApproval(
                provider_id=arguments.provider_id,
                source_code=arguments.source_code,
                legal_name=arguments.source_legal_name,
                source_kind=arguments.source_kind,
                rights_status=arguments.rights_status,
                license_scope=arguments.license_scope,
            )
            try:
                result = retain_failure_evidence(
                    raw_payload_store,
                    # 失败时才归档 AKShare 来源字节，成功发布不保留副本。
                    lambda: asyncio.run(
                        DerivativeDailyBarSyncService(
                            source=FailureEvidenceDataSource(source, raw_payload_store),
                            repository=SqlAlchemyDerivativeDailyBarRepository(
                                container.database,
                                approved_sources={approval.provider_id: approval},
                            ),
                            raw_payload_store=raw_payload_store,
                        ).sync(
                            contract=contract,
                            start=arguments.start,
                            end=arguments.end,
                        )
                    ),
                )
                if result.availability == "empty":
                    _empty_result(
                        arguments=arguments,
                        contract=contract,
                        availability_repository=availability_repository,
                        provider_id=source.provider_id,
                    )
            except ProviderError as error:
                if not is_source_unavailable_error(error):
                    raise
                result = _unavailable_result(
                    arguments=arguments,
                    contract=contract,
                    availability_repository=availability_repository,
                    reason_code=error.code.value,
                )
    finally:
        container.close()
    print(
        json.dumps(
            {
                "contract": contract.qualified_key,
                "dataVersion": None if result.data_version is None else str(result.data_version),
                "insertedCount": result.inserted_count,
                "unchangedCount": result.unchanged_count,
                "availability": getattr(result, "availability", "available"),
            },
            separators=(",", ":"),
        )
    )
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """读取有界合约窗口和可覆盖的个人 AKShare 来源元数据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contract", required=True, help="格式为 VENUE.CONTRACT，例如 CFFEX.IF2608"
    )
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--provider-id", default="akshare")
    parser.add_argument("--source-code", default="akshare")
    parser.add_argument("--source-legal-name", default="AKShare")
    parser.add_argument("--source-kind", default="community_aggregator")
    parser.add_argument("--rights-status", default="personal_internal_research")
    parser.add_argument("--license-scope", default="internal_research_no_redistribution")
    arguments = parser.parse_args(argv)
    if arguments.start > arguments.end:
        parser.error("--start must not be after --end")
    return arguments


def _select_source(
    *, sources: tuple[DataSourcePort, ...], provider_id: str
) -> DataSourcePort | None:
    """只接受唯一精确 adapter；找不到或重复时由入口记录成功空观测。"""
    matched = tuple(source for source in sources if source.provider_id == provider_id)
    return matched[0] if len(matched) == 1 else None


def _unavailable_result(
    *,
    arguments: argparse.Namespace,
    contract: DerivativeContractIdentifier,
    availability_repository: SqlAlchemyDatasetAvailabilityRepository,
    reason_code: str = "provider_not_registered",
) -> P0UnavailableSyncResult:
    """记录未注册衍生品来源，使合约查询能稳定显示为空而不阻断其他域。"""
    availability_repository.record(
        dataset="derivative.bar.1d.reported",
        partition_key=f"{contract.qualified_key}:{arguments.start.isoformat()}:{arguments.end.isoformat()}",
        availability="source_unavailable",
        reason_code=reason_code,
        provider_id=arguments.provider_id,
        observed_at=datetime.now(UTC),
    )
    return P0UnavailableSyncResult()


def _empty_result(
    *,
    arguments: argparse.Namespace,
    contract: DerivativeContractIdentifier,
    availability_repository: SqlAlchemyDatasetAvailabilityRepository,
    provider_id: str,
) -> None:
    """记录来源成功响应但合约窗口无行情，避免把尚未上市误报为同步失败。"""
    availability_repository.record(
        dataset="derivative.bar.1d.reported",
        partition_key=f"{contract.qualified_key}:{arguments.start.isoformat()}:{arguments.end.isoformat()}",
        availability="empty",
        reason_code="no_matching_facts",
        provider_id=provider_id,
        observed_at=datetime.now(UTC),
    )


if __name__ == "__main__":
    raise SystemExit(main())
