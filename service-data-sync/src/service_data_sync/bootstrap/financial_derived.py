"""平台派生财务指标的运行账本与应用组合根。

该模块把计算服务包在可恢复的 `SyncRun` 账本中：输入仍来自已发布的上游财务版本，输出
则携带独立公式血缘，避免同步失败或重跑时留下无法判断状态的派生结果。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update

from service_data_sync.application.financial.derived import (
    FinancialDerivationResult,
    FinancialDerivedMetricService,
)
from service_data_sync.domain.equity import Exchange
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.execution.sync_run import SyncRun
from service_data_sync.infrastructure.persistence.financial_derived_repository import (
    SqlAlchemyFinancialDerivationRepository,
)

_CAPABILITY = "financial.derived-metric"


def run_financial_derivation(
    *,
    database: DatabaseClient,
    exchange: Exchange,
    symbol: str,
    mode: str,
    request_key: str,
    run_id: UUID | None = None,
    before_final_publication: Callable[[], None] | None = None,
) -> FinancialDerivationResult:
    """登记可恢复运行，基于最新已发布报表派生指标，并写入明确终态。

    派生计算只读取已经对消费者可见的财务 publication，避免 candidate 或 research
    数据混入结果。运行账本与计算分开提交：即使进程异常退出，运维也能按 `run_id`
    识别未完成或失败的请求并安全重试。
    """
    started_at = datetime.now(UTC)
    if run_id is None:
        run_id = _start_run(
            database,
            mode=mode,
            request_key=request_key,
            started_at=started_at,
        )
    else:
        run_id = _start_run(
            database,
            mode=mode,
            request_key=request_key,
            started_at=started_at,
            requested_run_id=run_id,
        )
    try:
        service = FinancialDerivedMetricService(
            repository=SqlAlchemyFinancialDerivationRepository(database)
        )
        if before_final_publication is None:
            result = service.derive(
                exchange=exchange,
                symbol=symbol,
                derivation_run_id=run_id,
                computed_at=started_at,
            )
        else:
            result = service.derive(
                exchange=exchange,
                symbol=symbol,
                derivation_run_id=run_id,
                computed_at=started_at,
                before_final_publication=before_final_publication,
            )
    except Exception:
        # 先落失败终态再透传原异常，避免 worker 重启后把已失败请求误判为仍在执行。
        _finish_run(database, run_id=run_id, status="failed")
        raise
    return result


def _start_run(
    database: DatabaseClient,
    *,
    mode: str,
    request_key: str,
    started_at: datetime,
    requested_run_id: UUID | None = None,
) -> UUID:
    """创建或重启同一幂等请求的运行账本，并返回稳定 `run_id`。

    `request_key` 加 capability 前缀后成为跨任务唯一键；同一人工、调度或回填请求
    重跑时复用已有账本，而不是制造多条互相矛盾的运行记录。
    """
    if mode not in {"manual", "scheduled", "backfill"}:
        raise ValueError("unsupported financial derivation mode")
    stable_request_key = f"{_CAPABILITY}:{request_key}"
    with database.transaction() as connection:
        # 锁住同一 request_key，防止两个 worker 同时都看到“尚未创建”而各写一条运行记录。
        current = connection.execute(
            select(SyncRun.run_id)
            .where(SyncRun.request_key == stable_request_key)
            .with_for_update()
        ).scalar_one_or_none()
        if current is None:
            run_id = requested_run_id or uuid4()
            connection.execute(
                insert(SyncRun).values(
                    run_id=run_id,
                    capability=_CAPABILITY,
                    mode=mode,
                    request_key=stable_request_key,
                    target_date=None,
                    status="running",
                    requested_at=started_at,
                    started_at=started_at,
                    finished_at=None,
                    created_at=started_at,
                )
            )
            return run_id
        run_id = UUID(str(current))
        if requested_run_id is not None and run_id != requested_run_id:
            raise RuntimeError("financial derivation request key belongs to another run")
        connection.execute(
            update(SyncRun)
            .where(SyncRun.run_id == run_id)
            .values(
                mode=mode,
                status="running",
                started_at=started_at,
                finished_at=None,
            )
        )
        return run_id


def _finish_run(database: DatabaseClient, *, run_id: UUID, status: str) -> None:
    """把派生运行收敛为成功或失败终态；未知或已结束的 `run_id` 视为账本破坏。

    更新条件要求 capability 与 `running` 状态同时匹配，避免迟到的 worker 回写其他
    任务、或把已被恢复流程接管的运行记录覆盖为错误终态。
    """
    if status not in {"succeeded", "failed"}:
        raise ValueError("unsupported financial derivation terminal status")
    with database.transaction() as connection:
        updated_run_id = connection.execute(
            update(SyncRun)
            .where(
                SyncRun.run_id == run_id,
                SyncRun.capability == _CAPABILITY,
                SyncRun.status == "running",
            )
            .values(status=status, finished_at=datetime.now(UTC))
            .returning(SyncRun.run_id)
        ).scalar_one_or_none()
        if updated_run_id != run_id:
            raise RuntimeError("financial derivation run ledger is inconsistent")
