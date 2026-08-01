"""股票回填正式入口与真实单证券烟测范围测试。"""

from __future__ import annotations

import tomllib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from service_data_sync.entrypoints.equity_backfill import _parse_args
from service_data_sync.infrastructure.data_operations.equity_backfill import FrozenIdentity
from service_data_sync.infrastructure.data_operations.equity_backfill_orchestrator import (
    _instrument_scope,
    _reference_campaign_key,
    _ReferenceInputs,
    _scoped_campaign_key,
    _scoped_reference_inputs,
)
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)


def _identity(*, ordinal: int, exchange: str, symbol: str) -> FrozenIdentity:
    """构造仅供范围选择验证的冻结身份，不代表可被生产计划消费的来源数据。"""
    return FrozenIdentity(
        ordinal=ordinal,
        identifier_version_id=uuid4(),
        security_id=ordinal,
        instrument_id=uuid4(),
        exchange=exchange,
        symbol=symbol,
        effective_from=date(2000, 1, 1),
        effective_to=None,
        known_from=datetime(2026, 7, 31, tzinfo=UTC),
        known_to=None,
        effective_date_precision="OFFICIAL_DATE",
    )


def test_formal_cli_registers_and_parses_real_instrument_smoke_scope() -> None:
    """正式 console script 必须保留全市场默认值，并可解析精确 SSE 烟测身份。"""
    arguments = _parse_args(
        [
            "--campaign-key",
            "equity-live-smoke-20260731",
            "--instrument",
            "SSE.600519",
        ]
    )
    pyproject = tomllib.loads(
        (Path(__file__).parents[4] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert arguments.instrument == ("SSE", "600519")
    assert (
        pyproject["project"]["scripts"]["data-sync-equity-backfill"]
        == "service_data_sync.entrypoints.equity_backfill:main"
    )


def test_scoped_roster_comes_only_from_sealed_master_and_uses_distinct_campaign() -> None:
    """烟测只缩小已封存 roster，且不能复用全市场或另一证券的父计划。"""
    inputs = _ReferenceInputs(
        aggregate=cast(DatasetPublication, object()),
        aggregate_components=(),
        lifecycle_publications=(),
        known_at=datetime(2026, 7, 31, tzinfo=UTC),
        identities=(
            _identity(ordinal=1, exchange="SSE", symbol="600519"),
            _identity(ordinal=2, exchange="SZSE", symbol="000001"),
        ),
    )

    scoped = _scoped_reference_inputs(
        inputs,
        instrument_scope=("SSE", "600519"),
    )

    assert len(scoped.identities) == 1
    assert scoped.identities[0].exchange == "SSE"
    assert scoped.identities[0].symbol == "600519"
    assert scoped.identities[0].ordinal == 1
    assert (
        _scoped_campaign_key(
            "equity-live-smoke-20260731",
            instrument_scope=("SSE", "600519"),
        )
        != "equity-live-smoke-20260731"
    )
    assert len(_reference_campaign_key("x" * 128)) <= 128
    with pytest.raises(ValueError, match="scope"):
        _instrument_scope(("SSE", "60051"))
    with pytest.raises(RuntimeError, match="confirmed identity"):
        _scoped_reference_inputs(inputs, instrument_scope=("BSE", "430047"))
