"""东财证券目录异步传输和标准化边界的单元测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from service_data_sync.application.ports.data_source import ProviderError, SourceRequest
from service_data_sync.infrastructure.providers.akshare.eastmoney_equity_catalog import (
    AkshareEastmoneyEquityCatalogAdapter,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _catalog_response(records: list[dict[str, object]], *, total: int) -> httpx.Response:
    """构造冻结东财分页形状，避免测试依赖真实网络或供应商 SDK。"""
    return httpx.Response(200, json={"data": {"total": total, "diff": records}})


def _current_request(*, exchange: str = "SSE") -> SourceRequest:
    """构造当前上海日期的目录请求，确保网络测试只覆盖 adapter 传输逻辑。"""
    return SourceRequest(
        capability="equity.master.catalog",
        parameters=(
            ("exchange", exchange),
            ("targetDate", datetime.now(_SHANGHAI).date().isoformat()),
        ),
    )


def test_adapter_partitions_full_market_snapshot_by_exchange() -> None:
    """东财全市场快照必须只把目标交易所的标准代码交给目录发布。"""

    async def handler(_request: httpx.Request) -> httpx.Response:
        """返回一个含沪深北三市记录的单页目录。"""
        return _catalog_response(
            [
                {"f12": "600519", "f14": "贵州茅台"},
                {"f12": "000001", "f14": "平安银行"},
                {"f12": "920002", "f14": "万达轴承"},
            ],
            total=3,
        )

    batch = asyncio.run(
        AkshareEastmoneyEquityCatalogAdapter(
            request_timeout_seconds=5,
            transport=httpx.MockTransport(handler),
        ).fetch(_current_request())
    )

    payload = json.loads(batch.payload)
    assert payload["exchange"] == "SSE"
    assert payload["entries"] == [{"symbol": "600519", "name": "贵州茅台", "listedOn": None}]
    assert batch.upstream_source == "eastmoney"
    assert batch.schema_fingerprint is not None


def test_adapter_rejects_backfill_request_for_current_only_spot_source() -> None:
    """现货来源没有历史目录能力，不能把当前快照伪装为历史回补。"""
    calls = 0

    async def fail_if_called(_request: httpx.Request) -> httpx.Response:
        """确保无效目标日在访问东财前被拒绝。"""
        nonlocal calls
        calls += 1
        raise AssertionError("历史目录请求不应访问现货来源")

    with pytest.raises(ProviderError, match="current Shanghai date") as error:
        asyncio.run(
            AkshareEastmoneyEquityCatalogAdapter(
                request_timeout_seconds=5,
                transport=httpx.MockTransport(fail_if_called),
            ).fetch(
                SourceRequest(
                    capability="equity.master.catalog",
                    parameters=(("exchange", "SSE"), ("targetDate", "2026-01-01")),
                )
            )
        )

    assert error.value.retryable is False
    assert calls == 0


def test_adapter_cancels_hanging_transport_and_exposes_retryable_timeout() -> None:
    """目录总预算到期必须取消 TLS/HTTP transport，让 dispatcher 无需等待后台线程即可收敛。"""
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def hanging_handler(_request: httpx.Request) -> httpx.Response:
        """模拟卡在 TLS 握手的请求，并记录 ``httpx`` 是否收到取消。"""
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("不可达")

    async def run_timeout_case() -> ProviderError:
        """验证超时终态在 transport 被取消后立即回到 provider 错误边界。"""
        adapter = AkshareEastmoneyEquityCatalogAdapter(
            request_timeout_seconds=0.01,
            transport=httpx.MockTransport(hanging_handler),
        )
        task = asyncio.create_task(adapter.fetch(_current_request()))
        await asyncio.wait_for(entered.wait(), timeout=1)
        with pytest.raises(ProviderError, match="timed out") as raised:
            await asyncio.wait_for(task, timeout=1)
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        assert task.done()
        return raised.value

    error = asyncio.run(run_timeout_case())

    assert error.retryable is True


def test_adapter_maps_httpx_connect_timeout_to_existing_provider_timeout() -> None:
    """连接阶段超时必须保持现有 ``UNAVAILABLE`` 可重试技术错误语义。"""

    async def timeout_handler(_request: httpx.Request) -> httpx.Response:
        """模拟尚未完成 TLS 连接即由异步 client 报出的超时。"""
        raise httpx.ConnectTimeout("TLS handshake timed out")

    with pytest.raises(ProviderError, match="timed out") as raised:
        asyncio.run(
            AkshareEastmoneyEquityCatalogAdapter(
                request_timeout_seconds=5,
                transport=httpx.MockTransport(timeout_handler),
            ).fetch(_current_request())
        )

    assert raised.value.retryable is True


def test_adapter_passes_remaining_total_budget_to_connection_and_read() -> None:
    """每一页请求必须把目录总预算传给 connect/read/write/pool，不能沿用无限 SDK 默认值。"""
    observed_timeout: dict[str, float | None] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        """记录 ``httpx`` 实际交给 transport 的四类 I/O timeout。"""
        observed_timeout.update(request.extensions["timeout"])
        return _catalog_response([{"f12": "600519", "f14": "贵州茅台"}], total=1)

    asyncio.run(
        AkshareEastmoneyEquityCatalogAdapter(
            request_timeout_seconds=5,
            transport=httpx.MockTransport(handler),
        ).fetch(_current_request())
    )

    assert set(observed_timeout) == {"connect", "read", "write", "pool"}
    assert all(value is not None and 0 < value <= 5 for value in observed_timeout.values())
