"""一键创建或恢复股票中心真实当前态引用 bundle。"""

from __future__ import annotations

import argparse
import json
import socket
from collections.abc import Sequence
from uuid import uuid4

from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.data_operations.canonical_executors import (
    register_canonical_executors,
)
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    build_catalog,
)
from service_data_sync.infrastructure.data_operations.equity_reference_bundle import (
    EquityReferenceBundleOrchestrator,
)


def main(argv: Sequence[str] | None = None) -> int:
    """执行完整引用刷新并输出可供下一阶段计划冻结的 bundle 身份。"""
    arguments = _parse_args(argv)
    settings = load_settings()
    configure_logging(settings, process_role="equity-reference-bundle-cli")
    container = build_container(settings)
    try:
        control_plane = DataOperationsControlPlane(
            database=container.database,
            catalog=build_catalog(settings, container.source_registry),
            source_registry=container.source_registry,
            trading_calendar=container.trading_calendar,
            etf_auto_retry_max_attempts=settings.etf_auto_retry_max_attempts,
        )
        register_canonical_executors(control_plane, container)
        bundle = EquityReferenceBundleOrchestrator(
            database=container.database,
            control_plane=control_plane,
            trading_calendar=container.trading_calendar,
        ).run_until_sealed(
            campaign_key=arguments.campaign_key,
            worker_id=f"{socket.gethostname()}:{uuid4()}",
            max_wait_seconds=arguments.max_wait_seconds,
        )
    finally:
        container.close()
    print(
        json.dumps(
            {
                "publicationId": str(bundle.publication_id),
                "dataVersion": str(bundle.data_version),
                "releaseId": str(bundle.release_id),
                "snapshotObservedOn": bundle.snapshot_observed_on.isoformat(),
                "marketAsOf": bundle.market_as_of.isoformat(),
                "manifestHash": bundle.manifest_hash,
                "componentCount": len(bundle.manifest),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """定义稳定批次键与总等待预算，重复运行会恢复同一数据库 attempt。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--campaign-key",
        required=True,
        help="稳定批次键；同日重复执行会恢复或重放同一封印结果",
    )
    parser.add_argument(
        "--max-wait-seconds",
        type=float,
        default=7200,
        help="本次进程等待七步完成的总秒数；超时后可用同一批次键恢复",
    )
    arguments = parser.parse_args(argv)
    if arguments.max_wait_seconds <= 0:
        parser.error("--max-wait-seconds must be positive")
    return arguments


if __name__ == "__main__":
    raise SystemExit(main())
