"""执行一个指数目录、当前成分或权重的受控研究态影子同步。"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime

from service_data_sync.application.index.shadow_sync import (
    IndexShadowSyncResult,
    IndexShadowSyncService,
)
from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderError,
    ProviderErrorCode,
)
from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.index import IndexAdministrator, IndexCapability, IndexIdentifier
from service_data_sync.infrastructure.object_storage.raw_payload_store import (
    FailureEvidenceDataSource,
    S3RawPayloadStore,
    retain_failure_evidence,
)
from service_data_sync.infrastructure.persistence.dataset_availability_repository import (
    SqlAlchemyDatasetAvailabilityRepository,
)
from service_data_sync.infrastructure.persistence.index_shadow_repository import (
    SqlAlchemyIndexShadowRepository,
)

_PROVIDER_BY_ADMINISTRATOR = {
    IndexAdministrator.CSI: "akshare-csindex-index-snapshot",
    IndexAdministrator.CNI: "akshare-cnindex-index-snapshot",
}


def main(argv: Sequence[str] | None = None) -> int:
    """运行一个显式白名单指数观察，不推断生效日期、证券身份或正式发布。"""
    arguments = _parse_args(argv)
    administrator = IndexAdministrator(arguments.administrator)
    capability = IndexCapability(arguments.capability)
    identifier = (
        None
        if capability is IndexCapability.CATALOG_SNAPSHOT
        else IndexIdentifier(administrator, arguments.index_code)
    )
    settings = load_settings()
    configure_logging(settings, process_role="index-shadow-cli")
    container = build_container(settings)
    try:
        source = _select_source(
            sources=container.source_registry.for_capability(capability.value),
            administrator=administrator,
        )
        availability_repository = SqlAlchemyDatasetAvailabilityRepository(container.database)
        if source is None:
            payload = _unavailable_payload(
                administrator=administrator,
                capability=capability,
                identifier=identifier,
                availability_repository=availability_repository,
                reason_code="provider_not_registered",
            )
        else:
            raw_payload_store = S3RawPayloadStore(container.object_storage)
            service = IndexShadowSyncService(
                source=FailureEvidenceDataSource(source, raw_payload_store),
                repository=SqlAlchemyIndexShadowRepository(container.database),
                raw_payload_store=raw_payload_store,
            )
            try:
                result = retain_failure_evidence(
                    raw_payload_store,
                    # 同一执行边界仅在同步异常时将暂存来源字节固化为排障证据。
                    lambda: asyncio.run(
                        service.sync_catalog(administrator=administrator)
                        if identifier is None
                        else service.sync_snapshot(identifier=identifier, capability=capability)
                    ),
                )
            except ProviderError as error:
                if error.code not in {
                    ProviderErrorCode.UNAVAILABLE,
                    ProviderErrorCode.RATE_LIMITED,
                    ProviderErrorCode.AUTHENTICATION,
                    ProviderErrorCode.INVALID_REQUEST,
                }:
                    raise
                payload = _unavailable_payload(
                    administrator=administrator,
                    capability=capability,
                    identifier=identifier,
                    availability_repository=availability_repository,
                    reason_code=error.code.value,
                    provider_id=source.provider_id,
                )
            else:
                payload = _render_result(result, administrator)
    finally:
        container.close()
    print(json.dumps(payload, separators=(",", ":")))
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """解析单管理人、单能力和单指数参数，禁止一个运行混合多个观察分区。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--administrator", choices=tuple(IndexAdministrator), required=True)
    parser.add_argument(
        "--capability",
        choices=(
            IndexCapability.CATALOG_SNAPSHOT.value,
            IndexCapability.CONSTITUENT_SNAPSHOT.value,
            IndexCapability.WEIGHT_SNAPSHOT.value,
        ),
        required=True,
    )
    parser.add_argument("--index-code")
    arguments = parser.parse_args(argv)
    needs_index = arguments.capability != IndexCapability.CATALOG_SNAPSHOT.value
    if needs_index != (arguments.index_code is not None):
        parser.error("--index-code is required only for constituent or weight snapshots")
    return arguments


def _select_source(
    *, sources: tuple[DataSourcePort, ...], administrator: IndexAdministrator
) -> DataSourcePort | None:
    """仅选择管理人唯一 adapter；缺失或歧义时记录成功空状态而不静默回退。"""
    provider_id = _PROVIDER_BY_ADMINISTRATOR[administrator]
    selected = tuple(source for source in sources if source.provider_id == provider_id)
    return selected[0] if len(selected) == 1 else None


def _unavailable_payload(
    *,
    administrator: IndexAdministrator,
    capability: IndexCapability,
    identifier: IndexIdentifier | None,
    availability_repository: SqlAlchemyDatasetAvailabilityRepository,
    reason_code: str,
    provider_id: str | None = None,
) -> dict[str, object]:
    """记录指数来源不可用并输出研究态空结果，不把观察能力缺失伪装成正式发布。"""
    partition_key = (
        f"{administrator.value}:{identifier.code if identifier is not None else 'catalog'}"
    )
    availability_repository.record(
        dataset=capability.value,
        partition_key=partition_key,
        availability="source_unavailable",
        reason_code=reason_code,
        provider_id=provider_id or _PROVIDER_BY_ADMINISTRATOR[administrator],
        observed_at=datetime.now(UTC),
    )
    return {
        "administrator": administrator.value,
        "capability": capability.value,
        "observation_id": None,
        "item_count": 0,
        "quality_status": "SOURCE_UNAVAILABLE",
        "publication_created": False,
        "availability": "source_unavailable",
    }


def _render_result(
    result: IndexShadowSyncResult, administrator: IndexAdministrator
) -> dict[str, object]:
    """渲染不含供应商载荷、数据库键或发布版本的机器可读研究态摘要。"""
    return {
        "administrator": administrator.value,
        "capability": result.capability,
        "observation_id": str(result.observation.observation_id),
        "item_count": result.observation.item_count,
        "quality_status": result.observation.quality_status,
        "publication_created": False,
    }


if __name__ == "__main__":
    raise SystemExit(main())
