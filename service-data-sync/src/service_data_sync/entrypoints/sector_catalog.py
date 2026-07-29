"""用于同步一个行业或概念板块目录快照的受控运维 CLI。

完整目录快照会把带名称的板块身份提升为 `ACTIVE`；行情先建的 `PENDING` 身份保留
稳定 UUID 后再升级，避免名称变更或不同分类体系造成错误合并。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence

from service_data_sync.application.sector.catalog_sync import SectorCatalogSyncService
from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.sector import SectorScheme
from service_data_sync.infrastructure.object_storage.raw_payload_store import (
    FailureEvidenceDataSource,
    S3RawPayloadStore,
    retain_failure_evidence,
)
from service_data_sync.infrastructure.persistence.sector_market_data_repository import (
    SqlAlchemySectorMarketDataRepository,
)


def main(argv: Sequence[str] | None = None) -> int:
    """同步一个明确的分类体系目录，不推断成员关系或历史有效期。"""
    arguments = _parse_args(argv)
    scheme = SectorScheme(arguments.scheme)
    settings = load_settings()
    configure_logging(settings, process_role="sector-catalog-cli")
    container = build_container(settings)
    try:
        sources = container.source_registry.for_capability(scheme.catalog_capability)
        # 一个目录快照只能归属一个获准来源，避免公开身份名称的血缘含糊。
        if len(sources) != 1:
            raise SystemExit("exactly one approved sector-catalog provider must be enabled")
        raw_payload_store = S3RawPayloadStore(container.object_storage)
        result = retain_failure_evidence(
            raw_payload_store,
            # 同一执行边界仅在同步异常时将暂存来源字节固化为排障证据。
            lambda: asyncio.run(
                SectorCatalogSyncService(
                    source=FailureEvidenceDataSource(sources[0], raw_payload_store),
                    repository=SqlAlchemySectorMarketDataRepository(container.database),
                    raw_payload_store=raw_payload_store,
                ).sync(scheme=scheme)
            ),
        )
    finally:
        container.close()
    print(
        json.dumps(
            {
                "sector_scheme": result.scheme.value,
                "data_version": str(result.data_version),
                "inserted_count": result.inserted_count,
                "unchanged_count": result.unchanged_count,
            },
            separators=(",", ":"),
        )
    )
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """解析封闭分类体系参数，禁止一次任务混合行业和概念目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheme", choices=tuple(SectorScheme), required=True)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
