"""只读校验互联互通官方交付清单，不访问数据库、网络或业务同步入口。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from service_data_sync.infrastructure.providers.official.stock_connect import (
    StockConnectManifestValidation,
    calculate_stock_connect_sftp_manifest_root,
    validate_stock_connect_calendar_manifest,
    validate_stock_connect_master_profile_manifest,
    validate_stock_connect_sftp_delivery_manifest,
    validate_stock_connect_status_manifest,
)

_DEFAULT_MAX_BYTES = 256 * 1024
_MASTER_SHA_ENV = "DATA_SYNC_HKEX_SECURITIES_MASTER_PROFILE_MANIFEST_SHA256"


def main(argv: Sequence[str] | None = None) -> int:
    """执行一个离线校验命令，并只输出不含路径、订单号和凭据的机器摘要。"""
    arguments = _parse_args(argv)
    try:
        summaries = _execute(arguments)
    except (OSError, TypeError, ValueError):
        print(
            json.dumps(
                {
                    "ok": False,
                    "command": arguments.command,
                    "errorCode": "MANIFEST_INVALID",
                },
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "command": arguments.command,
                "manifests": [_validation_view(item) for item in summaries],
            },
            separators=(",", ":"),
        )
    )
    return 0


def _execute(arguments: argparse.Namespace) -> tuple[StockConnectManifestValidation, ...]:
    """按子命令调用纯本地 validator；calculate-root 也不读取远端对象。"""
    max_bytes = int(arguments.max_bytes)
    if arguments.command == "validate-calendar":
        return (
            validate_stock_connect_calendar_manifest(
                Path(arguments.path),
                max_bytes=max_bytes,
            ),
        )
    if arguments.command == "validate-sftp":
        return (
            validate_stock_connect_sftp_delivery_manifest(
                Path(arguments.path),
                max_bytes_per_page=max_bytes,
            ),
        )
    if arguments.command == "validate-status":
        return (
            validate_stock_connect_status_manifest(
                Path(arguments.path),
                required_from=date.fromisoformat(arguments.required_from),
                max_bytes=max_bytes,
            ),
        )
    if arguments.command == "validate-master":
        expected_sha256 = _master_sha256(arguments.master_sha256)
        return (
            validate_stock_connect_master_profile_manifest(
                Path(arguments.path),
                expected_sha256=expected_sha256,
                max_bytes=max_bytes,
            ),
        )
    if arguments.command == "calculate-sftp-root":
        root_hash = calculate_stock_connect_sftp_manifest_root(
            Path(arguments.path),
            max_bytes=max_bytes,
        )
        return (
            StockConnectManifestValidation(
                manifest_kind="sftp-delivery-root",
                sha256=root_hash,
                entry_count=0,
                root_hash=root_hash,
            ),
        )
    if arguments.command == "validate-all":
        expected_sha256 = _master_sha256(arguments.master_sha256)
        return (
            validate_stock_connect_calendar_manifest(
                Path(arguments.calendar),
                max_bytes=max_bytes,
            ),
            validate_stock_connect_sftp_delivery_manifest(
                Path(arguments.sftp),
                max_bytes_per_page=max_bytes,
            ),
            validate_stock_connect_status_manifest(
                Path(arguments.status),
                required_from=date.fromisoformat(arguments.status_required_from),
                max_bytes=max_bytes,
            ),
            validate_stock_connect_master_profile_manifest(
                Path(arguments.master),
                expected_sha256=expected_sha256,
                max_bytes=max_bytes,
            ),
        )
    raise ValueError("unknown stock-connect manifest command")


def _master_sha256(argument: str | None) -> str:
    """从显式参数或既有环境变量读取 licensed profile 摘要，不输出其来源。"""
    value = argument or os.environ.get(_MASTER_SHA_ENV)
    if value is None:
        raise ValueError("master profile manifest SHA-256 is required")
    return value


def _validation_view(item: StockConnectManifestValidation) -> dict[str, object]:
    """把 validator 结果投影为不含本地或远端路径的稳定 JSON。"""
    return {
        "kind": item.manifest_kind,
        "sha256": item.sha256,
        "entryCount": item.entry_count,
        "rootHash": item.root_hash,
        "requiredFrom": item.required_from.isoformat() if item.required_from is not None else None,
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """定义四类清单独立校验、统一校验和 SFTP canonical root 计算参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    calendar = subparsers.add_parser("validate-calendar")
    _add_path_and_limit(calendar)

    sftp = subparsers.add_parser("validate-sftp")
    _add_path_and_limit(sftp)

    status = subparsers.add_parser("validate-status")
    _add_path_and_limit(status)
    status.add_argument("--required-from", required=True)

    master = subparsers.add_parser("validate-master")
    _add_path_and_limit(master)
    master.add_argument("--master-sha256")

    calculate_root = subparsers.add_parser("calculate-sftp-root")
    _add_path_and_limit(calculate_root)

    validate_all = subparsers.add_parser("validate-all")
    validate_all.add_argument("--calendar", required=True)
    validate_all.add_argument("--sftp", required=True)
    validate_all.add_argument("--status", required=True)
    validate_all.add_argument("--status-required-from", required=True)
    validate_all.add_argument("--master", required=True)
    validate_all.add_argument("--master-sha256")
    validate_all.add_argument("--max-bytes", type=int, default=_DEFAULT_MAX_BYTES)
    return parser.parse_args(argv)


def _add_path_and_limit(parser: argparse.ArgumentParser) -> None:
    """为单清单命令添加本地路径和有界读取上限。"""
    parser.add_argument("--path", required=True)
    parser.add_argument("--max-bytes", type=int, default=_DEFAULT_MAX_BYTES)


if __name__ == "__main__":
    raise SystemExit(main())
