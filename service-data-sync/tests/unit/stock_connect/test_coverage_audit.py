"""互联互通全量 coverage 集合审计与只读 CLI 的单元测试。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date
from uuid import UUID

import pytest

from service_data_sync.entrypoints import stock_connect_coverage_audit as entrypoint
from service_data_sync.infrastructure.persistence.stock_connect_coverage_audit_repository import (
    StockConnectCoverageAudit,
    audit_stock_connect_coverage_sets,
    stock_connect_coverage_audit_view,
)

_MANIFEST_ID = UUID("10000000-0000-4000-8000-000000000001")
_ROOT_HASH = "a" * 64
_EXPECTED = (
    (date(2026, 7, 28), "SH", "NORTHBOUND"),
    (date(2026, 7, 28), "SH", "SOUTHBOUND"),
    (date(2026, 7, 29), "SZ", "NORTHBOUND"),
)
_STAGES = ("entitlement", "object", "status", "market", "active", "bundle")


def test_coverage_audit_passes_only_when_every_required_stage_is_complete() -> None:
    """六个阶段全集一致时才通过，并输出包含状态边界的稳定机器视图。"""
    audit = _audit(
        observations={stage: _EXPECTED for stage in _STAGES},
        status_required_from=date(2026, 7, 28),
    )

    assert audit.passed is True
    assert audit.expected_count == 3
    assert all(stage.missing_count == 0 for stage in audit.stages.values())
    assert all(stage.published_count == 3 for stage in audit.stages.values())
    assert stock_connect_coverage_audit_view(audit) == {
        "schema": "quant-v2.stock-connect-coverage-audit.v1",
        "manifestId": str(_MANIFEST_ID),
        "rootHash": _ROOT_HASH,
        "minimumTradeDate": "2026-07-28",
        "maximumTradeDate": "2026-07-29",
        "expectedCount": 3,
        "statusRequiredFrom": "2026-07-28",
        "statusHistoricalWarningCount": 0,
        "stages": {
            stage: {
                "expectedCount": 3,
                "publishedCount": 3,
                "missingCount": 0,
                "duplicateCount": 0,
                "outOfRangeCount": 0,
                "gaps": [],
            }
            for stage in _STAGES
        },
        "passed": True,
    }


def test_coverage_audit_reports_missing_duplicate_and_out_of_range_independently() -> None:
    """缺件、重复和窗内越界必须分别计数，不能被集合去重掩盖。"""
    unexpected = (date(2026, 7, 30), "SH", "NORTHBOUND")
    observations: dict[str, Sequence[tuple[date, str, str]]] = {
        stage: _EXPECTED for stage in _STAGES
    }
    observations["entitlement"] = _EXPECTED[:-1]
    observations["object"] = (*_EXPECTED, _EXPECTED[0], unexpected)

    audit = _audit(
        observations=observations,
        status_required_from=date(2026, 7, 28),
    )

    assert audit.passed is False
    assert audit.stages["entitlement"].missing_count == 1
    assert audit.stages["entitlement"].gaps == ("2026-07-29:SZ:NORTHBOUND",)
    assert audit.stages["object"].published_count == 3
    assert audit.stages["object"].duplicate_count == 1
    assert audit.stages["object"].out_of_range_count == 1


def test_status_before_persistent_boundary_is_warning_without_weakening_other_stages() -> None:
    """边界前状态缺源只告警；边界后状态和其他阶段仍必须全量齐备。"""
    observations: dict[str, Sequence[tuple[date, str, str]]] = {
        stage: _EXPECTED for stage in _STAGES
    }
    observations["status"] = (_EXPECTED[-1],)

    audit = _audit(
        observations=observations,
        status_required_from=date(2026, 7, 29),
    )

    assert audit.passed is True
    assert audit.status_historical_warning_count == 2
    assert audit.stages["status"].expected_count == 1
    assert audit.stages["status"].published_count == 1
    assert audit.stages["status"].missing_count == 0
    assert audit.stages["market"].expected_count == 3


def test_coverage_cli_returns_gap_exit_code_with_machine_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI 对完整性缺口返回 3，并保留可供部署脚本读取的审计正文。"""
    observations: dict[str, Sequence[tuple[date, str, str]]] = {
        stage: _EXPECTED for stage in _STAGES
    }
    observations["bundle"] = _EXPECTED[:-1]
    audit = _audit(
        observations=observations,
        status_required_from=date(2026, 7, 28),
    )

    def run_audit(*, manifest_id: UUID, root_hash: str) -> StockConnectCoverageAudit:
        """确认 CLI 传递不可变清单身份并返回预置缺口。"""
        assert manifest_id == _MANIFEST_ID
        assert root_hash == _ROOT_HASH
        return audit

    monkeypatch.setattr(entrypoint, "_run_audit", run_audit)

    exit_code = entrypoint.main(["--manifest-id", str(_MANIFEST_ID), "--root-hash", _ROOT_HASH])
    captured = capsys.readouterr()

    assert exit_code == 3
    assert captured.err == ""
    assert json.loads(captured.out)["stages"]["bundle"]["missingCount"] == 1


def test_coverage_cli_rejects_bad_identity_without_database_and_redacts_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """无效身份不得连接数据库；运行失败不得把底层敏感异常写入输出。"""
    calls: list[tuple[UUID, str]] = []

    def run_audit(*, manifest_id: UUID, root_hash: str) -> StockConnectCoverageAudit:
        """记录意外数据库路径；合法输入分支则模拟含敏感文本的依赖异常。"""
        calls.append((manifest_id, root_hash))
        raise RuntimeError("postgresql://sensitive-user:sensitive-password@private-host/database")

    monkeypatch.setattr(entrypoint, "_run_audit", run_audit)

    invalid_exit = entrypoint.main(["--manifest-id", "not-a-uuid", "--root-hash", _ROOT_HASH])
    invalid_output = capsys.readouterr()
    unavailable_exit = entrypoint.main(
        ["--manifest-id", str(_MANIFEST_ID), "--root-hash", _ROOT_HASH]
    )
    unavailable_output = capsys.readouterr()

    assert invalid_exit == 2
    assert calls == [(_MANIFEST_ID, _ROOT_HASH)]
    assert json.loads(invalid_output.err)["errorCode"] == "COVERAGE_AUDIT_INPUT_INVALID"
    assert unavailable_exit == 2
    assert "sensitive" not in unavailable_output.err
    assert json.loads(unavailable_output.err)["errorCode"] == "COVERAGE_AUDIT_UNAVAILABLE"


def _audit(
    *,
    observations: Mapping[str, Sequence[tuple[date, str, str]]],
    status_required_from: date,
) -> StockConnectCoverageAudit:
    """用固定 manifest 身份执行纯集合审计，隔离数据库和 provider。"""
    return audit_stock_connect_coverage_sets(
        manifest_id=_MANIFEST_ID,
        root_hash=_ROOT_HASH,
        expected=_EXPECTED,
        observations=observations,
        status_required_from=status_required_from,
    )
