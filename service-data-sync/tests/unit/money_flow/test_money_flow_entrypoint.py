"""资金流 CLI 参数、机器可读输出和装配门禁测试。"""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import pytest

from service_data_sync.application.money_flow.sync import MoneyFlowSyncResult
from service_data_sync.application.ports.money_flow import PublishedMoneyFlow
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.entrypoints import money_flow


class FakeRegistry:
    """返回受控 provider 数量。"""

    def __init__(self, providers: tuple[object, ...]) -> None:
        """保存 provider 集。"""
        self.providers = providers

    def for_capability(self, _: str) -> tuple[object, ...]:
        """返回固定 provider 集。"""
        return self.providers


def _result(*, published: bool) -> MoneyFlowSyncResult:
    """构造 CLI 输出所需同步摘要。"""
    return MoneyFlowSyncResult(
        capability="money_flow.order_size.daily.market.raw",
        source_payload_sha256="a" * 64,
        raw_uri="s3://private/raw/evidence.json",
        publication=PublishedMoneyFlow(
            data_version=(UUID("00000000-0000-4000-8000-000000000140") if published else None),
            inserted_count=1,
            revised_count=2,
            unchanged_count=3,
            published=published,
            quality_status="passed",
        ),
    )


def test_parameters_are_sorted_and_reject_ambiguous_values() -> None:
    """重复、空键、空值和缺少等号的 CLI 参数都必须拒绝。"""
    assert money_flow._parameters(("symbol=600000", "exchange=SSE")) == (
        ("exchange", "SSE"),
        ("symbol", "600000"),
    )
    invalid_sets = (
        ("missing-separator",),
        ("=value",),
        ("key=",),
        ("key=one", "key=two"),
    )
    for values in invalid_sets:
        with pytest.raises(ValueError):
            money_flow._parameters(values)


def test_main_prints_complete_json_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI 成功后输出 raw、dataVersion 和修订计数，不泄漏内部对象。"""

    async def run_fixture(**kwargs: object) -> MoneyFlowSyncResult:
        """验证 CLI 已解析排序参数和回补模式。"""
        assert kwargs == {
            "capability": "money_flow.order_size.daily.market.raw",
            "parameters": (("marketCode", "cn-a"),),
            "mode": "backfill",
        }
        return _result(published=True)

    monkeypatch.setattr(money_flow, "_run", run_fixture)
    exit_code = money_flow.main(
        (
            "--capability",
            "money_flow.order_size.daily.market.raw",
            "--param",
            "marketCode=cn-a",
            "--mode",
            "backfill",
        )
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["dataVersion"] == "00000000-0000-4000-8000-000000000140"
    assert payload["insertedCount"] == 1
    assert payload["revisedCount"] == 2
    assert payload["unchangedCount"] == 3


def test_run_rejects_disabled_or_ambiguous_source_before_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """功能开关关闭或 provider 非唯一时不得创建数据库和对象存储客户端。"""

    def disabled_settings() -> Settings:
        """返回关闭资金流的配置。"""
        return Settings.model_construct(money_flow_enabled=False)

    monkeypatch.setattr(money_flow, "load_settings", disabled_settings)
    with pytest.raises(RuntimeError, match="disabled"):
        asyncio.run(
            money_flow._run(
                capability="money_flow.order_size.daily.market.raw",
                parameters=(("marketCode", "cn-a"),),
                mode="manual",
            )
        )

    def enabled_settings() -> Settings:
        """返回开启资金流的配置。"""
        return Settings.model_construct(money_flow_enabled=True)

    def empty_registry(_: Settings) -> FakeRegistry:
        """返回没有 provider 的注册表。"""
        return FakeRegistry(())

    monkeypatch.setattr(money_flow, "load_settings", enabled_settings)
    monkeypatch.setattr(money_flow, "build_source_registry", empty_registry)
    with pytest.raises(RuntimeError, match="exactly one"):
        asyncio.run(
            money_flow._run(
                capability="money_flow.order_size.daily.market.raw",
                parameters=(("marketCode", "cn-a"),),
                mode="manual",
            )
        )
