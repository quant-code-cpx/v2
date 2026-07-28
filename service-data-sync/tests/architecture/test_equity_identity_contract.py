"""验证 Contract C1 后所有证券事实写入只依赖双时间标识历史。"""

from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "service_data_sync"
PERSISTENCE_ROOT = PACKAGE_ROOT / "infrastructure" / "persistence"


def _source(name: str) -> str:
    """读取一个受身份契约约束的仓储源码。"""
    return (PERSISTENCE_ROOT / name).read_text(encoding="utf-8")


def test_existing_fact_writers_use_date_aware_identity_resolution() -> None:
    """行情、参考数据、成分、财务和生命周期写入必须显式调用日期感知解析器。"""
    expected_tokens = {
        "equity_market_data_repository.py": (
            "resolve_identity_on_connection",
            "require_single_confirmed_identity_on_connection",
        ),
        "sector_membership_repository.py": ("resolve_identity_on_connection",),
        "financial_sync_repository.py": ("require_single_confirmed_identity_on_connection",),
        "equity_lifecycle_repository.py": ("require_single_confirmed_identity_on_connection",),
        "money_flow_repository.py": ("require_single_confirmed_identity_on_connection",),
    }

    for file_name, tokens in expected_tokens.items():
        source = _source(file_name)
        assert all(token in source for token in tokens), file_name


def test_fact_writers_do_not_resolve_identity_from_anchor_projection() -> None:
    """当前兼容列可展示但不得再以 exchange+symbol 组合选择事实所属证券。"""
    for file_name in (
        "equity_market_data_repository.py",
        "sector_membership_repository.py",
        "financial_sync_repository.py",
        "equity_lifecycle_repository.py",
        "money_flow_repository.py",
    ):
        source = _source(file_name)
        assert "EquityInstrument.exchange ==" not in source, file_name
        assert "EquityInstrument.symbol ==" not in source, file_name


def test_financial_reader_uses_publication_time_identity_projection() -> None:
    """财务读取按 publication 有效/知识时点还原代码，禁止读取身份锚当前投影。"""
    source = _source("financial_read_repository.py")

    assert "EquityIdentifierVersion" in source
    assert "FinancialPublication.effective_as_of" in source
    assert "FinancialPublication.knowledge_cutoff" in source
    assert "EquityInstrument.exchange ==" not in source
    assert "EquityInstrument.symbol ==" not in source
