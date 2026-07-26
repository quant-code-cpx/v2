"""验证数据同步服务分层与数据源隔离边界。"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "service_data_sync"
FORBIDDEN_PROVIDER_MODULES = {"akshare", "tushare"}
FORBIDDEN_PROVIDER_SYMBOLS = {"akshare", "tushare", "ts_code", "dataframe"}


def _imports(path: Path) -> set[str]:
    """不执行源码，返回一个文件导入的模块名集合。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _source_files() -> list[Path]:
    """返回受架构约束检查的全部包内 Python 文件。"""
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def test_application_does_not_depend_on_infrastructure() -> None:
    """保证应用层不依赖基础设施实现。"""
    violations = [
        path
        for path in (PACKAGE_ROOT / "application").rglob("*.py")
        if any(item.startswith("service_data_sync.infrastructure") for item in _imports(path))
    ]

    assert violations == []


def test_provider_sdk_imports_are_scoped_to_provider_adapters() -> None:
    """仅允许专用数据源适配器边界导入供应商 SDK。"""
    violations: list[Path] = []
    provider_root = PACKAGE_ROOT / "infrastructure" / "providers"
    for path in _source_files():
        modules = {item.split(".", maxsplit=1)[0] for item in _imports(path)}
        if modules & FORBIDDEN_PROVIDER_MODULES and provider_root not in path.parents:
            violations.append(path)

    assert violations == []


def test_provider_adapters_cannot_depend_on_persistence_or_messaging() -> None:
    """阻止适配器绕过标准持久化与消息端口。"""
    provider_root = PACKAGE_ROOT / "infrastructure" / "providers"
    violations = [
        path
        for path in provider_root.rglob("*.py")
        if any(
            item.startswith("service_data_sync.infrastructure.database")
            or item.startswith("service_data_sync.infrastructure.messaging")
            for item in _imports(path)
        )
    ]

    assert violations == []


def test_provider_specific_symbols_are_absent_from_port_contract() -> None:
    """保证数据源无关端口不出现供应商专有词汇。"""
    port_source = (
        (PACKAGE_ROOT / "application" / "ports" / "data_source.py")
        .read_text(encoding="utf-8")
        .lower()
    )

    assert all(symbol not in port_source for symbol in FORBIDDEN_PROVIDER_SYMBOLS)
