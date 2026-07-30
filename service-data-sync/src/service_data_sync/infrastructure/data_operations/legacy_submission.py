"""将保留的 CLI、Celery 和恢复入口收敛为 command 提交。

兼容入口只能把既有受限业务参数转换为合同 SyncTarget；它们不能直接选择 Provider、调用
同步 use case 或取得全局执行槽。真正抓取与发布只发生在数据运维 dispatcher。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
)


def system_command_group_identity(
    *,
    targets: list[dict[str, Any]],
    request_prefix: str,
    intents: list[dict[str, Any]] | None = None,
) -> tuple[str, UUID]:
    """只计算内部 command 组的规范摘要与稳定 submission UUID，不产生外部副作用。"""
    if not targets:
        raise ValueError("system command group targets must not be empty")
    dataset_codes = [str(target.get("datasetCode", "")) for target in targets]
    if len(set(dataset_codes)) != len(dataset_codes):
        raise ValueError("system command group dataset codes must be unique")
    resolved_intents = intents or [{"kind": "STANDARD"} for _target in targets]
    if len(resolved_intents) != len(targets):
        raise ValueError("system command group intents must align with targets")
    fingerprint = hashlib.sha256(
        json.dumps(
            {"targets": targets, "intents": resolved_intents},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return fingerprint, uuid5(NAMESPACE_URL, f"quant-v2:{request_prefix}:{fingerprint}")


def system_command_identity(
    *,
    target: dict[str, Any],
    request_prefix: str,
    intent: dict[str, Any] | None = None,
) -> tuple[str, UUID]:
    """计算单目标内部 command 的规范摘要与稳定 submission UUID，不产生提交副作用。"""
    legacy_intent = intent or {"kind": "STANDARD"}
    fingerprint = hashlib.sha256(
        json.dumps(
            {"target": target, "intent": legacy_intent},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return fingerprint, uuid5(NAMESPACE_URL, f"quant-v2:{request_prefix}:{fingerprint}")


def submit_system_command(
    control_plane: DataOperationsControlPlane,
    *,
    target: dict[str, Any],
    reason: str,
    request_prefix: str,
    intent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """以仅 Python 可用的 SYSTEM 兼容层提交 command，并冻结私有 LegacyExecutionIntent。"""
    legacy_intent = intent or {"kind": "STANDARD"}
    fingerprint, submission_id = system_command_identity(
        target=target,
        intent=legacy_intent,
        request_prefix=request_prefix,
    )
    return control_plane.submit_system_legacy_command(
        targets=[target],
        intents=[legacy_intent],
        reason=reason,
        idempotency_key=f"{request_prefix}:{fingerprint}",
        request_id=f"{request_prefix}:{fingerprint[:24]}",
        submission_id=submission_id,
    )


def submit_system_command_group(
    control_plane: DataOperationsControlPlane,
    *,
    targets: list[dict[str, Any]],
    reason: str,
    request_prefix: str,
    intents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """幂等提交一个数据集代码互异的内部目标组，供父级回填计划聚合单证券能力。"""
    legacy_intents = intents or [{"kind": "STANDARD"} for _target in targets]
    fingerprint, submission_id = system_command_group_identity(
        targets=targets,
        intents=legacy_intents,
        request_prefix=request_prefix,
    )
    return control_plane.submit_system_legacy_command(
        targets=targets,
        intents=legacy_intents,
        reason=reason,
        idempotency_key=f"{request_prefix}:{fingerprint}",
        request_id=f"{request_prefix}:{fingerprint[:24]}",
        submission_id=submission_id,
    )
