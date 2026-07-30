"""发现页游标、列投影、交易状态与申万层级专项测试。"""

from __future__ import annotations

import base64
from datetime import date
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import select

from service_data_sync.infrastructure.database.models.equity.workspace import (
    EquityDiscoveryMembership,
    EquityDiscoverySnapshot,
)
from service_data_sync.interfaces import internal_equity_workspace_api as reader
from service_data_sync.interfaces.internal_sector_api import InternalProblem


class _Rows:
    """提供游标解析候选查询需要的最小结果。"""

    def __init__(self, values: list[Any]) -> None:
        """保存候选行。"""
        self._values = values

    def all(self) -> list[Any]:
        """返回全部候选行。"""
        return list(self._values)


class _CursorSession:
    """返回相同公开代码下的多个永久证券候选。"""

    def __init__(self, values: list[Any]) -> None:
        """保存代码复用候选。"""
        self._values = values

    def scalars(self, _statement: Any) -> _Rows:
        """返回代码复用候选供单向锚点消歧。"""
        return _Rows(self._values)


def _cursor_row(security_id: int) -> EquityDiscoverySnapshot:
    """构造同代码不同永久身份的游标行。"""
    return cast(
        EquityDiscoverySnapshot,
        SimpleNamespace(
            release_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            security_id=security_id,
            exchange="SSE",
            symbol="600000",
            name=f"证券{security_id}",
        ),
    )


def _projection_row(*, trading_status: str = "TRADED") -> EquityDiscoverySnapshot:
    """构造覆盖发现响应全部字段的投影行。"""
    return cast(
        EquityDiscoverySnapshot,
        SimpleNamespace(
            security_id=9,
            exchange="SSE",
            symbol="600519",
            name="贵州茅台",
            lifecycle_status="LISTED",
            trading_status=trading_status,
            trading_status_reason=None,
            listed_on=date(2001, 8, 27),
            delisted_on=None,
            trade_date=date(2026, 7, 29),
            close_price=1,
            previous_close_price=1,
            change_amount=0,
            change_percent=0,
            volume_shares=1,
            amount_cny=1,
            turnover_rate=0,
            capital_effective_on=date(2026, 7, 29),
            total_shares=1,
            listed_tradable_a_shares=1,
            total_market_cap_cny=1,
            float_market_cap_cny=1,
            valuation_date=date(2026, 7, 29),
            pe_ttm=1,
            pb=1,
            ps_ttm=1,
            valuation_source_label="source",
            valuation_methodology_code="valuation",
            valuation_methodology_version="1",
            money_flow_date=date(2026, 7, 29),
            money_flow_net_amount_cny=1,
            money_flow_net_ratio=1,
            money_flow_source_label="source",
            money_flow_methodology_code="flow",
            money_flow_methodology_version="1",
        ),
    )


def _tamper_cursor(value: str) -> str:
    """修改签名首字符以模拟不会受 Base64 填充位影响的游标篡改。"""
    payload, signature = value.split(".", maxsplit=1)
    replacement = "A" if signature[0] != "A" else "B"
    return f"{payload}.{replacement}{signature[1:]}"


def _keep_cursor(value: str) -> str:
    """保持游标正文，仅由测试改变请求 scope。"""
    return value


def test_cursor_uses_unique_opaque_anchor_for_reused_code() -> None:
    """相同交易所代码的两行必须由不可逆唯一锚点准确续页。"""
    version = UUID("11111111-1111-4111-8111-111111111111")
    secret = b"cursor-secret"
    first = _cursor_row(101)
    reused = _cursor_row(202)
    cursor = reader._next_cursor(
        has_more=True,
        page_rows=[reused],
        secret=secret,
        scope="scope-a",
        data_version=version,
    )

    assert cursor is not None
    payload_text = cursor.split(".", maxsplit=1)[0]
    decoded_text = base64.urlsafe_b64decode(payload_text + "=" * (-len(payload_text) % 4)).decode()
    assert "202" not in decoded_text
    assert "security_id" not in decoded_text

    statement = reader._apply_discovery_cursor(
        _CursorSession([first, reused]),
        statement=select(EquityDiscoverySnapshot),
        release_id=first.release_id,
        cursor=cursor,
        sort_spec=[{"field": "symbol", "direction": "ASC"}],
        secret=secret,
        scope="scope-a",
        data_version=version,
    )

    assert "security_id" in str(statement)


@pytest.mark.parametrize(
    ("mutator", "expected_status"),
    [
        (_tamper_cursor, 400),
        (_keep_cursor, 409),
    ],
)
def test_cursor_rejects_tamper_and_scope_reuse(
    mutator: Any,
    expected_status: int,
) -> None:
    """篡改游标返回 400，跨请求范围复用返回 409。"""
    version = UUID("11111111-1111-4111-8111-111111111111")
    cursor = reader._next_cursor(
        has_more=True,
        page_rows=[_cursor_row(1)],
        secret=b"cursor-secret",
        scope="scope-a",
        data_version=version,
    )
    assert cursor is not None

    with pytest.raises(InternalProblem) as raised:
        reader._apply_discovery_cursor(
            _CursorSession([_cursor_row(1)]),
            statement=select(EquityDiscoverySnapshot),
            release_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            cursor=mutator(cursor),
            sort_spec=[{"field": "symbol", "direction": "ASC"}],
            secret=b"cursor-secret",
            scope="scope-b" if expected_status == 409 else "scope-a",
            data_version=version,
        )

    assert raised.value.status == expected_status


def test_cursor_rejects_data_version_reuse() -> None:
    """旧 publication 游标不得在新 dataVersion 中继续分页。"""
    cursor = reader._next_cursor(
        has_more=True,
        page_rows=[_cursor_row(1)],
        secret=b"cursor-secret",
        scope="scope-a",
        data_version=UUID("11111111-1111-4111-8111-111111111111"),
    )
    assert cursor is not None

    with pytest.raises(InternalProblem) as raised:
        reader._apply_discovery_cursor(
            _CursorSession([_cursor_row(1)]),
            statement=select(EquityDiscoverySnapshot),
            release_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            cursor=cursor,
            sort_spec=[{"field": "symbol", "direction": "ASC"}],
            secret=b"cursor-secret",
            scope="scope-a",
            data_version=UUID("22222222-2222-4222-8222-222222222222"),
        )

    assert raised.value.status == 409


@pytest.mark.parametrize(
    ("stored", "public"),
    [
        ("TRADED", "TRADED"),
        ("SUSPENDED", "TRADE_SUSPENDED"),
        ("NO_SESSION", "NO_SESSION"),
        ("NOT_APPLICABLE", "NOT_APPLICABLE"),
        ("UNKNOWN", "UNKNOWN"),
        ("RESUMED", "UNKNOWN"),
    ],
)
def test_trading_status_projects_only_frozen_public_enum(
    stored: str,
    public: str,
) -> None:
    """旧版 RESUMED 必须降为 UNKNOWN，五种公开状态保持原义。"""
    assert reader._trading_status(stored) == public


def test_columns_are_strict_and_change_projection() -> None:
    """列白名单必须去重校验，并让未请求宽字段不进入值投影。"""
    normalized = reader._validate_search({"limit": 20, "columns": ["symbol"]})
    assert normalized["columns"] == ["symbol"]
    assert reader._component_families(frozenset(normalized["columns"])) == frozenset({"identity"})
    assert reader._component_families(frozenset({"tradingStatus"})) == frozenset(
        {"identity", "trading_status"}
    )

    record = reader._discovery_record(
        _projection_row(),
        identity_as_of=date(2026, 7, 29),
        columns=frozenset({"symbol"}),
        memberships=(),
        availability={},
    )
    assert record["market"]["close"] is None
    assert record["market"]["nullReason"] == "COLUMN_NOT_REQUESTED"
    assert record["valuation"]["peTtm"] is None

    with pytest.raises(InternalProblem):
        reader._validate_search({"limit": 20, "columns": ["symbol", "symbol"]})
    with pytest.raises(InternalProblem):
        reader._validate_search({"limit": 20, "columns": ["notAColumn"]})


@pytest.mark.parametrize(
    "payload",
    (
        {
            "limit": 20,
            "moneyFlow": {
                "bucket": "MAIN",
                "methodology": {"code": "provider.research"},
                "range": {"min": "1"},
            },
        },
        {
            "limit": 20,
            "sort": [{"field": "moneyFlowNetAmount", "direction": "DESC"}],
        },
        {
            "limit": 20,
            "columns": ["symbol", "moneyFlowNetAmount"],
        },
        {
            "limit": 20,
            "columns": ["symbol", "moneyFlowNetRatio"],
        },
    ),
)
def test_discovery_money_flow_capability_fails_closed(payload: dict[str, object]) -> None:
    """研究来源未获准发布时，筛选、排序和列都稳定拒绝，不能返回误导性全空结果。"""
    with pytest.raises(InternalProblem) as raised:
        reader._validate_search(payload)

    assert raised.value.status == 400
    assert raised.value.code == "validation-error"
    assert raised.value.detail == "money flow discovery capability is unavailable"


def test_discovery_default_capabilities_hide_unavailable_money_flow() -> None:
    """默认列和排序能力不声明资金流，但响应 envelope 仍可表达未请求状态。"""
    normalized = reader._validate_search({"limit": 20})

    assert "moneyFlowNetAmount" not in normalized["columns"]
    assert "moneyFlowNetRatio" not in normalized["columns"]
    assert "moneyFlowNetAmount" not in reader._SORT_FIELDS
    record = reader._discovery_record(
        _projection_row(),
        identity_as_of=date(2026, 7, 29),
        columns=frozenset(normalized["columns"]),
        memberships=(),
        availability={},
    )
    assert record["moneyFlow"]["netAmountCny"] is None
    assert record["moneyFlow"]["netRatio"] is None
    assert record["moneyFlow"]["nullReason"] == "COLUMN_NOT_REQUESTED"


def test_suspended_listing_lifecycle_fails_closed_without_verified_producer() -> None:
    """暂停上市尚无已验证 producer 时必须拒绝筛选，不能把未覆盖解释成零只证券。"""
    with pytest.raises(InternalProblem) as raised:
        reader._validate_search({"limit": 20, "lifecycleStatuses": ["SUSPENDED"]})

    assert raised.value.status == 400
    assert raised.value.code == "validation-error"
    assert raised.value.detail == "suspended listing lifecycle capability is unavailable"


@pytest.mark.parametrize(
    ("level", "scheme"),
    [("1", "SW2021_L1"), ("2", "SW2021_L2"), ("3", "SW2021_L3")],
)
def test_sw_membership_preserves_level(level: str, scheme: str) -> None:
    """申万成员必须按真实层级投影，不能统一冒充三级。"""
    membership = cast(
        EquityDiscoveryMembership,
        SimpleNamespace(
            scheme="sw.industry",
            code=f"8010{level}0",
            name=f"申万{level}级",
            level=level,
            observed_on=date(2026, 7, 29),
        ),
    )
    record = reader._discovery_record(
        _projection_row(),
        identity_as_of=date(2026, 7, 29),
        columns=frozenset({"memberships"}),
        memberships=(membership,),
        availability={},
    )

    assert record["memberships"] == [
        {
            "scheme": scheme,
            "code": membership.code,
            "name": membership.name,
            "level": int(level),
            "observedOn": "2026-07-29",
        }
    ]


def test_delisted_identity_uses_last_effective_business_date() -> None:
    """退市发现行必须输出可解析旧身份的 delistedOn 锚点。"""
    row = _projection_row()
    row.lifecycle_status = "DELISTED"
    row.delisted_on = date(2020, 1, 2)

    assert reader._identity_as_of(row, snapshot_as_of=date(2026, 7, 29)) == date(2020, 1, 2)
