"""创建、封印并恢复股票中心真实全量历史回填。"""

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
from service_data_sync.infrastructure.data_operations.equity_backfill_orchestrator import (
    EquityBackfillOrchestrator,
)
from service_data_sync.infrastructure.data_operations.equity_reference_bundle import (
    EquityReferenceBundleOrchestrator,
)


def main(argv: Sequence[str] | None = None) -> int:
    """自动封印当前引用、创建历史计划并经 fencing 控制面推进至终态。"""
    arguments = _parse_args(argv)
    settings = load_settings()
    configure_logging(settings, process_role="equity-backfill-cli")
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
        worker_id = f"{socket.gethostname()}:{uuid4()}"
        reference_orchestrator = EquityReferenceBundleOrchestrator(
            database=container.database,
            control_plane=control_plane,
            trading_calendar=container.trading_calendar,
        )
        orchestrator = EquityBackfillOrchestrator(
            database=container.database,
            control_plane=control_plane,
            reference_bundle_orchestrator=reference_orchestrator,
        )
        plan_id = orchestrator.create_or_resume_plan(
            campaign_key=arguments.campaign_key,
            reference_bundle=None,
            worker_id=worker_id,
            instrument_scope=arguments.instrument,
            max_wait_seconds=arguments.max_wait_seconds,
        )
        summary = orchestrator.run_until_terminal(
            plan_id=plan_id,
            worker_id=worker_id,
            max_wait_seconds=arguments.max_wait_seconds,
            maximum_inflight_children=arguments.maximum_inflight_children,
        )
    finally:
        container.close()
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if summary["status"] == "SUCCEEDED" else 3


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """解析稳定批次键、总等待预算与有限并发，避免 CLI 绕开数据库恢复水位。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--campaign-key",
        required=True,
        help="稳定批次键；重复调用复用已封印历史计划，未创建时自动生成引用 bundle",
    )
    parser.add_argument(
        "--max-wait-seconds",
        type=float,
        default=86400,
        help="本进程总等待秒数；超时后使用相同批次键恢复",
    )
    parser.add_argument(
        "--maximum-inflight-children",
        type=int,
        default=16,
        help="同一阶段可同时受理的 child command 数，范围为 1 到 100",
    )
    parser.add_argument(
        "--instrument",
        type=_instrument_scope,
        default=None,
        metavar="EXCHANGE.SYMBOL",
        help=(
            "真实端到端烟测的单证券，例如 SSE.600519；仍生成完整引用 bundle，"
            "仅缩小后续历史 child roster，且自动使用独立 campaign key"
        ),
    )
    arguments = parser.parse_args(argv)
    if arguments.max_wait_seconds <= 0:
        parser.error("--max-wait-seconds must be positive")
    if not 1 <= arguments.maximum_inflight_children <= 100:
        parser.error("--maximum-inflight-children must be between 1 and 100")
    return arguments


def _instrument_scope(value: str) -> tuple[str, str]:
    """把 CLI 的精确证券文本解析为固定交易所和六位代码，拒绝模糊搜索值。"""
    exchange, separator, symbol = value.strip().upper().partition(".")
    if (
        separator != "."
        or exchange not in {"SSE", "SZSE", "BSE"}
        or len(symbol) != 6
        or not symbol.isascii()
        or not symbol.isdecimal()
    ):
        raise argparse.ArgumentTypeError(
            "--instrument 必须是 SSE.600519、SZSE.000001 或 BSE.XXXXXX"
        )
    return exchange, symbol


if __name__ == "__main__":
    raise SystemExit(main())
