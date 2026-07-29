"""衍生品真实合约日线受控 CLI 的参数与来源选择测试。"""

from __future__ import annotations

from typing import cast

import pytest

from service_data_sync.application.ports.data_source import DataSourcePort
from service_data_sync.entrypoints.derivative_daily_bars import _parse_args, _select_source


class FakeSource:
    """提供最小 adapter 身份，用于验证 CLI 不会按数组顺序静默选取来源。"""

    def __init__(self, provider_id: str) -> None:
        """保存测试来源 ID。"""
        self.provider_id = provider_id

    def capabilities(self) -> frozenset[str]:
        """CLI 测试不触发真实请求，仅声明空能力集合。"""
        return frozenset()

    async def fetch(self, request: object) -> object:
        """任何网络调用都意味着 CLI 边界测试失败。"""
        del request
        raise AssertionError("unexpected provider fetch")


def test_cli_requires_explicit_source_approval_and_bounded_window() -> None:
    """没有来源权利字段或日期倒置时，任务必须在构造 adapter 前退出。"""
    with pytest.raises(SystemExit):
        _parse_args(["--contract", "CFFEX.IF2608", "--start", "2026-07-29", "--end", "2026-07-28"])


def test_cli_selects_only_one_exact_provider_or_returns_empty_path() -> None:
    """同名重复或没有对应来源时不按顺序 fallback，入口可改写为空状态。"""
    selected = _select_source(
        sources=(cast(DataSourcePort, FakeSource("approved")),), provider_id="approved"
    )

    assert selected is not None
    assert selected.provider_id == "approved"
    assert (
        _select_source(
            sources=(
                cast(DataSourcePort, FakeSource("approved")),
                cast(DataSourcePort, FakeSource("approved")),
            ),
            provider_id="approved",
        )
        is None
    )
