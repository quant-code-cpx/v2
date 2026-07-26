"""本地依赖诊断 CLI，输出稳定且不含密钥的健康状态。"""

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
    """诊断 CLI 对外承诺的稳定退出码。"""

    OK = 0
    INTERNAL_ERROR = 1
    CONFIGURATION_ERROR = 2
    POSTGRES_UNAVAILABLE = 3
    REDIS_UNAVAILABLE = 4
    S3_UNAVAILABLE = 5
    MULTIPLE_DEPENDENCIES_UNAVAILABLE = 6


@dataclass(frozen=True)
class CheckResult:
    """一项基础设施探测的成功状态、耗时与安全错误类型。"""

    dependency: str
    ok: bool
    duration_ms: int
    error_type: str | None = None


@dataclass(frozen=True)
class DiagnosticsReport:
    """汇总全部依赖探测结果，并负责转换为 CLI 输出。"""

    results: tuple[CheckResult, ...]

    @property
    def exit_code(self) -> DiagnosticsExitCode:
        """将各项探测结果归并为稳定的 CLI 退出码。"""
        failed = {result.dependency for result in self.results if not result.ok}
        if not failed:
            return DiagnosticsExitCode.OK
        if failed == {"configuration"}:
            return DiagnosticsExitCode.CONFIGURATION_ERROR
        # 多项依赖同时失败不能伪装成任意一种单依赖故障。
        if len(failed) > 1:
            return DiagnosticsExitCode.MULTIPLE_DEPENDENCIES_UNAVAILABLE
        return {
            "postgres": DiagnosticsExitCode.POSTGRES_UNAVAILABLE,
            "redis": DiagnosticsExitCode.REDIS_UNAVAILABLE,
            "s3": DiagnosticsExitCode.S3_UNAVAILABLE,
        }.get(next(iter(failed)), DiagnosticsExitCode.INTERNAL_ERROR)

    def as_dict(self) -> dict[str, Any]:
        """将报告序列化为字段顺序稳定、可机器读取的诊断载荷。"""
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
    """并发执行相互独立的依赖探测，并归类失败或超时。"""
    started_at = {name: time.monotonic() for name in checks}
    executor = ThreadPoolExecutor(max_workers=len(checks), thread_name_prefix="dependency-check")
    futures: dict[Future[None], str] = {
        executor.submit(check): name for name, check in checks.items()
    }
    done, pending = wait(futures, timeout=timeout_seconds)
    results: list[CheckResult] = []

    for future, dependency in futures.items():
        duration_ms = int((time.monotonic() - started_at[dependency]) * 1000)
        # 取消线程池任务无法中止已运行 I/O；仍应立即报告超时，不能继续等待。
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
    """以对外文档约定的稳定名称暴露依赖探测函数。"""
    return {
        "postgres": container.database.ping,
        "redis": container.broker.ping,
        "s3": container.object_storage.ping,
    }


def diagnose(settings: Settings) -> DiagnosticsReport:
    """创建临时容器、执行全部探测，并始终释放客户端。"""
    container = build_container(settings)
    try:
        return run_diagnostics(
            _checks(container),
            timeout_seconds=settings.diagnostics_timeout_seconds,
        )
    finally:
        container.close()


def _render(report: DiagnosticsReport, output_format: str) -> str:
    """将诊断结果渲染为稳定 JSON 或简洁的人工可读行。"""
    if output_format == "json":
        return json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True)
    lines = [
        f"{result.dependency}: {'ok' if result.ok else 'failed'} ({result.duration_ms}ms)"
        for result in report.results
    ]
    return "\n".join(lines)


def _configuration_report() -> DiagnosticsReport:
    """配置无法加载时创建不泄漏细节的失败报告。"""
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
    """解析 CLI 输出格式，测试时不绑定进程参数。"""
    parser = argparse.ArgumentParser(description="检查 service-data-sync 基础设施依赖。")
    parser.add_argument("--format", choices=("console", "json"), default="console")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """运行诊断 CLI，返回文档约定退出码且不暴露密钥。"""
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
