"""资金流同步对共享 run/partition 账本的 checkpoint 与恢复操作。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from service_data_sync.application.money_flow.sync import MoneyFlowSyncResult
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.execution.sync_partition import (
    SyncPartition,
)
from service_data_sync.infrastructure.database.models.execution.sync_run import (
    SyncRun,
)

_LEASE_DURATION = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class MoneyFlowRun:
    """携带一次可恢复资金流分区的 run 身份和 fencing owner。"""

    run_id: UUID
    partition_key: str
    lease_owner: str
    attempt: int


class SqlAlchemyMoneyFlowRunLedger:
    """使用共享执行账本维护幂等请求、租约、checkpoint 和稳定错误码。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存短生命周期数据库会话工厂。"""
        self._database = database

    def start(
        self,
        *,
        capability: str,
        parameters: tuple[tuple[str, str], ...],
        mode: str,
    ) -> MoneyFlowRun:
        """创建或接管一个稳定请求分区，保留旧 checkpoint 供幂等恢复。"""
        if mode not in {"manual", "scheduled", "backfill"}:
            raise ValueError("money-flow run mode is invalid")
        canonical_parameters = tuple(sorted(parameters))
        request_digest = hashlib.sha256(
            json.dumps(
                [capability, canonical_parameters],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        request_key = f"money-flow:{request_digest}"
        partition_key = f"request:{request_digest}"
        now = datetime.now(UTC)
        lease_owner = f"money-flow:{uuid4()}"
        lease_until = now + _LEASE_DURATION
        target_date = _target_date(canonical_parameters)
        with self._database.transaction() as session:
            run_insert = postgresql_insert(SyncRun).values(
                run_id=uuid4(),
                capability=capability,
                mode=mode,
                request_key=request_key,
                target_date=target_date,
                status="running",
                requested_at=now,
                started_at=now,
                finished_at=None,
                created_at=now,
            )
            run_id = UUID(
                str(
                    session.execute(
                        run_insert.on_conflict_do_update(
                            index_elements=[SyncRun.request_key],
                            set_={
                                "mode": mode,
                                "status": "running",
                                "started_at": now,
                                "finished_at": None,
                            },
                        ).returning(SyncRun.run_id)
                    ).scalar_one()
                )
            )
            existing = (
                session.execute(
                    select(
                        SyncPartition.attempt,
                        SyncPartition.lease_until,
                    )
                    .where(
                        SyncPartition.run_id == run_id,
                        SyncPartition.partition_key == partition_key,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if (
                existing is not None
                and existing["lease_until"] is not None
                and existing["lease_until"] > now
            ):
                raise RuntimeError("money-flow partition is already leased")
            attempt = 1 if existing is None else int(existing["attempt"]) + 1
            partition_insert = postgresql_insert(SyncPartition).values(
                run_id=run_id,
                partition_key=partition_key,
                status="running",
                attempt=attempt,
                lease_owner=lease_owner,
                lease_until=lease_until,
                heartbeat_at=now,
                next_retry_at=None,
                checkpoint_json={"stage": "requested"},
                error_code=None,
                updated_at=now,
            )
            session.execute(
                partition_insert.on_conflict_do_update(
                    index_elements=[
                        SyncPartition.run_id,
                        SyncPartition.partition_key,
                    ],
                    set_={
                        "status": "running",
                        "attempt": attempt,
                        "lease_owner": lease_owner,
                        "lease_until": lease_until,
                        "heartbeat_at": now,
                        "next_retry_at": None,
                        # 恢复时保留旧 raw/dataVersion 指针；新结果完成后整体替换。
                        "checkpoint_json": SyncPartition.checkpoint_json,
                        "error_code": None,
                        "updated_at": now,
                    },
                )
            )
        return MoneyFlowRun(
            run_id=run_id,
            partition_key=partition_key,
            lease_owner=lease_owner,
            attempt=attempt,
        )

    def finish(self, *, run: MoneyFlowRun, result: MoneyFlowSyncResult) -> None:
        """原子写入 raw 和 publication checkpoint，并释放当前 fencing 租约。"""
        now = datetime.now(UTC)
        status = "succeeded" if result.publication.quality_status == "passed" else "partial"
        checkpoint = {
            "stage": ("published" if result.publication.published else "canonical-or-raw-stored"),
            "capability": result.capability,
            "sourcePayloadSha256": result.source_payload_sha256,
            "rawUri": result.raw_uri,
            "dataVersion": (
                None
                if result.publication.data_version is None
                else str(result.publication.data_version)
            ),
            "published": result.publication.published,
            "qualityStatus": result.publication.quality_status,
            "insertedCount": result.publication.inserted_count,
            "revisedCount": result.publication.revised_count,
            "unchangedCount": result.publication.unchanged_count,
        }
        with self._database.transaction() as session:
            changed = session.execute(
                update(SyncPartition)
                .where(
                    SyncPartition.run_id == run.run_id,
                    SyncPartition.partition_key == run.partition_key,
                    SyncPartition.lease_owner == run.lease_owner,
                    SyncPartition.status == "running",
                )
                .values(
                    status=status,
                    lease_owner=None,
                    lease_until=None,
                    heartbeat_at=now,
                    next_retry_at=None,
                    checkpoint_json=checkpoint,
                    error_code=None,
                    updated_at=now,
                )
            )
            if getattr(changed, "rowcount", 1) == 0:
                raise RuntimeError("money-flow run lease is no longer active")
            session.execute(
                update(SyncRun)
                .where(SyncRun.run_id == run.run_id)
                .values(status=status, finished_at=now)
            )

    def fail(
        self,
        *,
        run: MoneyFlowRun,
        error_code: str,
        retryable: bool,
    ) -> None:
        """保存稳定失败码；可重试失败进入 partial，后续同请求可安全接管。"""
        if not error_code or len(error_code) > 64:
            raise ValueError("money-flow error code is invalid")
        now = datetime.now(UTC)
        status = "partial" if retryable else "failed"
        with self._database.transaction() as session:
            changed = session.execute(
                update(SyncPartition)
                .where(
                    SyncPartition.run_id == run.run_id,
                    SyncPartition.partition_key == run.partition_key,
                    SyncPartition.lease_owner == run.lease_owner,
                    SyncPartition.status == "running",
                )
                .values(
                    status=status,
                    lease_owner=None,
                    lease_until=None,
                    heartbeat_at=now,
                    next_retry_at=(now + timedelta(minutes=1) if retryable else None),
                    checkpoint_json={
                        "stage": "failed",
                        "errorCode": error_code,
                    },
                    error_code=error_code,
                    updated_at=now,
                )
            )
            if getattr(changed, "rowcount", 1) == 0:
                raise RuntimeError("money-flow run lease is no longer active")
            session.execute(
                update(SyncRun)
                .where(SyncRun.run_id == run.run_id)
                .values(status=status, finished_at=now)
            )


def _target_date(
    parameters: tuple[tuple[str, str], ...],
) -> date | None:
    """从目标排行日期读取账本日期；日历史窗口不伪装单一 targetDate。"""
    values = dict(parameters)
    target = values.get("targetDate")
    return None if target is None else date.fromisoformat(target)


__all__ = [
    "MoneyFlowRun",
    "SqlAlchemyMoneyFlowRunLedger",
]
