"""资金流读取仓储的签名游标、参数门禁和公开投影测试。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import pytest

from service_data_sync.domain.equity import Exchange
from service_data_sync.domain.money_flow import (
    MoneyFlowScope,
    MoneyFlowScopeType,
    MoneyFlowWindowType,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence import money_flow_read_repository


def _repository() -> money_flow_read_repository.SqlAlchemyMoneyFlowReadRepository:
    """构造只用于纯校验和游标方法的仓储。"""
    database = cast(DatabaseClient, cast(Any, object()))
    return money_flow_read_repository.SqlAlchemyMoneyFlowReadRepository(
        database,
        cursor_secret=b"fixture-money-flow-cursor-secret-32",
    )


def _scope(scope_type: MoneyFlowScopeType) -> MoneyFlowScope:
    """构造三类读取 scope。"""
    if scope_type is MoneyFlowScopeType.EQUITY:
        return MoneyFlowScope(
            scope_type=scope_type,
            exchange=Exchange.SSE,
            symbol="600000",
            name="浦发银行",
        )
    if scope_type is MoneyFlowScopeType.SECTOR:
        return MoneyFlowScope(
            scope_type=scope_type,
            sector_scheme="eastmoney.industry",
            sector_code="BK0475",
            name="银行",
        )
    return MoneyFlowScope(
        scope_type=scope_type,
        market_code="cn-a",
        name="A 股",
    )


def _methodology_row() -> dict[str, object]:
    """构造读取投影所需方法学字段。"""
    return {
        "public_key": "fixture-money-flow",
        "version": "1",
        "upstream_source": "fixture-source",
        "source_dataset": "fixture-dataset",
        "semantic_family": "order_size_flow",
        "supports_gross_inflow": False,
        "supports_gross_outflow": False,
        "supports_net_amount": True,
        "supports_net_ratio": True,
        "ratio_denominator": "供应商成交额",
        "direction_definition": "供应商订单规模净流入",
        "currency": None,
        "standard_amount_unit": None,
        "raw_amount_unit": "unknown",
    }


def test_constructor_and_public_query_validation_fail_before_database_access() -> None:
    """弱 HMAC、越界分页、错误日期和窗口语义必须在访问数据库前拒绝。"""
    database = cast(DatabaseClient, cast(Any, object()))
    with pytest.raises(ValueError, match="at least 32"):
        money_flow_read_repository.SqlAlchemyMoneyFlowReadRepository(
            database,
            cursor_secret=b"short",
        )
    repository = _repository()
    with pytest.raises(ValueError, match="1 to 100"):
        repository.list_methodologies(
            semantic_family=None,
            methodology_status=None,
            scope_type=None,
            cursor=None,
            limit=0,
        )
    with pytest.raises(ValueError, match="date range"):
        repository.list_daily(
            methodology_id="fixture-money-flow",
            methodology_version="1",
            scope=_scope(MoneyFlowScopeType.EQUITY),
            bucket="main",
            start=date(2026, 7, 25),
            end=date(2026, 7, 24),
            known_at=None,
            cursor=None,
            limit=1,
        )
    with pytest.raises(ValueError, match="timezone"):
        repository.list_daily(
            methodology_id="fixture-money-flow",
            methodology_version="1",
            scope=_scope(MoneyFlowScopeType.EQUITY),
            bucket="main",
            start=date(2026, 7, 1),
            end=date(2026, 7, 24),
            known_at=datetime(2026, 7, 24, 10),
            cursor=None,
            limit=1,
        )
    with pytest.raises(ValueError, match="market scope"):
        repository.list_ranking(
            methodology_id="fixture-money-flow",
            methodology_version="1",
            scope_type=MoneyFlowScopeType.MARKET,
            universe="cn-a",
            window_type=MoneyFlowWindowType.SUPPLIER_DAY,
            window_size=1,
            bucket="main",
            trade_date=None,
            cursor=None,
            limit=1,
        )
    invalid_windows = (
        (MoneyFlowWindowType.SUPPLIER_DAY, 2),
        (MoneyFlowWindowType.SUPPLIER_ROLLING, 1),
        (MoneyFlowWindowType.SUPPLIER_ROLLING, 253),
    )
    for window_type, window_size in invalid_windows:
        with pytest.raises(ValueError, match="window"):
            repository.list_ranking(
                methodology_id="fixture-money-flow",
                methodology_version="1",
                scope_type=MoneyFlowScopeType.EQUITY,
                universe="cn-a",
                window_type=window_type,
                window_size=window_size,
                bucket="main",
                trade_date=None,
                cursor=None,
                limit=1,
            )


def test_hmac_cursor_binds_filters_and_rejects_tampering() -> None:
    """游标签名必须绑定筛选和最后位置，任何篡改均稳定冲突。"""
    repository = _repository()
    filters = {"kind": "ranking", "dataVersion": str(UUID(int=1))}
    cursor = repository._encode_cursor(filters, last=3)

    assert repository._decode_cursor(cursor, filters) == {
        "v": 1,
        "filters": filters,
        "last": 3,
    }
    with pytest.raises(money_flow_read_repository.MoneyFlowCursorMismatch):
        repository._decode_cursor(f"{cursor[:-1]}x", filters)
    with pytest.raises(money_flow_read_repository.MoneyFlowCursorMismatch):
        repository._decode_cursor(cursor, {"kind": "other"})
    with pytest.raises(money_flow_read_repository.MoneyFlowCursorMismatch):
        repository._decode_cursor("not-a-cursor", filters)
    assert repository._decode_cursor(None, filters) is None


def test_cursor_position_and_methodology_projection_are_typed_and_stable() -> None:
    """排行位置必须为正整数，方法学度量按固定顺序公开。"""
    assert money_flow_read_repository._cursor_positive_int({"last": 2}, "last") == 2
    for payload in ({"last": True}, {"last": "2"}, {"last": 0}, {}):
        with pytest.raises(money_flow_read_repository.MoneyFlowCursorMismatch):
            money_flow_read_repository._cursor_positive_int(payload, "last")

    row = _methodology_row()
    assert money_flow_read_repository._supported_measures(row) == [
        "net_amount",
        "net_ratio",
    ]
    summary = money_flow_read_repository._methodology_summary(row)
    assert summary["methodologyId"] == "fixture-money-flow"
    assert summary["amountUnit"] == "unknown"
    assert money_flow_read_repository._methodology_sort_key("fixture", "1") == "fixture\u00001"


def test_scope_cursor_and_response_projection_never_expose_ambiguous_identity() -> None:
    """三类 scope 游标和响应分别使用证券、板块或市场强身份。"""
    equity = _scope(MoneyFlowScopeType.EQUITY)
    sector = _scope(MoneyFlowScopeType.SECTOR)
    market = _scope(MoneyFlowScopeType.MARKET)

    assert money_flow_read_repository._scope_cursor_identity(equity) == {
        "scopeType": "equity",
        "exchange": "SSE",
        "symbol": "600000",
        "sectorScheme": None,
        "sectorCode": None,
        "marketCode": None,
    }
    assert (
        money_flow_read_repository._scope_projection(
            equity,
            42,
            {
                "instrument_id": UUID("00000000-0000-4000-8000-000000000130"),
                "equity_name": "浦发银行",
            },
        )["securityId"]
        == 42
    )
    assert (
        money_flow_read_repository._scope_projection(
            sector,
            43,
            {
                "sector_id": UUID("00000000-0000-4000-8000-000000000131"),
                "sector_name": "银行",
            },
        )["sectorCode"]
        == "BK0475"
    )
    assert money_flow_read_repository._scope_projection(market, "cn-a", {}) == {
        "scopeType": "market",
        "marketCode": "cn-a",
        "name": "A 股",
    }


def test_ranking_item_projection_requires_publication_time_equity_identity() -> None:
    """证券排行必须能按发布截点投影代码，板块排行保留 canonical sector。"""
    metrics = {
        "supplier_position": 1,
        "scope_name_at_snapshot": "浦发银行",
        "gross_inflow": None,
        "gross_outflow": None,
        "net_amount": Decimal("1.2300"),
        "net_ratio": Decimal("0.01"),
    }
    equity = money_flow_read_repository._ranking_item(
        {
            **metrics,
            "scope_type": "equity",
            "security_id": 42,
            "instrument_id": UUID("00000000-0000-4000-8000-000000000132"),
            "exchange": "SSE",
            "symbol": "600000",
            "sector_id": None,
            "scheme": None,
            "sector_code": None,
        }
    )
    sector = money_flow_read_repository._ranking_item(
        {
            **metrics,
            "scope_type": "sector",
            "security_id": None,
            "instrument_id": None,
            "exchange": None,
            "symbol": None,
            "sector_id": UUID("00000000-0000-4000-8000-000000000133"),
            "scheme": "eastmoney.industry",
            "sector_code": "BK0475",
        }
    )

    equity_scope = cast(dict[str, object], equity["scope"])
    sector_scope = cast(dict[str, object], sector["scope"])
    assert equity_scope["symbol"] == "600000"
    assert equity["netAmount"] == "1.2300"
    assert sector_scope["sectorCode"] == "BK0475"
    with pytest.raises(money_flow_read_repository.MoneyFlowReadUnavailable):
        money_flow_read_repository._ranking_item(
            {
                **metrics,
                "scope_type": "equity",
                "security_id": 42,
                "instrument_id": None,
                "exchange": None,
                "symbol": None,
            }
        )


def test_partition_decimal_time_and_base64_helpers_are_canonical() -> None:
    """分区键、精确小数、UTC 时间和无填充 Base64 必须可复验。"""
    partition = money_flow_read_repository._ranking_partition_key(
        {
            "public_key": "fixture-money-flow",
            "version": "1",
            "scope_type": "equity",
            "universe_code": "cn-a",
            "window_type": "supplier_day",
            "window_size": 1,
            "bucket_code": "main",
            "target_trade_date": date(2026, 7, 24),
        }
    )
    assert partition == ("fixture-money-flow/1/ranking/equity/cn-a/supplier_day/1/main/2026-07-24")
    assert money_flow_read_repository._decimal_text(Decimal("1.2300")) == "1.2300"
    assert money_flow_read_repository._decimal_text(None) is None
    shanghai = datetime.fromisoformat("2026-07-24T18:00:00+08:00")
    assert money_flow_read_repository._timestamp(shanghai) == "2026-07-24T10:00:00Z"
    with pytest.raises(money_flow_read_repository.MoneyFlowReadUnavailable):
        money_flow_read_repository._aware_datetime(datetime(2026, 7, 24, 10))
    encoded = money_flow_read_repository._b64url(b"money-flow?")
    assert "=" not in encoded
    assert money_flow_read_repository._b64url_decode(encoded) == b"money-flow?"
