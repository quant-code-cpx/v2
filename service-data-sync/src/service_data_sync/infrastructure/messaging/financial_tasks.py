"""财务来源探针和受控单证券同步 `Celery` 任务。

本模块负责从配置组装一个来源中立数据源和仓储，并把一次证券同步的失败证据
留存到服务自有桶。它不解释报表字段、不跨供应商拼接报表，也不把原始响应写入任务
结果；这些数据口径与发布事务分别由应用层和持久化层负责。
"""

from __future__ import annotations

import structlog
from celery import Celery

from service_data_sync.application.source_registry import SourceRegistry
from service_data_sync.bootstrap.container import build_container, build_source_registry
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    build_catalog,
)
from service_data_sync.infrastructure.data_operations.legacy_submission import submit_system_command

_PROBE_TASK = "service_data_sync.financial.probe"
_SYNC_TASK = "service_data_sync.financial.sync_security"
_REQUIRED_CAPABILITIES = frozenset(
    {
        "financial.statement.raw",
        "financial.metric.raw",
        "financial.valuation.raw",
    }
)
_LOGGER = structlog.get_logger(__name__)


def register_financial_tasks(app: Celery, *, settings: Settings) -> None:
    """注册无自动调度的探针和单证券同步任务；重复初始化保持幂等。

    财务同步没有隐式全市场调度，调用方必须明确选择证券，便于控制供应商许可和负载。
    """
    if _PROBE_TASK not in app.tasks:

        @app.task(name=_PROBE_TASK, shared=False)
        def probe() -> dict[str, str | int]:
            """报告来源和同步代码是否可用；探针只读取 adapter 声明能力，绝不调用 `fetch`。"""
            if not settings.financial_enabled:
                return _probe_result(
                    status="disabled",
                    capability_count=0,
                    provider_count=0,
                    settings=settings,
                )

            registry = build_source_registry(settings)
            capability_count, provider_count = _financial_adapter_summary(registry)
            status = (
                "sync-ready"
                if capability_count == len(_REQUIRED_CAPABILITIES)
                else "provider-adapter-unavailable"
            )
            return _probe_result(
                status=status,
                capability_count=capability_count,
                provider_count=provider_count,
                settings=settings,
            )

    if _SYNC_TASK in app.tasks:
        return

    @app.task(name=_SYNC_TASK, shared=False)
    def sync_security(exchange: str, symbol: str) -> dict[str, object]:
        """将明确证券参数转换为 command，禁止 Celery 任务直接同步财务 canonical 数据。"""
        if not settings.financial_enabled:
            raise RuntimeError("financial sync is disabled")
        if exchange not in {"SSE", "SZSE", "BSE"} or not symbol.strip():
            raise ValueError("financial security selector is invalid")
        container = build_container(settings)
        try:
            control_plane = DataOperationsControlPlane(
                database=container.database,
                catalog=build_catalog(settings, container.source_registry),
                source_registry=container.source_registry,
                trading_calendar=container.trading_calendar,
            )
            return submit_system_command(
                control_plane,
                target={
                    "datasetCode": "financial.report",
                    "mode": "INCREMENTAL",
                    "selector": {"kind": "INSTRUMENT", "exchange": exchange, "symbol": symbol},
                    "dateFrom": None,
                    "dateTo": None,
                    "observationDate": None,
                },
                reason="兼容财务 Celery 提交",
                request_prefix="legacy-financial-task",
            )
        finally:
            container.close()


def _financial_adapter_summary(registry: SourceRegistry) -> tuple[int, int]:
    """统计已声明的财务能力与适配器数；不在探针中决定来源组合或数据合并。

    返回数量而非 provider 细节，避免健康检查输出配置、凭据或未获批来源信息。
    """
    provider_ids: set[str] = set()
    available_capability_count = 0
    for capability in _REQUIRED_CAPABILITIES:
        capability_providers = {
            provider.provider_id for provider in registry.for_capability(capability)
        }
        if capability_providers:
            available_capability_count += 1
        provider_ids.update(capability_providers)
    return available_capability_count, len(provider_ids)


def _probe_result(
    *, status: str, capability_count: int, provider_count: int, settings: Settings
) -> dict[str, str | int]:
    """记录低基数准入结论，并返回可由受控运维任务读取的最小摘要。"""
    _LOGGER.info(
        "financial.dark_launch_probe_completed",
        status=status,
        source_policy=settings.financial_source_policy,
        capability_count=capability_count,
        provider_count=provider_count,
    )
    return {
        "status": status,
        "capabilityCount": capability_count,
        "providerCount": provider_count,
    }
