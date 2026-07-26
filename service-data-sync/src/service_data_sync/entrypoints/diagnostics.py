from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from enum import IntEnum
from typing import Any

from service_data_sync.bootstrap.container import ServiceContainer, build_container
from service_data_sync.bootstrap.errors import ConfigurationError
from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import Settings, load_settings


class DiagnosticsExitCode(IntEnum):
    OK = 0
    INTERNAL_ERROR = 1
    CONFIGURATION_ERROR = 2
    POSTGRES_UNAVAILABLE = 3
    REDIS_UNAVAILABLE = 4
    S3_UNAVAILABLE = 5
    MULTIPLE_DEPENDENCIES_UNAVAILABLE = 6


@dataclass(frozen=True)
class CheckResult:
    dependency: str
    ok: bool
    duration_ms: int
    error_type: str | None = None


@dataclass(frozen=True)
class DiagnosticsReport:
    results: tuple[CheckResult, ...]

    @property
    def exit_code(self) -> DiagnosticsExitCode:
        """Collapse check outcomes into stable CLI exit code."""
        failed = {result.dependency for result in self.results if not result.ok}
        if not failed:
            return DiagnosticsExitCode.OK
        if failed == {"configuration"}:
            return DiagnosticsExitCode.CONFIGURATION_ERROR
        # A mixed failure must not masquerade as any single dependency outage.
        if len(failed) > 1:
            return DiagnosticsExitCode.MULTIPLE_DEPENDENCIES_UNAVAILABLE
        return {
            "postgres": DiagnosticsExitCode.POSTGRES_UNAVAILABLE,
            "redis": DiagnosticsExitCode.REDIS_UNAVAILABLE,
            "s3": DiagnosticsExitCode.S3_UNAVAILABLE,
        }.get(next(iter(failed)), DiagnosticsExitCode.INTERNAL_ERROR)

    def as_dict(self) -> dict[str, Any]:
        """Serialize report into deterministic machine-readable diagnostics payload."""
        return {
            "ok": self.exit_code is DiagnosticsExitCode.OK,
            "exit_code": int(self.exit_code),
            "results": [asdict(result) for result in self.results],
        }


def run_diagnostics(
    checks: Mapping[str, Callable[[], None]],
    *,
    timeout_seconds: int,
) -> DiagnosticsReport:
    """Run independent dependency checks concurrently and classify failures or timeout."""
    started_at = {name: time.monotonic() for name in checks}
    executor = ThreadPoolExecutor(max_workers=len(checks), thread_name_prefix="dependency-check")
    futures: dict[Future[None], str] = {
        executor.submit(check): name for name, check in checks.items()
    }
    done, pending = wait(futures, timeout=timeout_seconds)
    results: list[CheckResult] = []

    for future, dependency in futures.items():
        duration_ms = int((time.monotonic() - started_at[dependency]) * 1000)
        # Executor cancellation cannot stop running I/O, but report timeout without waiting for it.
        if future in pending:
            future.cancel()
            results.append(
                CheckResult(
                    dependency=dependency,
                    ok=False,
                    duration_ms=duration_ms,
                    error_type="TimeoutError",
                )
            )
            continue
        try:
            future.result()
        except Exception as error:
            results.append(
                CheckResult(
                    dependency=dependency,
                    ok=False,
                    duration_ms=duration_ms,
                    error_type=type(error).__name__,
                )
            )
        else:
            results.append(CheckResult(dependency=dependency, ok=True, duration_ms=duration_ms))

    executor.shutdown(wait=False, cancel_futures=True)
    return DiagnosticsReport(results=tuple(results))


def _checks(container: ServiceContainer) -> dict[str, Callable[[], None]]:
    """Expose dependency probes under stable externally documented names."""
    return {
        "postgres": container.database.ping,
        "redis": container.broker.ping,
        "s3": container.object_storage.ping,
    }


def diagnose(settings: Settings) -> DiagnosticsReport:
    """Build temporary container, run all probes, and always release its clients."""
    container = build_container(settings)
    try:
        return run_diagnostics(
            _checks(container),
            timeout_seconds=settings.diagnostics_timeout_seconds,
        )
    finally:
        container.close()


def _render(report: DiagnosticsReport, output_format: str) -> str:
    """Render diagnostics as stable JSON or concise human-readable lines."""
    if output_format == "json":
        return json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True)
    lines = [
        f"{result.dependency}: {'ok' if result.ok else 'failed'} ({result.duration_ms}ms)"
        for result in report.results
    ]
    return "\n".join(lines)


def _configuration_report() -> DiagnosticsReport:
    """Create safe failure report when settings could not be loaded."""
    return DiagnosticsReport(
        results=(
            CheckResult(
                dependency="configuration",
                ok=False,
                duration_ms=0,
                error_type="ConfigurationError",
            ),
        )
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI output-format option without binding process arguments in tests."""
    parser = argparse.ArgumentParser(
        description="Check service-data-sync infrastructure dependencies."
    )
    parser.add_argument("--format", choices=("console", "json"), default="console")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run diagnostics CLI and return documented status code without exposing secrets."""
    args = parse_args(argv)
    try:
        settings = load_settings()
    except ConfigurationError:
        report = _configuration_report()
        print(_render(report, args.format))
        return int(DiagnosticsExitCode.CONFIGURATION_ERROR)

    configure_logging(settings, process_role="diagnostics")
    report = diagnose(settings)
    print(_render(report, args.format))
    return int(report.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
