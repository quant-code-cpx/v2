"""ETF、两融、港通、公告和交易公开信息受控 CLI 的参数边界测试。"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import cast

import pytest

from service_data_sync.application.ports.data_source import DataSourcePort
from service_data_sync.entrypoints import (
    corporate_events,
    etf,
    margin,
    stock_connect,
    trading_events,
)
from service_data_sync.entrypoints._p0 import build_source_approval, select_single_source
from service_data_sync.infrastructure.persistence.margin_market_data_repository import (
    MarginSourceApproval,
)

_APPROVAL = (
    "--provider-id",
    "approved-source",
    "--source-code",
    "official",
    "--source-legal-name",
    "Approved Source",
    "--source-kind",
    "official",
    "--source-policy",
    "personal-internal-research",
)


class FakeSource:
    """提供最小 adapter 身份，验证 CLI 不会按注册顺序静默选择来源。"""

    def __init__(self, provider_id: str) -> None:
        """保存来源 ID，测试不执行真实网络请求。"""
        self.provider_id = provider_id

    def capabilities(self) -> frozenset[str]:
        """声明空能力，因为来源选择测试只检查身份匹配。"""
        return frozenset()

    async def fetch(self, request: object) -> object:
        """阻止任何意外网络调用。"""
        del request
        raise AssertionError("unexpected provider fetch")


def test_etf_master_requires_snapshot_partition_and_listing_requires_window() -> None:
    """ETF 目录快照不接受无日期发布，上市工具任务不允许没有日期范围。"""
    with pytest.raises(SystemExit):
        etf._parse_args(["--operation", "master", "--venue", "SSE", *_APPROVAL])
    with pytest.raises(SystemExit):
        etf._parse_args(["--operation", "bars", "--etf", "SSE.510300", *_APPROVAL])


def test_etf_master_accepts_explicit_snapshot_partition() -> None:
    """完整目录分区和书面来源批准字段可被 ETF CLI 精确解析。"""
    arguments = etf._parse_args(
        [
            "--operation",
            "master",
            "--venue",
            "SSE",
            "--observation-date",
            "2026-07-29",
            *_APPROVAL,
        ]
    )

    assert arguments.venue == "SSE"
    assert arguments.observation_date.isoformat() == "2026-07-29"


def test_p0_personal_research_policy_forbids_redistribution() -> None:
    """个人研究策略必须持久化为明确权利状态和禁止再分发范围。"""
    arguments = margin._parse_args(
        [
            "--operation",
            "market",
            "--venue",
            "SSE",
            "--start",
            "2026-07-29",
            "--end",
            "2026-07-29",
            *_APPROVAL,
        ]
    )

    approval = build_source_approval(arguments, MarginSourceApproval)

    assert approval.rights_status == "personal_internal_research"
    assert approval.license_scope == "internal_research_no_redistribution"


@pytest.mark.parametrize(
    ("parser", "arguments"),
    [
        (
            margin._parse_args,
            [
                "--operation",
                "market",
                "--venue",
                "SSE",
                "--start",
                "2026-07-30",
                "--end",
                "2026-07-29",
                *_APPROVAL,
            ],
        ),
        (
            stock_connect._parse_args,
            [
                "--operation",
                "market",
                "--channel",
                "SH",
                "--direction",
                "NORTHBOUND",
                "--start",
                "2026-07-30",
                "--end",
                "2026-07-29",
                *_APPROVAL,
            ],
        ),
        (
            corporate_events._parse_args,
            ["--start", "2026-07-30", "--end", "2026-07-29", *_APPROVAL],
        ),
        (
            trading_events._parse_args,
            [
                "--operation",
                "dragon-tiger",
                "--start",
                "2026-07-30",
                "--end",
                "2026-07-29",
                *_APPROVAL,
            ],
        ),
    ],
)
def test_p0_cli_rejects_reversed_windows(
    parser: Callable[[Sequence[str] | None], argparse.Namespace], arguments: list[str]
) -> None:
    """每个时间序列 P0 入口都会在构造容器和访问来源前拒绝倒置窗口。"""
    with pytest.raises(SystemExit):
        parser(arguments)


def test_p0_cli_selects_only_one_exact_provider_or_returns_empty_path() -> None:
    """没有匹配或存在重复匹配时不按注册顺序 fallback，而由入口返回空状态。"""
    source = select_single_source(
        sources=(cast(DataSourcePort, FakeSource("approved-source")),),
        provider_id="approved-source",
        capability="market.margin.market.1d.reported",
    )

    assert source is not None
    assert source.provider_id == "approved-source"
    assert (
        select_single_source(
            sources=(
                cast(DataSourcePort, FakeSource("approved-source")),
                cast(DataSourcePort, FakeSource("approved-source")),
            ),
            provider_id="approved-source",
            capability="market.margin.market.1d.reported",
        )
        is None
    )


def test_p0_cli_uses_personal_akshare_defaults_when_source_metadata_is_omitted() -> None:
    """自用环境没有已接 adapter 时仍可发起空同步，不需要先填写来源元数据。"""
    arguments = margin._parse_args(
        [
            "--operation",
            "market",
            "--venue",
            "SSE",
            "--start",
            "2026-07-29",
            "--end",
            "2026-07-29",
        ]
    )

    assert arguments.provider_id == "akshare"
    assert arguments.source_policy == "personal-internal-research"
