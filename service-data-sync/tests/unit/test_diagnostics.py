"""基础设施诊断结果、退出码和安全输出的单元测试。"""

from __future__ import annotations

import json

import pytest

from service_data_sync.bootstrap.errors import ConfigurationError
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.entrypoints import diagnostics
from service_data_sync.entrypoints.diagnostics import (
    CheckResult,
    DiagnosticsExitCode,
    DiagnosticsReport,
    main,
    run_diagnostics,
)


def test_diagnostics_reports_success() -> None:
    """将全部成功的依赖探测归类为零退出码。"""
    # 三个匿名回调均模拟健康依赖，用于验证零退出码分支。
    report = run_diagnostics(
        {
            "postgres": lambda: None,
            "redis": lambda: None,
            "s3": lambda: None,
        },
        timeout_seconds=1,
    )

    assert report.exit_code is DiagnosticsExitCode.OK
    assert all(result.ok for result in report.results)


def test_diagnostics_classifies_single_dependency_failure() -> None:
    """将一项失败的 Redis 探测映射为 Redis 专用退出码。"""

    def fail() -> None:
        """模拟不可用的依赖探测。"""
        raise ConnectionError("not exposed")

    # 除 Redis 外的匿名回调模拟健康依赖，隔离单依赖失败分类。
    report = run_diagnostics(
        {
            "postgres": lambda: None,
            "redis": fail,
            "s3": lambda: None,
        },
        timeout_seconds=1,
    )

    assert report.exit_code is DiagnosticsExitCode.REDIS_UNAVAILABLE
    failed = next(result for result in report.results if not result.ok)
    assert failed.dependency == "redis"
    assert failed.error_type == "ConnectionError"


def test_diagnostics_classifies_multiple_failures() -> None:
    """将多项不可用探测映射为组合依赖退出码。"""

    def fail() -> None:
        """模拟不包含载荷细节的依赖探测失败。"""
        raise ConnectionError

    # S3 匿名回调保持健康，以验证两项失败的组合退出码。
    report = run_diagnostics(
        {
            "postgres": fail,
            "redis": fail,
            "s3": lambda: None,
        },
        timeout_seconds=1,
    )

    assert report.exit_code is DiagnosticsExitCode.MULTIPLE_DEPENDENCIES_UNAVAILABLE


def test_diagnose_closes_the_container(
    configured_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """即使诊断正常完成，也必须释放容器依赖。"""
    calls: list[str] = []

    class FakeContainer:
        """暴露可控依赖与关闭记录的容器替身。"""

        def close(self) -> None:
            """记录测试容器的清理调用。"""
            calls.append("closed")

        class database:
            """模拟数据库依赖。"""

            @staticmethod
            def ping() -> None:
                """模拟健康的 PostgreSQL 探测。"""
                return None

        class broker:
            """模拟消息 broker 依赖。"""

            @staticmethod
            def ping() -> None:
                """模拟健康的 Redis 探测。"""
                return None

        class object_storage:
            """模拟对象存储依赖。"""

            @staticmethod
            def ping() -> None:
                """模拟健康的 S3 探测。"""
                return None

    # 替换组合根为可控容器，以验证清理行为。
    # 匿名回调固定返回可控容器，避免诊断测试创建真实基础设施客户端。
    monkeypatch.setattr(diagnostics, "build_container", lambda _settings: FakeContainer())

    report = diagnostics.diagnose(load_settings())

    assert report.exit_code is DiagnosticsExitCode.OK
    assert calls == ["closed"]


def test_main_renders_a_safe_configuration_error(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """返回安全配置报告，不渲染异常链中的校验细节。"""

    def raise_configuration_error() -> None:
        """模拟包含敏感诊断信息的配置加载失败。"""
        raise ConfigurationError("sensitive details are chained")

    monkeypatch.setattr(diagnostics, "load_settings", raise_configuration_error)

    assert main(["--format", "json"]) == DiagnosticsExitCode.CONFIGURATION_ERROR

    rendered = json.loads(capsys.readouterr().out)
    assert rendered["exit_code"] == DiagnosticsExitCode.CONFIGURATION_ERROR
    assert rendered["results"][0]["dependency"] == "configuration"


def test_report_renders_console_and_unknown_failure() -> None:
    """未知依赖名称使用内部错误兜底，并渲染控制台输出。"""
    report = DiagnosticsReport(
        results=(CheckResult("other", ok=False, duration_ms=1, error_type="RuntimeError"),)
    )

    assert report.exit_code is DiagnosticsExitCode.INTERNAL_ERROR
    assert diagnostics._render(report, "console") == "other: failed (1ms)"
