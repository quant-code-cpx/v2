"""只读审计互联互通交付清单到当前完整包的全量覆盖，不访问任何外部数据源。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from uuid import UUID

from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.stock_connect_coverage_audit_repository import (
    SqlAlchemyStockConnectCoverageAuditRepository,
    StockConnectCoverageAudit,
    stock_connect_coverage_audit_view,
)

_ERROR_SCHEMA = "quant-v2.stock-connect-coverage-audit-error.v1"


def main(argv: Sequence[str] | None = None) -> int:
    """执行一次只读审计；完整通过返回 0，coverage 缺口返回 3，输入或依赖失败返回 2。"""
    arguments = _parse_args(argv)
    try:
        manifest_id, root_hash = _audit_identity(arguments)
    except ValueError:
        _print_error("COVERAGE_AUDIT_INPUT_INVALID")
        return 2
    try:
        audit = _run_audit(manifest_id=manifest_id, root_hash=root_hash)
    except Exception:
        # 顶层边界绝不打印数据库、文件路径或异常消息；受控运行器只依赖稳定退出码和原因码。
        _print_error("COVERAGE_AUDIT_UNAVAILABLE")
        return 2
    print(
        json.dumps(
            stock_connect_coverage_audit_view(audit),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if audit.passed else 3


def _run_audit(*, manifest_id: UUID, root_hash: str) -> StockConnectCoverageAudit:
    """仅组装 PostgreSQL 客户端和只读仓储，避免创建 provider、Redis 或对象存储客户端。"""
    database = DatabaseClient.from_settings(load_settings())
    try:
        return SqlAlchemyStockConnectCoverageAuditRepository(database).audit(
            manifest_id=manifest_id,
            root_hash=root_hash,
        )
    finally:
        database.close()


def _audit_identity(arguments: argparse.Namespace) -> tuple[UUID, str]:
    """校验不可变清单 UUID 与小写 SHA-256，错误输入不得触发数据库连接。"""
    manifest_id = UUID(arguments.manifest_id)
    root_hash = str(arguments.root_hash)
    if (
        len(root_hash) != 64
        or root_hash != root_hash.lower()
        or any(character not in "0123456789abcdef" for character in root_hash)
    ):
        raise ValueError("stock-connect coverage root hash is invalid")
    return manifest_id, root_hash


def _print_error(error_code: str) -> None:
    """向标准错误输出不含输入值和底层异常文本的稳定机器错误。"""
    print(
        json.dumps(
            {
                "schema": _ERROR_SCHEMA,
                "passed": False,
                "errorCode": error_code,
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=sys.stderr,
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """定义不可变数据库清单身份参数；状态边界只能来自持久化生产锁。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-id", required=True)
    parser.add_argument("--root-hash", required=True)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
