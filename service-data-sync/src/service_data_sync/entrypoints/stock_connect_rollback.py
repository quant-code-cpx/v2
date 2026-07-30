"""执行受全局 fencing 保护的互联互通历史完整包回滚。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from uuid import UUID

from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.stock_connect import StockConnectChannel
from service_data_sync.infrastructure.data_operations.stock_connect_rollback_operator import (
    StockConnectRollbackOperation,
    StockConnectRollbackOperationRejected,
    StockConnectRollbackOperator,
    stock_connect_rollback_result_view,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient

_ERROR_SCHEMA = "quant-v2.stock-connect-bundle-rollback-error.v1"
_CHANNELS = {
    "SH_NORTHBOUND": StockConnectChannel("SH", "NORTHBOUND"),
    "SZ_NORTHBOUND": StockConnectChannel("SZ", "NORTHBOUND"),
    "SH_SOUTHBOUND": StockConnectChannel("SH", "SOUTHBOUND"),
    "SZ_SOUTHBOUND": StockConnectChannel("SZ", "SOUTHBOUND"),
}


def main(argv: Sequence[str] | None = None) -> int:
    """执行或重放一次幂等回滚；成功 0、业务拒绝 3、输入或依赖失败 2。"""
    try:
        operation = _operation(_parse_args(argv))
    except (TypeError, ValueError):
        _print_error("ROLLBACK_INPUT_INVALID")
        return 2
    database: DatabaseClient | None = None
    try:
        database = DatabaseClient.from_settings(load_settings())
        result = StockConnectRollbackOperator(database).execute(operation)
    except StockConnectRollbackOperationRejected as error:
        _print_error(error.code)
        return 3
    except Exception:
        # 顶层只输出稳定机器码，禁止数据库、目标 UUID 或底层异常进入运维日志。
        _print_error("ROLLBACK_UNAVAILABLE")
        return 2
    finally:
        if database is not None:
            database.close()
    print(
        json.dumps(
            stock_connect_rollback_result_view(result),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _operation(arguments: argparse.Namespace) -> StockConnectRollbackOperation:
    """把 CLI 字符串解析为强类型、永久幂等的运维操作。"""
    return StockConnectRollbackOperation(
        operation_id=UUID(arguments.operation_id),
        channel=_CHANNELS[arguments.channel],
        trade_date=date.fromisoformat(arguments.trade_date),
        target_bundle_release_id=UUID(arguments.target_bundle_release_id),
        actor_ref=str(arguments.actor_ref),
        reason=str(arguments.reason),
        request_id=str(arguments.request_id),
    )


def _print_error(error_code: str) -> None:
    """输出不含参数值、数据库或 publication 细节的稳定失败对象。"""
    print(
        json.dumps(
            {
                "schema": _ERROR_SCHEMA,
                "ok": False,
                "errorCode": error_code,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=sys.stderr,
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """定义回滚目标、幂等键和强制审计字段；不提供隐式 latest。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--channel", choices=tuple(_CHANNELS), required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--target-bundle-release-id", required=True)
    parser.add_argument("--actor-ref", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--request-id", required=True)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
