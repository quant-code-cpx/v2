"""同步一个板块分类体系当前成分观测并尝试发布固定 release 的运维 CLI。"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import date, datetime
from zoneinfo import ZoneInfo

from service_data_sync.application.sector.membership_sync import SectorMembershipSyncService
from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.sector import SectorScheme
from service_data_sync.infrastructure.object_storage.raw_payload_store import S3RawPayloadStore
from service_data_sync.infrastructure.persistence.sector_membership_repository import (
    SqlAlchemySectorMembershipRepository,
)

_CAPABILITY = "sector.membership.snapshot.raw"
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def main(argv: Sequence[str] | None = None) -> int:
    """运行一个分类体系的完整成员观察；来源未获准或无完整 release 时返回非零退出码。"""
    arguments = _parse_args(argv)
    scheme = SectorScheme(arguments.scheme)
    observation_date = (
        date.fromisoformat(arguments.observation_date)
        if arguments.observation_date is not None
        else datetime.now(_SHANGHAI).date()
    )
    settings = load_settings()
    configure_logging(settings, process_role="sector-membership-cli")
    container = build_container(settings)
    try:
        sources = container.source_registry.for_capability(_CAPABILITY)
        if len(sources) != 1:
            raise SystemExit("exactly one approved sector-membership provider must be enabled")
        result = asyncio.run(
            SectorMembershipSyncService(
                source=sources[0],
                repository=SqlAlchemySectorMembershipRepository(container.database),
                raw_payload_store=S3RawPayloadStore(container.object_storage),
            ).sync_scheme(scheme=scheme, observation_date=observation_date)
        )
    finally:
        container.close()
    print(
        json.dumps(
            {
                "scheme": result.scheme.value,
                "items": [
                    {
                        "sector": item.identifier.code,
                        "snapshot_id": str(item.snapshot_id),
                        "complete": item.complete,
                        "pending_count": item.pending_count,
                        "quarantine_count": item.quarantine_count,
                    }
                    for item in result.items
                ],
                "failures": [
                    {"sector": failure.identifier.code, "code": failure.code.value}
                    for failure in result.failures
                ],
                "release": None
                if result.release is None
                else {
                    "release_id": str(result.release.release_id),
                    "data_version": str(result.release.data_version),
                    "quality_status": result.release.quality_status,
                    "fresh_sector_count": result.release.fresh_sector_count,
                    "carried_forward_sector_count": result.release.carried_forward_sector_count,
                },
            },
            separators=(",", ":"),
        )
    )
    return 0 if result.release is not None and not result.failures else 1


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """解析封闭分类体系与可复现观测日期，禁止一次任务混合两个 scheme。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheme", choices=tuple(SectorScheme), required=True)
    parser.add_argument("--observation-date", help="来源观察市场日，格式 YYYY-MM-DD")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
