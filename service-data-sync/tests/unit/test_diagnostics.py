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
    def fail() -> None:
        raise ConnectionError("not exposed")

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
    def fail() -> None:
        raise ConnectionError

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
    calls: list[str] = []

    class FakeContainer:
        def close(self) -> None:
            calls.append("closed")

        class database:
            @staticmethod
            def ping() -> None:
                return None

        class broker:
            @staticmethod
            def ping() -> None:
                return None

        class object_storage:
            @staticmethod
            def ping() -> None:
                return None

    monkeypatch.setattr(diagnostics, "build_container", lambda _settings: FakeContainer())

    report = diagnostics.diagnose(load_settings())

    assert report.exit_code is DiagnosticsExitCode.OK
    assert calls == ["closed"]


def test_main_renders_a_safe_configuration_error(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def raise_configuration_error() -> None:
        raise ConfigurationError("sensitive details are chained")

    monkeypatch.setattr(diagnostics, "load_settings", raise_configuration_error)

    assert main(["--format", "json"]) == DiagnosticsExitCode.CONFIGURATION_ERROR

    rendered = json.loads(capsys.readouterr().out)
    assert rendered["exit_code"] == DiagnosticsExitCode.CONFIGURATION_ERROR
    assert rendered["results"][0]["dependency"] == "configuration"


def test_report_renders_console_and_unknown_failure() -> None:
    report = DiagnosticsReport(
        results=(CheckResult("other", ok=False, duration_ms=1, error_type="RuntimeError"),)
    )

    assert report.exit_code is DiagnosticsExitCode.INTERNAL_ERROR
    assert diagnostics._render(report, "console") == "other: failed (1ms)"
