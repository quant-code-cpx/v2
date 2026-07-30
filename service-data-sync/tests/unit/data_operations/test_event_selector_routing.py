"""事件数据集全市场与单证券 selector 路由回归测试。"""

from __future__ import annotations

import pytest

from service_data_sync.infrastructure.data_operations.canonical_executors import (
    _event_identifier,
)


def test_trading_event_selector_resolves_to_whole_market_without_fake_identifier() -> None:
    """龙虎榜和大宗交易全市场 selector 必须解析为空身份并交给真实批量来源。"""
    assert (
        _event_identifier(
            {"kind": "TRADING_EVENT", "operation": "DRAGON_TIGER"},
            allow_global=False,
        )
        is None
    )


def test_corporate_event_global_selector_remains_separate() -> None:
    """业绩事件只接受其目录声明的 `GLOBAL`，不能误收交易事件 operation。"""
    assert _event_identifier({"kind": "GLOBAL"}, allow_global=True) is None
    with pytest.raises(ValueError, match="does not match dataset"):
        _event_identifier(
            {"kind": "TRADING_EVENT", "operation": "BLOCK_TRADE"},
            allow_global=True,
        )
