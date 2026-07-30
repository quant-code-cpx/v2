"""互联互通受控回滚 CLI 的输入、审计与机器退出码测试。"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import Mock, patch
from uuid import UUID

import pytest

from service_data_sync.entrypoints import stock_connect_rollback
from service_data_sync.infrastructure.data_operations.stock_connect_rollback_operator import (
    StockConnectRollbackOperationRejected,
    StockConnectRollbackOperationResult,
)
from service_data_sync.infrastructure.persistence.stock_connect_rollback_repository import (
    RolledBackStockConnectBundle,
)

_OPERATION_ID = "10000000-0000-4000-8000-000000000001"
_TARGET_ID = "20000000-0000-4000-8000-000000000001"


def _arguments() -> list[str]:
    """返回含永久幂等键、精确目标和三项强制审计字段的完整参数。"""
    return [
        "--operation-id",
        _OPERATION_ID,
        "--channel",
        "SH_NORTHBOUND",
        "--trade-date",
        "2026-07-29",
        "--target-bundle-release-id",
        _TARGET_ID,
        "--actor-ref",
        "operator:stock-connect",
        "--reason",
        "生产完整包出现数据质量回归，回滚到已验证历史版本",
        "--request-id",
        "incident:stock-connect:20260729",
    ]


@pytest.mark.parametrize(
    "required_flag",
    ["--actor-ref", "--reason", "--request-id"],
)
def test_cli_requires_every_audit_field(required_flag: str) -> None:
    """缺 actor、原因或链路标识任一项时 argparse 必须以输入错误码 2 终止。"""
    arguments = _arguments()
    index = arguments.index(required_flag)
    del arguments[index : index + 2]

    with pytest.raises(SystemExit) as exited:
        stock_connect_rollback._parse_args(arguments)

    assert exited.value.code == 2


def test_cli_rejects_invalid_identity_before_database_connection(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """非法 operation UUID 必须返回 2，且不得创建数据库连接。"""
    arguments = _arguments()
    arguments[1] = "not-a-uuid"

    with patch.object(
        stock_connect_rollback.DatabaseClient,
        "from_settings",
    ) as connect:
        exit_code = stock_connect_rollback.main(arguments)

    assert exit_code == 2
    assert connect.call_count == 0
    assert json.loads(capsys.readouterr().err) == {
        "schema": "quant-v2.stock-connect-bundle-rollback-error.v1",
        "ok": False,
        "errorCode": "ROLLBACK_INPUT_INVALID",
    }


def test_cli_returns_stable_business_rejection_exit_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """历史目标不满足 fail-closed 条件时返回 3，且始终关闭数据库。"""
    database = Mock()
    operator = Mock()
    operator.execute.side_effect = StockConnectRollbackOperationRejected(
        "rollback-target-incomplete",
        "target is incomplete",
    )

    with (
        patch.object(stock_connect_rollback, "load_settings", return_value=object()),
        patch.object(
            stock_connect_rollback.DatabaseClient,
            "from_settings",
            return_value=database,
        ),
        patch.object(
            stock_connect_rollback,
            "StockConnectRollbackOperator",
            return_value=operator,
        ),
    ):
        exit_code = stock_connect_rollback.main(_arguments())

    assert exit_code == 3
    database.close.assert_called_once_with()
    assert json.loads(capsys.readouterr().err)["errorCode"] == ("rollback-target-incomplete")


def test_cli_outputs_atomic_operation_and_publication_identities(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """成功返回 0，并同时输出 operation/run、bundle、overview 与重放语义。"""
    database = Mock()
    operator = Mock()
    operator.execute.return_value = StockConnectRollbackOperationResult(
        operation_id=UUID(_OPERATION_ID),
        run_id=UUID("30000000-0000-4000-8000-000000000001"),
        channel="SH_NORTHBOUND",
        trade_date=date(2026, 7, 29),
        rollback=RolledBackStockConnectBundle(
            rollback_id=UUID("40000000-0000-4000-8000-000000000001"),
            from_bundle_release_id=UUID("50000000-0000-4000-8000-000000000001"),
            to_bundle_release_id=UUID(_TARGET_ID),
            target_data_version="stock-connect:historical:verified",
            overview_release_ids=(
                (
                    "SH_NORTHBOUND",
                    UUID("60000000-0000-4000-8000-000000000001"),
                ),
            ),
            reused=False,
        ),
    )

    with (
        patch.object(stock_connect_rollback, "load_settings", return_value=object()),
        patch.object(
            stock_connect_rollback.DatabaseClient,
            "from_settings",
            return_value=database,
        ),
        patch.object(
            stock_connect_rollback,
            "StockConnectRollbackOperator",
            return_value=operator,
        ),
    ):
        exit_code = stock_connect_rollback.main(_arguments())

    body = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert body["schema"] == "quant-v2.stock-connect-bundle-rollback-result.v1"
    assert body["operationId"] == _OPERATION_ID
    assert body["channel"] == "SH_NORTHBOUND"
    assert body["tradeDate"] == "2026-07-29"
    assert body["toBundleReleaseId"] == _TARGET_ID
    assert body["overviewReleases"] == [
        {
            "channelSet": "SH_NORTHBOUND",
            "overviewReleaseId": "60000000-0000-4000-8000-000000000001",
        }
    ]
    assert body["reused"] is False
    database.close.assert_called_once_with()
