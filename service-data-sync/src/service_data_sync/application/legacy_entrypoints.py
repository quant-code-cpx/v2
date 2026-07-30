"""尚未接入 fenced dispatcher 的历史同步入口统一拒绝策略。

历史 CLI、Celery、重放和回滚入口曾直接调用同步用例或写入 canonical 数据。它们在具备
对应的 `DataOperationsControlPlane` 执行器前必须安全停止，不能以局部锁、旧 checkpoint
或来源重放绕过全局 execution slot。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from typing import NoReturn

LEGACY_ENTRYPOINT_UNAVAILABLE = "data-operations-legacy-entrypoint-unavailable"


class LegacyEntryPointUnavailable(RuntimeError):
    """表示历史入口没有已注册的 fenced dispatcher 执行器。

    异常文本只包含稳定错误码和入口标识，不泄漏 provider、原始 URI、checkpoint 或凭据。
    """

    def __init__(self, entrypoint: str) -> None:
        """保存机器可读错误码与受限入口名，供 CLI 和 Celery 使用同一失败语义。"""
        self.code = LEGACY_ENTRYPOINT_UNAVAILABLE
        self.entrypoint = entrypoint
        super().__init__(legacy_entrypoint_message(entrypoint))


def legacy_entrypoint_message(entrypoint: str) -> str:
    """构造固定、可审计且不含敏感运行时细节的停用原因。"""
    return f"{LEGACY_ENTRYPOINT_UNAVAILABLE}: {entrypoint}"


def reject_legacy_cli(
    *, entrypoint: str, argv: Sequence[str] | None, description: str | None
) -> NoReturn:
    """保留 CLI 帮助入口后拒绝所有旧执行参数，避免构造容器或访问任何来源。"""
    parser = argparse.ArgumentParser(prog=entrypoint, description=description)
    # 保留旧运维脚本的参数兼容性；入口已停用，不能把参数解释为新的未审核执行语义。
    parser.parse_known_args(argv)
    raise SystemExit(legacy_entrypoint_message(entrypoint))


def reject_legacy_task(entrypoint: str) -> NoReturn:
    """使旧 Celery 消息显式失败，避免重试路径直接调用 canonical 同步用例。"""
    raise LegacyEntryPointUnavailable(entrypoint)
