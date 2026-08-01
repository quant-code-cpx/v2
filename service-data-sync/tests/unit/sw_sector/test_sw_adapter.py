"""固定 AKShare 申万函数签名与字段映射的 adapter 回归测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from decimal import Decimal
from functools import partial
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from service_data_sync.application.ports.data_source import (
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.infrastructure.providers.akshare import sw_industry_snapshot
from service_data_sync.infrastructure.providers.akshare.sw_industry_snapshot import (
    AkshareSwIndustrySnapshotAdapter,
)


def test_adapter_calls_fixed_version_real_functions_and_freezes_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """adapter 应调用三个真实无参数函数，并输出父级和百分比原单位字段。"""
    calls: list[str] = []

    def first() -> pd.DataFrame:
        """模拟 `sw_index_first_info()` 的固定版本返回。"""
        calls.append("sw_index_first_info")
        return _frame(level=1)

    def second() -> pd.DataFrame:
        """模拟 `sw_index_second_info()` 的固定版本返回。"""
        calls.append("sw_index_second_info")
        return _frame(level=2)

    def third() -> pd.DataFrame:
        """模拟 `sw_index_third_info()` 的固定版本返回。"""
        calls.append("sw_index_third_info")
        return _frame(level=3)

    monkeypatch.setattr(sw_industry_snapshot.ak, "sw_index_first_info", first)
    monkeypatch.setattr(sw_industry_snapshot.ak, "sw_index_second_info", second)
    monkeypatch.setattr(sw_industry_snapshot.ak, "sw_index_third_info", third)
    snapshot_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()

    batch = asyncio.run(
        AkshareSwIndustrySnapshotAdapter(request_timeout_seconds=5).fetch(
            SourceRequest(
                capability="sector.sw.snapshot.raw",
                parameters=(("snapshotDate", snapshot_date.isoformat()),),
            )
        )
    )
    payload = json.loads(batch.payload)
    raw = json.loads(batch.raw_payload or b"{}")

    assert calls == [
        "sw_index_first_info",
        "sw_index_second_info",
        "sw_index_third_info",
    ]
    assert payload["schema"] == "quant-v2.sw-industry-snapshot.v1"
    assert payload["levels"][1]["items"][0]["parentName"] == "农林牧渔"
    assert payload["levels"][2]["items"][0]["dividendYieldPercent"] == "0.61"
    assert raw["functions"] == calls
    assert batch.adapter_version == "akshare-1.18.81-legulegu-sw-overview-v1"
    assert len(batch.schema_fingerprint or "") == 64


def test_adapter_quarantines_unknown_source_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    """任意未知附加列都应改变冻结 schema 并阻断整个三级快照。"""

    def invalid_frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """返回带未知列的一级 frame，模拟上游 schema drift。"""
        first = _frame(level=1)
        first["未知字段"] = "drift"
        return first, _frame(level=2), _frame(level=3)

    monkeypatch.setattr(sw_industry_snapshot, "_fetch_frames", invalid_frames)
    snapshot_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()

    with pytest.raises(ProviderError) as captured:
        asyncio.run(
            AkshareSwIndustrySnapshotAdapter(request_timeout_seconds=5).fetch(
                SourceRequest(
                    capability="sector.sw.snapshot.raw",
                    parameters=(("snapshotDate", snapshot_date.isoformat()),),
                )
            )
        )

    assert captured.value.code == "schema"
    assert captured.value.retryable is False


def test_adapter_validates_timeout_capability_date_and_replay_boundary() -> None:
    """adapter 应拒绝无效超时、未知能力、错误日期和任何历史在线抓取。"""
    with pytest.raises(ValueError, match="positive"):
        AkshareSwIndustrySnapshotAdapter(request_timeout_seconds=0)

    adapter = AkshareSwIndustrySnapshotAdapter(request_timeout_seconds=5)
    assert adapter.capabilities() == frozenset({"sector.sw.snapshot.raw"})

    with pytest.raises(ProviderError) as unsupported:
        asyncio.run(
            adapter.fetch(
                SourceRequest(
                    capability="sector.sw.taxonomy",
                    parameters=(("snapshotDate", date.today().isoformat()),),
                )
            )
        )
    with pytest.raises(ProviderError) as malformed:
        asyncio.run(
            adapter.fetch(
                SourceRequest(
                    capability="sector.sw.snapshot.raw",
                    parameters=(("snapshotDate", "not-a-date"),),
                )
            )
        )
    with pytest.raises(ProviderError) as historical:
        asyncio.run(
            adapter.fetch(
                SourceRequest(
                    capability="sector.sw.snapshot.raw",
                    parameters=(("snapshotDate", "2000-01-01"),),
                )
            )
        )

    assert unsupported.value.code == ProviderErrorCode.INVALID_REQUEST
    assert malformed.value.code == ProviderErrorCode.INVALID_REQUEST
    assert historical.value.code == ProviderErrorCode.INVALID_REQUEST
    assert historical.value.retryable is False


def test_adapter_classifies_timeout_and_provider_failure_as_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """网络超时与非 schema 上游异常都应标记为可重试不可用。"""
    adapter = AkshareSwIndustrySnapshotAdapter(request_timeout_seconds=5)
    request = SourceRequest(
        capability="sector.sw.snapshot.raw",
        parameters=(("snapshotDate", datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()),),
    )

    async def timeout(*arguments: object, **keywords: object) -> object:
        """模拟线程桥接在超时上下文内抛出超时。"""
        del arguments, keywords
        raise TimeoutError

    monkeypatch.setattr(sw_industry_snapshot.asyncio, "to_thread", timeout)
    with pytest.raises(ProviderError) as timed_out:
        asyncio.run(adapter.fetch(request))

    async def fail(*arguments: object, **keywords: object) -> object:
        """模拟 AKShare 请求阶段的普通运行异常。"""
        del arguments, keywords
        raise RuntimeError("upstream failed")

    monkeypatch.setattr(sw_industry_snapshot.asyncio, "to_thread", fail)
    with pytest.raises(ProviderError) as failed:
        asyncio.run(adapter.fetch(request))

    assert timed_out.value.code == ProviderErrorCode.UNAVAILABLE
    assert timed_out.value.retryable is True
    assert failed.value.code == ProviderErrorCode.UNAVAILABLE
    assert failed.value.retryable is True


def test_adapter_quarantines_empty_identity_count_and_non_finite_valuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空层级、缺失身份、小数成分数和无穷估值都应触发 schema 隔离。"""
    snapshot_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    request = SourceRequest(
        capability="sector.sw.snapshot.raw",
        parameters=(("snapshotDate", snapshot_date.isoformat()),),
    )
    adapter = AkshareSwIndustrySnapshotAdapter(request_timeout_seconds=5)
    invalid_sets = [
        (pd.DataFrame(columns=_frame(level=1).columns), _frame(level=2), _frame(level=3)),
        (_changed_frame(level=1, column="行业代码", value=None), _frame(level=2), _frame(level=3)),
        (
            _changed_frame(level=1, column="成份个数", value=Decimal("1.5")),
            _frame(level=2),
            _frame(level=3),
        ),
        (
            _changed_frame(level=1, column="静态市盈率", value=Decimal("Infinity")),
            _frame(level=2),
            _frame(level=3),
        ),
    ]

    for frames in invalid_sets:
        monkeypatch.setattr(sw_industry_snapshot, "_fetch_frames", partial(_return_frames, frames))
        with pytest.raises(ProviderError) as captured:
            asyncio.run(adapter.fetch(request))
        assert captured.value.code == ProviderErrorCode.SCHEMA
        assert captured.value.retryable is False


def test_adapter_preserves_optional_nulls_and_serializes_raw_scalars() -> None:
    """可空估值应保持空值，raw evidence 默认编码应稳定处理日期和精确小数。"""
    assert sw_industry_snapshot._optional_decimal_text(None) is None
    assert sw_industry_snapshot._optional_decimal_text(" ") is None
    assert sw_industry_snapshot._optional_decimal_text("None") is None
    assert sw_industry_snapshot._json_default(date(2026, 7, 28)) == "2026-07-28"
    assert sw_industry_snapshot._json_default(Decimal("1.20")) == "1.20"


def _changed_frame(*, level: int, column: str, value: object) -> pd.DataFrame:
    """复制合法 frame 并替换一个供应商字段。"""
    frame = _frame(level=level)
    frame[column] = frame[column].astype(object)
    frame.loc[0, column] = value
    return frame


def _return_frames(
    frames: tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """返回当前 schema 异常场景的三级 frame。"""
    return frames


def _frame(*, level: int) -> pd.DataFrame:
    """构造与 1.18.81 源码和可执行探针一致的最小 DataFrame。"""
    identity = {
        1: ("801010.SI", "农林牧渔", None),
        2: ("801016.SI", "种植业", "农林牧渔"),
        3: ("850111.SI", "种子", "种植业"),
    }[level]
    row: dict[str, object] = {
        "行业代码": identity[0],
        "行业名称": identity[1],
        "成份个数": 8,
        "静态市盈率": 67.1,
        "TTM(滚动)市盈率": 95.96,
        "市净率": 2.16,
        "静态股息率": 0.61,
    }
    if identity[2] is not None:
        row["上级行业"] = identity[2]
    return pd.DataFrame([row])
