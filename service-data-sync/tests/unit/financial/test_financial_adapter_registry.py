"""财务 source policy 与 AKShare 东财 adapter 注册边界测试。"""

from __future__ import annotations

from service_data_sync.bootstrap.container import build_source_registry
from service_data_sync.bootstrap.settings import load_settings


def test_akshare_eastmoney_policy_registers_all_financial_capabilities(
    configured_environment: None,
) -> None:
    """显式东财策略应注册同一个 provider 的三项财务能力，且不产生网络请求。"""
    settings = load_settings().model_copy(
        update={
            "akshare_enabled": True,
            "financial_enabled": True,
            "financial_source_policy": "akshare-eastmoney",
            "financial_max_concurrency": 1,
            "financial_requests_per_minute": 1,
            "financial_request_timeout_seconds": 5,
        }
    )

    registry = build_source_registry(settings)

    provider_ids = {
        provider.provider_id
        for capability in (
            "financial.statement.raw",
            "financial.metric.raw",
            "financial.valuation.raw",
        )
        for provider in registry.for_capability(capability)
    }
    assert provider_ids == {"akshare-eastmoney-financial"}


def test_unrecognized_financial_policy_does_not_reuse_akshare_adapter(
    configured_environment: None,
) -> None:
    """未知策略不能被静默映射到东财 adapter，避免来源声明与实际调用不一致。"""
    settings = load_settings().model_copy(
        update={
            "akshare_enabled": True,
            "financial_enabled": True,
            "financial_source_policy": "another-provider",
            "financial_max_concurrency": 1,
            "financial_requests_per_minute": 1,
            "financial_request_timeout_seconds": 5,
        }
    )

    registry = build_source_registry(settings)

    assert registry.for_capability("financial.statement.raw") == ()
