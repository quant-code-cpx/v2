"""用于同步一个板块日、周或月上游原生行情窗口的有界运维 CLI。

行业和概念分类体系、板块身份、周期与日期窗口均须显式指定；日/周/月调用三条独立
上游接口和物理表，防止把日线派生结果误标为供应商原生周期行情。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import date

from service_data_sync.application.sector.bar_sync import SectorBarSyncService
from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.sector import SectorIdentifier, SectorPeriod, SectorScheme
from service_data_sync.infrastructure.object_storage.raw_payload_store import (
    FailureEvidenceDataSource,
    S3RawPayloadStore,
    retain_failure_evidence,
)
from service_data_sync.infrastructure.persistence.sector_market_data_repository import (
    SqlAlchemySectorMarketDataRepository,
)


def main(argv: Sequence[str] | None = None) -> int:
    """同步一个明确板块、周期和日期窗口，拒绝无界或日线派生请求。"""
    arguments = _parse_args(argv)
    identifier = SectorIdentifier(
        scheme=SectorScheme(arguments.scheme),
        code=arguments.sector,
    )
    period = SectorPeriod(arguments.period)
    start = date.fromisoformat(arguments.start)
    end = date.fromisoformat(arguments.end)
    if start > end:
        raise SystemExit("--start must not be after --end")
    settings = load_settings()
    configure_logging(settings, process_role="sector-bars-cli")
    container = build_container(settings)
    try:
        sources = container.source_registry.for_capability(period.capability)
        # 一个周期窗口只能归属一个获准来源，避免发布版本血缘含糊。
        if len(sources) != 1:
            raise SystemExit("exactly one approved sector-bar provider must be enabled")
        raw_payload_store = S3RawPayloadStore(container.object_storage)
        result = retain_failure_evidence(
            raw_payload_store,
            # 同一执行边界仅在同步异常时将暂存来源字节固化为排障证据。
            lambda: asyncio.run(
                SectorBarSyncService(
                    source=FailureEvidenceDataSource(sources[0], raw_payload_store),
                    repository=SqlAlchemySectorMarketDataRepository(container.database),
                    raw_payload_store=raw_payload_store,
                ).sync(identifier=identifier, period=period, start=start, end=end)
            ),
        )
    finally:
        container.close()
    print(
        json.dumps(
            {
                "sector_scheme": result.sector.scheme.value,
                "sector": result.sector.code,
                "period": result.period.value,
                "data_version": str(result.data_version),
                "inserted_count": result.inserted_count,
                "unchanged_count": result.unchanged_count,
            },
            separators=(",", ":"),
        )
    )
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """解析必填范围与封闭枚举，避免错误的默认全历史拉取。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheme", choices=tuple(SectorScheme), required=True)
    parser.add_argument("--sector", required=True, help="分类体系内的稳定板块代码，例如 BK0475")
    parser.add_argument("--period", choices=tuple(SectorPeriod), required=True)
    parser.add_argument("--start", required=True, help="包含端 ISO 日期")
    parser.add_argument("--end", required=True, help="包含端 ISO 日期")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
