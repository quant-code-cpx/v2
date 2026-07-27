"""AKShare 东财板块成分 adapter 的 SDK 边界与字段映射回归测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from service_data_sync.application.ports.data_source import ProviderError, SourceRequest
from service_data_sync.infrastructure.providers.akshare import eastmoney_sector_membership
from service_data_sync.infrastructure.providers.akshare.eastmoney_sector_membership import (
    AkshareEastmoneySectorMembershipAdapter,
)


class FakeFrame:
    """提供 adapter 所需最小 DataFrame 接口，不引入 pandas 测试依赖。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        """保存确定性供应商记录。"""
        self._rows = rows

    @property
    def empty(self) -> bool:
        """按是否存在记录报告空响应。"""
        return not self._rows

    def to_dict(self, *, orient: str) -> list[dict[str, object]]:
        """仅支持 adapter 使用的 records 投影。"""
        assert orient == "records"
        return self._rows


@pytest.mark.parametrize(
    ("scheme", "function_name"),
    [
        ("eastmoney.industry", "stock_board_industry_cons_em"),
        ("eastmoney.concept", "stock_board_concept_cons_em"),
    ],
)
def test_adapter_calls_only_matching_sdk_function_and_emits_neutral_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    scheme: str,
    function_name: str,
) -> None:
    """行业和概念必须分别调用唯一 SDK 函数，仅输出代码名称成员事实。"""
    captured: dict[str, object] = {}

    def fake_fetch(*, symbol: str) -> FakeFrame:
        """记录 SDK 参数并返回含无关即时行情字段的一条来源记录。"""
        captured["symbol"] = symbol
        return FakeFrame([{"代码": 600000, "名称": "浦发银行", "最新价": 12.34}])

    monkeypatch.setattr(eastmoney_sector_membership.ak, function_name, fake_fetch)
    observation_date = eastmoney_sector_membership.datetime.now(
        eastmoney_sector_membership._SHANGHAI
    ).date()
    batch = asyncio.run(
        AkshareEastmoneySectorMembershipAdapter(request_timeout_seconds=5).fetch(
            SourceRequest(
                capability="sector.membership.snapshot.raw",
                parameters=(
                    ("sectorScheme", scheme),
                    ("sector", "BK0475"),
                    ("observationDate", observation_date.isoformat()),
                ),
            )
        )
    )

    payload = json.loads(batch.payload)
    raw = json.loads(batch.raw_payload or b"{}")
    assert captured == {"symbol": "BK0475"}
    assert payload["members"] == [{"sourceSymbol": "600000", "sourceName": "浦发银行"}]
    assert "最新价" not in payload["members"][0]
    assert raw["records"][0]["最新价"] == 12.34


def test_adapter_rejects_historical_request_without_calling_sdk() -> None:
    """当前集合来源不能诚实支持历史回补，历史日期必须在 SDK 调用前被拒绝。"""
    adapter = AkshareEastmoneySectorMembershipAdapter(request_timeout_seconds=5)

    with pytest.raises(ProviderError, match="current Shanghai date"):
        asyncio.run(
            adapter.fetch(
                SourceRequest(
                    capability="sector.membership.snapshot.raw",
                    parameters=(
                        ("sectorScheme", "eastmoney.industry"),
                        ("sector", "BK0475"),
                        ("observationDate", date(2020, 1, 1).isoformat()),
                    ),
                )
            )
        )
