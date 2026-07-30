"""ETF、两融、港通和事件旧 CLI 的安全停用测试。"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pytest

from service_data_sync.application.legacy_entrypoints import LEGACY_ENTRYPOINT_UNAVAILABLE
from service_data_sync.entrypoints import (
    corporate_events,
    etf,
    margin,
    stock_connect,
    trading_events,
)


@pytest.mark.parametrize(
    ("main", "entrypoint"),
    (
        (etf.main, "data-sync-etf"),
        (margin.main, "data-sync-margin"),
        (stock_connect.main, "data-sync-stock-connect"),
        (corporate_events.main, "data-sync-corporate-events"),
        (trading_events.main, "data-sync-trading-events"),
    ),
)
def test_p0_cli_rejects_before_creating_dependencies(
    main: Callable[[Sequence[str] | None], int], entrypoint: str
) -> None:
    """所有 P0 旧 CLI 必须使用同一稳定错误，不再解析自由来源或执行参数。"""
    with pytest.raises(SystemExit) as error:
        main(["--legacy-argument", "value"])
    assert str(error.value) == f"{LEGACY_ENTRYPOINT_UNAVAILABLE}: {entrypoint}"
