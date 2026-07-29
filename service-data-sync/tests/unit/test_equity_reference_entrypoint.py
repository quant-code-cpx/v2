"""个股复权因子、公司行动与概况 CLI 的单元测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from service_data_sync.domain.equity import EquityIdentifier
from service_data_sync.entrypoints import equity_reference


class FakeSourceRegistry:
    """为每个测试能力返回唯一已启用来源。"""

    def for_capability(self, capability: str) -> tuple[object, ...]:
        """返回不携带供应商实现细节的来源替身。"""
        del capability
        return (object(),)


class FakeContainer:
    """提供 CLI 所需的来源、数据库、对象存储和关闭状态。"""

    def __init__(self) -> None:
        """初始化最小容器依赖。"""
        self.source_registry = FakeSourceRegistry()
        self.database = object()
        self.object_storage = object()
        self.closed = False

    def close(self) -> None:
        """记录 CLI 已释放容器资源。"""
        self.closed = True


class FakeSyncService:
    """返回指定 capability 的稳定同步结果。"""

    def __init__(self, capability: str) -> None:
        """保存该服务模拟的能力。"""
        self._capability = capability

    async def sync(self, **kwargs: object) -> SimpleNamespace:
        """忽略基础设施参数并返回机器摘要所需字段。"""
        identifier = kwargs["identifier"]
        assert isinstance(identifier, EquityIdentifier)
        return SimpleNamespace(
            instrument=identifier,
            capability=self._capability,
            data_version=uuid4(),
            inserted_count=1,
            unchanged_count=0,
        )


@pytest.mark.parametrize(
    "capability,service_name",
    [
        ("equity.adjustment_factor", "EquityAdjustmentFactorSyncService"),
        ("equity.corporate_action", "EquityCorporateActionSyncService"),
        ("equity.profile", "EquityCompanyProfileSyncService"),
    ],
)
def test_reference_cli_runs_each_capability_and_closes_container(
    configured_environment: None,
    monkeypatch,
    capsys,
    capability: str,
    service_name: str,
) -> None:
    """三个参考数据能力均输出 publication 摘要并关闭容器。"""
    del configured_environment
    container = FakeContainer()
    monkeypatch.setattr(equity_reference, "configure_logging", Mock())
    monkeypatch.setattr(equity_reference, "build_container", Mock(return_value=container))
    monkeypatch.setattr(
        equity_reference,
        "SqlAlchemyEquityMarketDataRepository",
        Mock(return_value=object()),
    )
    monkeypatch.setattr(equity_reference, "S3RawPayloadStore", Mock(return_value=object()))
    # 入口组合测试不触及对象存储；留证语义由原始载荷存储的专用单元测试覆盖。
    monkeypatch.setattr(
        equity_reference, "retain_failure_evidence", lambda _store, operation: operation()
    )
    monkeypatch.setattr(
        equity_reference,
        service_name,
        Mock(return_value=FakeSyncService(capability)),
    )

    arguments = [
        "--instrument",
        "SSE.600519",
        "--capability",
        capability,
        "--end",
        "2026-07-28",
        "--full-history",
    ]
    assert equity_reference.main(arguments) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["instrument"] == "SSE.600519"
    assert payload["capability"] == capability
    assert payload["inserted_count"] == 1
    assert container.closed is True


def test_reference_cli_rejects_conflicting_or_reversed_windows() -> None:
    """CLI 在访问依赖前拒绝冲突回填参数和倒置日期窗口。"""
    with pytest.raises(SystemExit):
        equity_reference.main(
            [
                "--instrument",
                "SSE.600519",
                "--capability",
                "equity.profile",
                "--start",
                "2026-01-01",
                "--full-history",
            ]
        )
    with pytest.raises(SystemExit, match="start must not be after"):
        equity_reference.main(
            [
                "--instrument",
                "SSE.600519",
                "--capability",
                "equity.profile",
                "--start",
                "2026-07-28",
                "--end",
                "2026-01-01",
            ]
        )
