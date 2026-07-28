"""平台派生财务指标的运行账本与应用组合根。"""

from __future__ import annotations

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
) -> FinancialDerivationResult:
    """登记可恢复运行，执行最新已发布报表派生，并把运行写入明确终态。"""
    started_at = datetime.now(UTC)
    run_id = _start_run(
        database,
        mode=mode,
        request_key=request_key,
        started_at=started_at,
    )
    try:
        result = FinancialDerivedMetricService(
            repository=SqlAlchemyFinancialDerivationRepository(database)
        ).derive(
            exchange=exchange,
            symbol=symbol,
            derivation_run_id=run_id,
            computed_at=started_at,
        )
    except Exception:
        # 先落失败终态再透传原异常，避免 worker 丢失后留下永久 running 账本。
        _finish_run(database, run_id=run_id, status="failed")
        raise
    _finish_run(database, run_id=run_id, status="succeeded")
    return result


def _start_run(
    database: DatabaseClient,
    *,
    mode: str,
    request_key: str,
    started_at: datetime,
) -> UUID:
    """创建或重启同一幂等请求的运行账本，并返回稳定 run_id。"""
    if mode not in {"manual", "scheduled", "backfill"}:
        raise ValueError("unsupported financial derivation mode")
    stable_request_key = f"{_CAPABILITY}:{request_key}"
    with database.transaction() as connection:
        current = connection.execute(
            select(SyncRun.run_id)
            .where(SyncRun.request_key == stable_request_key)
            .with_for_update()
        ).scalar_one_or_none()
        if current is None:
            run_id = uuid4()
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
    """把派生运行收敛为成功或失败终态；未知 run_id 视为账本破坏。"""
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
