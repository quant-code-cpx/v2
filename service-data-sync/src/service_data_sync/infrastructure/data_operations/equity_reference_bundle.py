"""生成股票中心全量回填依赖的真实当前态引用 bundle。

编排器只通过数据运维控制面执行七个已登记数据集，随后把每一步在其 run 终点实际可见的
publication、真实 `SourceBatch` 和日期边界原子封印为 canonical bundle。崩溃重启复用
数据库 attempt/step；跨越上海午夜则滚转旧 attempt 并从第一步重新生成，绝不混合日期。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from service_data_sync.application.ports.canonical_release import (
    CanonicalLineageRecord,
    CanonicalQualityDecision,
    CanonicalQualityRule,
    CanonicalReleaseCandidate,
)
from service_data_sync.application.ports.trading_calendar import TradingCalendarPort
from service_data_sync.application.sector.eod_schedule import sector_eod_source_cutoff_at
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
)
from service_data_sync.infrastructure.data_operations.equity_backfill import (
    FrozenReferenceBundle,
)
from service_data_sync.infrastructure.data_operations.legacy_submission import (
    submit_system_command,
    system_command_identity,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import (
    CanonicalCheckpoint,
    CanonicalDataset,
    CanonicalRecordLineage,
    MethodologyVersion,
    NormalizationRun,
)
from service_data_sync.infrastructure.database.models.equity.backfill import (
    EquityReferenceGenerationAttempt,
    EquityReferenceGenerationStep,
)
from service_data_sync.infrastructure.database.models.equity.identity.equity_master_snapshot import (  # noqa: E501
    EquityMasterSnapshot,
)
from service_data_sync.infrastructure.database.models.equity.workspace.sw_membership_release import (  # noqa: E501
    SwMembershipRelease,
)
from service_data_sync.infrastructure.database.models.execution.sync_run import SyncRun
from service_data_sync.infrastructure.database.models.operations import (
    DataOperationCommand,
    DataOperationRun,
    DataOperationRunSourceBatch,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.database.models.publication.dataset_publication_component import (  # noqa: E501
    DatasetPublicationComponent,
)
from service_data_sync.infrastructure.database.models.sector.membership.sector_membership_release import (  # noqa: E501
    SectorMembershipRelease,
)
from service_data_sync.infrastructure.database.models.sector.membership.sector_membership_release_sector import (  # noqa: E501
    SectorMembershipReleaseSector,
)
from service_data_sync.infrastructure.database.models.sector.membership.sector_membership_snapshot import (  # noqa: E501
    SectorMembershipSnapshot,
)
from service_data_sync.infrastructure.database.models.sector.sw.sw_sector_node_revision import (
    SwSectorNodeRevision,
)
from service_data_sync.infrastructure.database.models.sector.sw.sw_sector_publication import (
    SwSectorPublication,
)
from service_data_sync.infrastructure.persistence.canonical_release_repository import (
    SqlAlchemyCanonicalReleaseRepository,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_BUNDLE_DATASET = "equity.workspace.reference-bundle"
_BUNDLE_PARTITION = "CN_A_REFERENCE"
_BUNDLE_MAPPING_VERSION = "equity-reference-bundle-v1"
_BUNDLE_SCHEMA_FINGERPRINT = hashlib.sha256(b"quant-v2.equity-reference-bundle.v1").hexdigest()
_MAX_RETRIES = 3
_TERMINAL_COMMANDS = frozenset({"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "REJECTED"})


def _utc_now() -> datetime:
    """返回带时区 UTC 当前时刻，供未注入测试时钟的生产编排使用。"""
    return datetime.now(UTC)


class EquityReferenceGenerationError(RuntimeError):
    """表示引用刷新无法形成可封印 bundle，数据库仍保留精确恢复或失败状态。"""


class EquityReferenceGenerationPending(EquityReferenceGenerationError):
    """表示等待时间预算已用尽但 attempt 仍可由同一入口安全恢复。"""


class EquityReferenceBoundaryRolled(EquityReferenceGenerationError):
    """表示执行期间跨越上海自然日，调用方应创建下一 attempt 全量重跑。"""


class EquityReferenceBundleOrchestrator:
    """串行执行真实引用数据命令，并原子封印可被历史回填计划消费的 bundle。"""

    def __init__(
        self,
        *,
        database: DatabaseClient,
        control_plane: DataOperationsControlPlane,
        trading_calendar: TradingCalendarPort,
        now: Callable[[], datetime] | None = None,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        """保存权威数据库、统一控制面、交易日历和可测试时钟。"""
        self._database = database
        self._control_plane = control_plane
        self._trading_calendar = trading_calendar
        self._now = now or _utc_now
        self._poll_interval_seconds = max(0.0, poll_interval_seconds)
        self._release_repository = SqlAlchemyCanonicalReleaseRepository(database)

    def run_until_sealed(
        self,
        *,
        campaign_key: str,
        worker_id: str,
        max_wait_seconds: float = 7200,
    ) -> FrozenReferenceBundle:
        """创建或恢复 attempt，自动重试可重试失败，并返回真实封印 bundle。"""
        normalized_campaign = campaign_key.strip()
        if not normalized_campaign or len(normalized_campaign) > 128:
            raise ValueError("reference campaign key is invalid")
        deadline = time.monotonic() + max(1.0, max_wait_seconds)
        for _rollover in range(3):
            snapshot_observed_on, market_as_of = self._boundaries(self._now())
            attempt = self._ensure_attempt(
                campaign_key=normalized_campaign,
                snapshot_observed_on=snapshot_observed_on,
                market_as_of=market_as_of,
            )
            if attempt.status == "SEALED":
                return self._frozen_bundle(attempt)
            try:
                self._run_attempt(
                    attempt_id=attempt.attempt_id,
                    worker_id=worker_id,
                    deadline=deadline,
                )
                return self._seal_attempt(attempt.attempt_id)
            except EquityReferenceBoundaryRolled:
                continue
            except EquityReferenceGenerationPending:
                raise
            except Exception as error:
                self._fail_attempt(attempt.attempt_id, error)
                if isinstance(error, EquityReferenceGenerationError):
                    raise
                raise EquityReferenceGenerationError(
                    "equity reference generation failed"
                ) from error
        raise EquityReferenceGenerationPending(
            "equity reference generation crossed too many Shanghai date boundaries"
        )

    def _boundaries(self, now: datetime) -> tuple[date, date]:
        """解析上海自然日与最近达到 16:15 截点的权威开市日，未知日历立即失败。"""
        if now.tzinfo is None:
            raise ValueError("reference generation clock must include a timezone")
        snapshot_observed_on = now.astimezone(_SHANGHAI).date()
        candidate = snapshot_observed_on
        for _offset in range(32):
            state = self._trading_calendar.is_open(trade_date=candidate)
            if state is None:
                raise EquityReferenceGenerationError(
                    "authoritative trading calendar is unavailable"
                )
            if state and sector_eod_source_cutoff_at(candidate) <= now:
                return snapshot_observed_on, candidate
            candidate -= timedelta(days=1)
        raise EquityReferenceGenerationError(
            "recent complete authoritative trading day is unavailable"
        )

    def _ensure_attempt(
        self,
        *,
        campaign_key: str,
        snapshot_observed_on: date,
        market_as_of: date,
    ) -> EquityReferenceGenerationAttempt:
        """以事务级 advisory lock 复用同日 attempt，跨日时只滚转不覆盖。"""
        now = self._now()
        with self._database.transaction() as session:
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"equity-reference:{campaign_key}"},
            )
            building = session.execute(
                select(EquityReferenceGenerationAttempt)
                .where(
                    EquityReferenceGenerationAttempt.campaign_key == campaign_key,
                    EquityReferenceGenerationAttempt.status == "BUILDING",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if building is not None:
                if (
                    building.snapshot_observed_on == snapshot_observed_on
                    and building.market_as_of == market_as_of
                ):
                    return building
                building.status = "ROLLED_FORWARD"
                building.last_error_json = {
                    "code": "reference-boundary-rolled-forward",
                    "retryable": False,
                    "previousSnapshotObservedOn": building.snapshot_observed_on.isoformat(),
                    "nextSnapshotObservedOn": snapshot_observed_on.isoformat(),
                }
                building.updated_at = now
            sealed = session.execute(
                select(EquityReferenceGenerationAttempt)
                .where(
                    EquityReferenceGenerationAttempt.campaign_key == campaign_key,
                    EquityReferenceGenerationAttempt.status == "SEALED",
                    EquityReferenceGenerationAttempt.snapshot_observed_on == snapshot_observed_on,
                    EquityReferenceGenerationAttempt.market_as_of == market_as_of,
                )
                .order_by(EquityReferenceGenerationAttempt.attempt_no.desc())
                .limit(1)
            ).scalar_one_or_none()
            if sealed is not None:
                return sealed
            attempt_no = (
                int(
                    session.scalar(
                        select(
                            func.coalesce(func.max(EquityReferenceGenerationAttempt.attempt_no), 0)
                        ).where(EquityReferenceGenerationAttempt.campaign_key == campaign_key)
                    )
                    or 0
                )
                + 1
            )
            attempt_id = uuid5(
                NAMESPACE_URL,
                (
                    "quant-v2:equity-reference:"
                    f"{campaign_key}:{attempt_no}:{snapshot_observed_on}:{market_as_of}"
                ),
            )
            attempt = EquityReferenceGenerationAttempt(
                attempt_id=attempt_id,
                campaign_key=campaign_key,
                attempt_no=attempt_no,
                snapshot_observed_on=snapshot_observed_on,
                market_as_of=market_as_of,
                status="BUILDING",
                bundle_publication_id=None,
                bundle_data_version=None,
                bundle_release_id=None,
                manifest_json=None,
                manifest_hash=None,
                source_batch_ids_json=None,
                source_batch_hash=None,
                last_error_json=None,
                created_at=now,
                updated_at=now,
                sealed_at=None,
            )
            session.add(attempt)
            for ordinal, step_key, target in _step_specs(
                snapshot_observed_on=snapshot_observed_on,
                market_as_of=market_as_of,
            ):
                request_prefix = _request_prefix(
                    campaign_key=campaign_key,
                    attempt_no=attempt_no,
                    ordinal=ordinal,
                )
                _fingerprint, submission_id = system_command_identity(
                    target=target,
                    request_prefix=request_prefix,
                )
                session.add(
                    EquityReferenceGenerationStep(
                        attempt_id=attempt_id,
                        ordinal=ordinal,
                        step_key=step_key,
                        dataset_code=str(target["datasetCode"]),
                        target_json=target,
                        submission_id=submission_id,
                        command_id=None,
                        status="HELD",
                        retry_count=0,
                        last_error_json=None,
                        output_publications_json=None,
                        source_batch_ids_json=None,
                        output_hash=None,
                        submitted_at=None,
                        finished_at=None,
                        updated_at=now,
                    )
                )
            session.flush()
            return attempt

    def _run_attempt(
        self,
        *,
        attempt_id: UUID,
        worker_id: str,
        deadline: float,
    ) -> None:
        """按固定依赖顺序恢复七个步骤，任何一步未成功都不会封印 bundle。"""
        with self._database.session() as session:
            steps = list(
                session.scalars(
                    select(EquityReferenceGenerationStep)
                    .where(EquityReferenceGenerationStep.attempt_id == attempt_id)
                    .order_by(EquityReferenceGenerationStep.ordinal)
                ).all()
            )
        if len(steps) != 7 or [step.ordinal for step in steps] != list(range(1, 8)):
            raise EquityReferenceGenerationError(
                "equity reference generation step roster is incomplete"
            )
        for step in steps:
            self._assert_attempt_boundary(attempt_id)
            if step.status == "SUCCEEDED":
                continue
            self._run_step(
                attempt_id=attempt_id,
                ordinal=step.ordinal,
                worker_id=worker_id,
                deadline=deadline,
            )
            self._assert_attempt_boundary(attempt_id)

    def _run_step(
        self,
        *,
        attempt_id: UUID,
        ordinal: int,
        worker_id: str,
        deadline: float,
    ) -> None:
        """幂等提交一个步骤，内联消费统一队列并自动重试可重试终态。"""
        while True:
            attempt, step = self._load_attempt_step(attempt_id, ordinal)
            if step.status == "SUCCEEDED":
                return
            if step.status == "FAILED":
                raise EquityReferenceGenerationError(
                    f"equity reference step failed: {step.step_key}"
                )
            if step.command_id is None:
                request_prefix = _request_prefix(
                    campaign_key=attempt.campaign_key,
                    attempt_no=attempt.attempt_no,
                    ordinal=step.ordinal,
                )
                receipt = submit_system_command(
                    self._control_plane,
                    target=step.target_json,
                    reason="股票中心引用数据真实刷新",
                    request_prefix=request_prefix,
                )
                self._bind_step_command(
                    attempt_id=attempt_id,
                    ordinal=ordinal,
                    command_id=UUID(str(receipt["commandId"])),
                )
                continue
            detail = self._control_plane.command_detail(step.command_id)
            command_status = str(detail["status"])
            if command_status == "SUCCEEDED":
                self._complete_step_success(
                    attempt_id=attempt_id,
                    ordinal=ordinal,
                    command_id=step.command_id,
                )
                return
            if command_status in _TERMINAL_COMMANDS:
                error = _command_error(detail)
                if _command_retryable(detail) and step.retry_count < _MAX_RETRIES:
                    self._retry_step(
                        attempt=attempt,
                        step=step,
                        error=error,
                    )
                    continue
                self._complete_step_failure(
                    attempt_id=attempt_id,
                    ordinal=ordinal,
                    error=error,
                )
                raise EquityReferenceGenerationError(
                    f"equity reference step exhausted retries: {step.step_key}"
                )
            self._mark_step_running(
                attempt_id=attempt_id,
                ordinal=ordinal,
                status=command_status,
            )
            if time.monotonic() >= deadline:
                raise EquityReferenceGenerationPending(
                    f"equity reference step is still {command_status}: {step.step_key}"
                )
            progressed = self._control_plane.dispatch_once(
                f"{worker_id}:equity-reference:{attempt.attempt_no}:{ordinal}"
            )
            if not progressed:
                self._control_plane.reap_expired_slots()
                if self._poll_interval_seconds:
                    time.sleep(self._poll_interval_seconds)

    def _load_attempt_step(
        self, attempt_id: UUID, ordinal: int
    ) -> tuple[EquityReferenceGenerationAttempt, EquityReferenceGenerationStep]:
        """读取 attempt 与指定步骤；缺失记录视为账本损坏而不是重新生成。"""
        with self._database.session() as session:
            attempt = session.get(EquityReferenceGenerationAttempt, attempt_id)
            step = session.get(
                EquityReferenceGenerationStep,
                {"attempt_id": attempt_id, "ordinal": ordinal},
            )
            if attempt is None or step is None:
                raise EquityReferenceGenerationError(
                    "equity reference generation ledger is incomplete"
                )
            return attempt, step

    def _bind_step_command(
        self,
        *,
        attempt_id: UUID,
        ordinal: int,
        command_id: UUID,
    ) -> None:
        """在稳定 submission 已受理后绑定 command；崩溃重放必须得到同一身份。"""
        now = self._now()
        with self._database.transaction() as session:
            step = session.get(
                EquityReferenceGenerationStep,
                {"attempt_id": attempt_id, "ordinal": ordinal},
                with_for_update=True,
            )
            if step is None:
                raise EquityReferenceGenerationError("reference step is missing")
            if step.command_id is not None and step.command_id != command_id:
                raise EquityReferenceGenerationError(
                    "reference step submission resolved to a different command"
                )
            if step.command_id is None:
                step.command_id = command_id
                step.status = "SUBMITTED"
                step.submitted_at = now
                step.updated_at = now

    def _mark_step_running(
        self,
        *,
        attempt_id: UUID,
        ordinal: int,
        status: str,
    ) -> None:
        """把控制面运行态投影到步骤账本，不推测队列完成时间。"""
        with self._database.transaction() as session:
            step = session.get(
                EquityReferenceGenerationStep,
                {"attempt_id": attempt_id, "ordinal": ordinal},
                with_for_update=True,
            )
            if step is None or step.status in {"SUCCEEDED", "FAILED"}:
                return
            step.status = "RUNNING" if status in {"RUNNING", "CANCEL_REQUESTED"} else "SUBMITTED"
            step.updated_at = self._now()

    def _retry_step(
        self,
        *,
        attempt: EquityReferenceGenerationAttempt,
        step: EquityReferenceGenerationStep,
        error: dict[str, Any],
    ) -> None:
        """复制可重试 run 为新 command，并让后续恢复只跟踪新 command。"""
        assert step.command_id is not None
        retry_no = step.retry_count + 1
        retry_submission_id = uuid5(
            NAMESPACE_URL,
            f"quant-v2:equity-reference-retry:{attempt.attempt_id}:{step.ordinal}:{retry_no}",
        )
        retry_fingerprint = hashlib.sha256(
            f"{attempt.attempt_id}:{step.ordinal}:{retry_no}:{step.command_id}".encode()
        ).hexdigest()
        receipt = self._control_plane.retry_command(
            request={
                "submissionId": str(retry_submission_id),
                "target": {
                    "resourceType": "COMMAND",
                    "resourceId": str(step.command_id),
                },
                "actor": {
                    "actorRef": "system",
                    "role": "SYSTEM",
                    "reason": "股票中心引用数据自动重试",
                },
            },
            idempotency_key=f"eqref-retry:{retry_fingerprint}",
            request_id=f"eqref-retry:{retry_fingerprint[:24]}",
        )
        with self._database.transaction() as session:
            stored = session.get(
                EquityReferenceGenerationStep,
                {"attempt_id": attempt.attempt_id, "ordinal": step.ordinal},
                with_for_update=True,
            )
            if stored is None or stored.status in {"SUCCEEDED", "FAILED"}:
                return
            if stored.retry_count >= retry_no:
                return
            stored.command_id = UUID(str(receipt["commandId"]))
            stored.status = "SUBMITTED"
            stored.retry_count = retry_no
            stored.last_error_json = error
            stored.updated_at = self._now()

    def _complete_step_success(
        self,
        *,
        attempt_id: UUID,
        ordinal: int,
        command_id: UUID,
    ) -> None:
        """在一次事务内冻结 command 终点 publication 与真实来源清单。"""
        now = self._now()
        with self._database.transaction() as session:
            attempt = session.get(
                EquityReferenceGenerationAttempt,
                attempt_id,
                with_for_update=True,
            )
            step = session.get(
                EquityReferenceGenerationStep,
                {"attempt_id": attempt_id, "ordinal": ordinal},
                with_for_update=True,
            )
            if attempt is None or step is None:
                raise EquityReferenceGenerationError("reference step ledger is incomplete")
            if step.status == "SUCCEEDED":
                return
            command = session.get(DataOperationCommand, command_id, with_for_update=True)
            runs = list(
                session.scalars(
                    select(DataOperationRun)
                    .where(DataOperationRun.command_id == command_id)
                    .order_by(DataOperationRun.target_index)
                ).all()
            )
            if (
                command is None
                or command.status != "SUCCEEDED"
                or len(runs) != 1
                or runs[0].status != "SUCCEEDED"
                or runs[0].finished_at is None
            ):
                raise EquityReferenceGenerationError(
                    "reference command is not a single successful run"
                )
            run = runs[0]
            assert run.finished_at is not None
            run_source_ids = tuple(
                sorted(
                    set(
                        session.scalars(
                            select(DataOperationRunSourceBatch.source_batch_id).where(
                                DataOperationRunSourceBatch.run_id == run.run_id
                            )
                        ).all()
                    ),
                    key=str,
                )
            )
            if not run_source_ids:
                raise EquityReferenceGenerationError(
                    "reference command has no actual SourceBatch evidence"
                )
            outputs = _capture_step_publications(
                session,
                attempt=attempt,
                step=step,
                visible_at=run.finished_at,
                run_source_ids=run_source_ids,
            )
            output_payload = {
                "publications": outputs,
                "runSourceBatchIds": [str(value) for value in run_source_ids],
            }
            step.status = "SUCCEEDED"
            step.command_id = command_id
            step.last_error_json = None
            step.output_publications_json = outputs
            step.source_batch_ids_json = [str(value) for value in run_source_ids]
            step.output_hash = _hash_json(output_payload)
            step.finished_at = now
            step.updated_at = now

    def _complete_step_failure(
        self,
        *,
        attempt_id: UUID,
        ordinal: int,
        error: dict[str, Any],
    ) -> None:
        """把已耗尽重试的步骤和 attempt 同时收敛为不可再推进的失败。"""
        now = self._now()
        with self._database.transaction() as session:
            attempt = session.get(
                EquityReferenceGenerationAttempt,
                attempt_id,
                with_for_update=True,
            )
            step = session.get(
                EquityReferenceGenerationStep,
                {"attempt_id": attempt_id, "ordinal": ordinal},
                with_for_update=True,
            )
            if attempt is None or step is None:
                return
            if step.status not in {"SUCCEEDED", "FAILED"}:
                step.status = "FAILED"
                step.last_error_json = error
                step.finished_at = now
                step.updated_at = now
            if attempt.status == "BUILDING":
                attempt.status = "FAILED"
                attempt.last_error_json = error
                attempt.updated_at = now

    def _assert_attempt_boundary(self, attempt_id: UUID) -> None:
        """每一步前后重算日期；发生变化时终止旧 attempt 并要求完整重建。"""
        with self._database.session() as session:
            attempt = session.get(EquityReferenceGenerationAttempt, attempt_id)
            if attempt is None:
                raise EquityReferenceGenerationError("reference attempt is missing")
            expected = (attempt.snapshot_observed_on, attempt.market_as_of)
        actual = self._boundaries(self._now())
        if actual == expected:
            return
        now = self._now()
        with self._database.transaction() as session:
            attempt = session.get(
                EquityReferenceGenerationAttempt,
                attempt_id,
                with_for_update=True,
            )
            if attempt is not None and attempt.status == "BUILDING":
                attempt.status = "ROLLED_FORWARD"
                attempt.last_error_json = {
                    "code": "reference-boundary-rolled-forward",
                    "retryable": False,
                    "nextSnapshotObservedOn": actual[0].isoformat(),
                    "nextMarketAsOf": actual[1].isoformat(),
                }
                attempt.updated_at = now
        raise EquityReferenceBoundaryRolled("equity reference boundary changed")

    def _seal_attempt(self, attempt_id: UUID) -> FrozenReferenceBundle:
        """把七步清单与全部来源作为 derived canonical release 原子封印。"""
        self._assert_attempt_boundary(attempt_id)
        now = self._now()
        with self._database.transaction() as session:
            attempt = session.get(
                EquityReferenceGenerationAttempt,
                attempt_id,
                with_for_update=True,
            )
            if attempt is None:
                raise EquityReferenceGenerationError("reference attempt is missing")
            if attempt.status == "SEALED":
                return self._frozen_bundle(attempt)
            if attempt.status != "BUILDING":
                raise EquityReferenceGenerationError("reference attempt is not sealable")
            steps = list(
                session.scalars(
                    select(EquityReferenceGenerationStep)
                    .where(EquityReferenceGenerationStep.attempt_id == attempt_id)
                    .order_by(EquityReferenceGenerationStep.ordinal)
                    .with_for_update()
                ).all()
            )
            if len(steps) != 7 or any(step.status != "SUCCEEDED" for step in steps):
                raise EquityReferenceGenerationError("reference attempt has incomplete steps")
            manifest = [
                component for step in steps for component in (step.output_publications_json or [])
            ]
            manifest.sort(
                key=lambda item: (
                    str(item["datasetCode"]),
                    str(item["partitionKey"]),
                )
            )
            manifest_hash = _hash_json(manifest)
            source_batch_ids = sorted(
                {
                    str(source_batch_id)
                    for component in manifest
                    for source_batch_id in component["sourceBatchIds"]
                }
            )
            source_batch_hash = _hash_json(source_batch_ids)
            temporary_bundle = FrozenReferenceBundle(
                publication_id=attempt.attempt_id,
                data_version=attempt.attempt_id,
                release_id=attempt.attempt_id,
                snapshot_observed_on=attempt.snapshot_observed_on,
                market_as_of=attempt.market_as_of,
                manifest=tuple(manifest),
                manifest_hash=manifest_hash,
            )
            temporary_bundle.validate()
            candidate = _bundle_candidate(
                session,
                attempt=attempt,
                manifest=manifest,
                manifest_hash=manifest_hash,
                created_at=now,
            )

            def write_components(
                current_session: Session,
                publication_id: UUID,
                _data_version: UUID,
                _release_id: UUID,
            ) -> None:
                """把数据集与分区共同组成的稳定键写入 aggregate 组件清单。"""
                current_session.execute(
                    insert(DatasetPublicationComponent).values(
                        [
                            {
                                "aggregate_publication_id": publication_id,
                                "component_partition_key": (
                                    f"{component['datasetCode']}|{component['partitionKey']}"
                                ),
                                "component_data_version": UUID(str(component["dataVersion"])),
                            }
                            for component in manifest
                        ]
                    )
                )

            published = self._release_repository.publish_in_session(
                session=session,
                candidate=candidate,
                write_publication=write_components,
                record_fenced_progress=False,
            )
            publication = session.execute(
                select(DatasetPublication).where(
                    DatasetPublication.data_version == published.data_version
                )
            ).scalar_one()
            frozen = FrozenReferenceBundle(
                publication_id=publication.publication_id,
                data_version=publication.data_version,
                release_id=published.release_id,
                snapshot_observed_on=attempt.snapshot_observed_on,
                market_as_of=attempt.market_as_of,
                manifest=tuple(manifest),
                manifest_hash=manifest_hash,
            )
            frozen.validate()
            attempt.status = "SEALED"
            attempt.bundle_publication_id = publication.publication_id
            attempt.bundle_data_version = publication.data_version
            attempt.bundle_release_id = published.release_id
            attempt.manifest_json = manifest
            attempt.manifest_hash = manifest_hash
            attempt.source_batch_ids_json = source_batch_ids
            attempt.source_batch_hash = source_batch_hash
            attempt.last_error_json = None
            attempt.updated_at = now
            attempt.sealed_at = now
            return frozen

    def _frozen_bundle(self, attempt: EquityReferenceGenerationAttempt) -> FrozenReferenceBundle:
        """从已封印 attempt 恢复强类型 bundle，并重新运行完整清单校验。"""
        if (
            attempt.status != "SEALED"
            or attempt.bundle_publication_id is None
            or attempt.bundle_data_version is None
            or attempt.bundle_release_id is None
            or attempt.manifest_json is None
            or attempt.manifest_hash is None
        ):
            raise EquityReferenceGenerationError("sealed reference attempt is incomplete")
        bundle = FrozenReferenceBundle(
            publication_id=attempt.bundle_publication_id,
            data_version=attempt.bundle_data_version,
            release_id=attempt.bundle_release_id,
            snapshot_observed_on=attempt.snapshot_observed_on,
            market_as_of=attempt.market_as_of,
            manifest=tuple(attempt.manifest_json),
            manifest_hash=attempt.manifest_hash,
        )
        bundle.validate()
        return bundle

    def _fail_attempt(self, attempt_id: UUID, error: Exception) -> None:
        """只在 attempt 仍可变时记录稳定失败，不覆盖已封印或已滚转事实。"""
        with self._database.transaction() as session:
            attempt = session.get(
                EquityReferenceGenerationAttempt,
                attempt_id,
                with_for_update=True,
            )
            if attempt is None or attempt.status != "BUILDING":
                return
            attempt.status = "FAILED"
            attempt.last_error_json = {
                "code": "equity-reference-generation-failed",
                "retryable": False,
                "errorType": type(error).__name__,
            }
            attempt.updated_at = self._now()


def _step_specs(
    *,
    snapshot_observed_on: date,
    market_as_of: date,
) -> tuple[tuple[int, str, dict[str, Any]], ...]:
    """生成固定七步真实控制面目标；依赖顺序即返回顺序。"""

    def target(
        dataset_code: str,
        *,
        mode: str,
        observation_date: date | None,
    ) -> dict[str, Any]:
        """构造完整标准目标，空日期字段也显式冻结。"""
        return {
            "datasetCode": dataset_code,
            "mode": mode,
            "selector": {"kind": "GLOBAL"},
            "dateFrom": None,
            "dateTo": None,
            "observationDate": (None if observation_date is None else observation_date.isoformat()),
        }

    return (
        (1, "equity-master", target("equity.master.cn-a", mode="FULL", observation_date=None)),
        (
            2,
            "equity-lifecycle",
            target("equity.lifecycle.explicit", mode="FULL", observation_date=None),
        ),
        (
            3,
            "sector-catalog",
            target(
                "sector.catalog.raw",
                mode="OBSERVATION_DATE",
                observation_date=snapshot_observed_on,
            ),
        ),
        (
            4,
            "sector-membership",
            target(
                "sector.membership.release",
                mode="OBSERVATION_DATE",
                observation_date=snapshot_observed_on,
            ),
        ),
        (
            5,
            "sw-taxonomy",
            target(
                "sector.sw.taxonomy",
                mode="OBSERVATION_DATE",
                observation_date=snapshot_observed_on,
            ),
        ),
        (
            6,
            "sw-membership",
            target(
                "sector.sw2021.membership.snapshot",
                mode="OBSERVATION_DATE",
                observation_date=snapshot_observed_on,
            ),
        ),
        (
            7,
            "trading-status",
            target(
                "equity.trading_status.1d",
                mode="OBSERVATION_DATE",
                observation_date=market_as_of,
            ),
        ),
    )


def _request_prefix(*, campaign_key: str, attempt_no: int, ordinal: int) -> str:
    """把任意长度批次键压缩为控制面允许的稳定幂等前缀。"""
    campaign_hash = hashlib.sha256(campaign_key.encode()).hexdigest()[:20]
    return f"eqref-{campaign_hash}-{attempt_no}-{ordinal}"


def _capture_step_publications(
    session: Session,
    *,
    attempt: EquityReferenceGenerationAttempt,
    step: EquityReferenceGenerationStep,
    visible_at: datetime,
    run_source_ids: tuple[UUID, ...],
) -> list[dict[str, Any]]:
    """按 command 完成时刻捕获历史可见 publication，避免随后 current 指针推进串版。"""
    component_keys = _expected_component_keys(
        session,
        attempt=attempt,
        step=step,
        visible_at=visible_at,
    )
    publications = [
        _publication_visible_at(
            session,
            dataset_code=dataset_code,
            partition_key=partition_key,
            visible_at=visible_at,
        )
        for dataset_code, partition_key in component_keys
    ]
    outputs: list[dict[str, Any]] = []
    for publication in publications:
        source_ids = _component_source_ids(
            session,
            publication=publication,
            attempt=attempt,
            run_source_ids=run_source_ids,
        )
        source_contract_hash = _source_contract_hash(session, source_ids)
        observed_on = (
            attempt.market_as_of
            if publication.dataset == "equity.trading_status.1d"
            else attempt.snapshot_observed_on
        )
        outputs.append(
            {
                "datasetCode": publication.dataset,
                "partitionKey": publication.partition_key,
                "publicationId": str(publication.publication_id),
                "dataVersion": str(publication.data_version),
                "releaseId": (
                    None if publication.release_id is None else str(publication.release_id)
                ),
                "effectiveAsOf": (
                    None
                    if publication.effective_as_of is None
                    else publication.effective_as_of.isoformat()
                ),
                "observedOn": observed_on.isoformat(),
                "sourceBatchIds": [str(value) for value in source_ids],
                "sourceContractHash": source_contract_hash,
            }
        )
    outputs.sort(key=lambda item: (str(item["datasetCode"]), str(item["partitionKey"])))
    return outputs


def _expected_component_keys(
    session: Session,
    *,
    attempt: EquityReferenceGenerationAttempt,
    step: EquityReferenceGenerationStep,
    visible_at: datetime,
) -> list[tuple[str, str]]:
    """展开每一步必须捕获的真实分区，申万成分由已捕获 taxonomy 三级节点决定。"""
    dataset_code = step.dataset_code
    if dataset_code == "equity.master.cn-a":
        return [(dataset_code, "CN_A_STABLE")]
    if dataset_code == "equity.lifecycle.explicit":
        return [(dataset_code, exchange) for exchange in ("BSE", "SSE", "SZSE")]
    if dataset_code in {"sector.catalog.raw", "sector.membership.release"}:
        return [
            (dataset_code, "eastmoney.concept"),
            (dataset_code, "eastmoney.industry"),
        ]
    if dataset_code == "sector.sw.taxonomy":
        return [
            (
                dataset_code,
                f"sw.industry:{attempt.snapshot_observed_on.isoformat()}",
            )
        ]
    if dataset_code == "equity.trading_status.1d":
        return [(dataset_code, f"date:{attempt.market_as_of.isoformat()}")]
    if dataset_code == "sector.sw2021.membership.snapshot":
        taxonomy = _publication_visible_at(
            session,
            dataset_code="sector.sw.taxonomy",
            partition_key=f"sw.industry:{attempt.snapshot_observed_on.isoformat()}",
            visible_at=visible_at,
        )
        detail = session.get(SwSectorPublication, taxonomy.data_version)
        if detail is None:
            raise EquityReferenceGenerationError("SW taxonomy publication detail is missing")
        node_codes = tuple(
            sorted(
                {
                    str(value).removesuffix(".SI")
                    for value in session.scalars(
                        select(SwSectorNodeRevision.sector_code).where(
                            SwSectorNodeRevision.snapshot_date == attempt.snapshot_observed_on,
                            SwSectorNodeRevision.methodology_id == detail.methodology_id,
                            SwSectorNodeRevision.level == 3,
                            SwSectorNodeRevision.known_to.is_(None),
                            SwSectorNodeRevision.quality_status.in_(("passed", "warned")),
                        )
                    ).all()
                }
            )
        )
        if not node_codes:
            raise EquityReferenceGenerationError(
                "SW taxonomy contains no publishable third-level nodes"
            )
        return [(dataset_code, f"SW2021:{node_code}") for node_code in node_codes]
    raise EquityReferenceGenerationError(f"unsupported equity reference dataset: {dataset_code}")


def _publication_visible_at(
    session: Session,
    *,
    dataset_code: str,
    partition_key: str,
    visible_at: datetime,
) -> DatasetPublication:
    """读取 command 完成时仍可见的唯一 publication，包括之后已被 supersede 的版本。"""
    publication = session.execute(
        select(DatasetPublication).where(
            DatasetPublication.dataset == dataset_code,
            DatasetPublication.partition_key == partition_key,
            DatasetPublication.published_at <= visible_at,
            or_(
                DatasetPublication.superseded_at.is_(None),
                DatasetPublication.superseded_at > visible_at,
            ),
        )
    ).scalar_one_or_none()
    if publication is None or publication.quality_status not in {"passed", "warned"}:
        raise EquityReferenceGenerationError(
            f"reference publication is unavailable: {dataset_code}/{partition_key}"
        )
    return publication


def _component_source_ids(
    session: Session,
    *,
    publication: DatasetPublication,
    attempt: EquityReferenceGenerationAttempt,
    run_source_ids: tuple[UUID, ...],
) -> tuple[UUID, ...]:
    """解析组件精确来源；多分区步骤不能把一个批次随意归给所有 publication。"""
    run_sources = set(run_source_ids)
    if publication.dataset == "equity.master.cn-a":
        return _require_sources(run_sources, publication)
    if publication.dataset == "equity.lifecycle.explicit":
        # 显式生命周期只报告有官方证据的事件，快照语义必为 PARTIAL；以 COMPLETE 过滤会让
        # 已发布且真实的 lifecycle component 永远无法绑定其本次 command 的来源批次。
        values = set(
            session.scalars(
                select(EquityMasterSnapshot.source_batch_id).where(
                    EquityMasterSnapshot.snapshot_kind == "LIFECYCLE",
                    EquityMasterSnapshot.exchange == publication.partition_key,
                    EquityMasterSnapshot.target_date == attempt.snapshot_observed_on,
                    EquityMasterSnapshot.quality_status.in_(("passed", "warned")),
                    EquityMasterSnapshot.source_batch_id.in_(run_sources),
                )
            ).all()
        )
        return _require_sources(values, publication)
    if publication.dataset == "sector.membership.release":
        release = session.execute(
            select(SectorMembershipRelease).where(
                SectorMembershipRelease.data_version == publication.data_version
            )
        ).scalar_one_or_none()
        if release is None:
            raise EquityReferenceGenerationError("sector membership domain release is missing")
        values = set(
            session.scalars(
                select(SectorMembershipSnapshot.source_batch_id)
                .select_from(SectorMembershipReleaseSector)
                .join(
                    SectorMembershipSnapshot,
                    SectorMembershipSnapshot.snapshot_id
                    == SectorMembershipReleaseSector.snapshot_id,
                )
                .where(SectorMembershipReleaseSector.release_id == release.release_id)
            ).all()
        )
        return _require_sources(values, publication)
    if publication.dataset == "sector.sw2021.membership.snapshot":
        if publication.release_id is None:
            raise EquityReferenceGenerationError(
                "SW membership publication has no canonical release"
            )
        value = session.scalar(
            select(SwMembershipRelease.source_batch_id).where(
                SwMembershipRelease.release_id == publication.release_id
            )
        )
        return _require_sources(set() if value is None else {value}, publication)
    if publication.dataset == "equity.trading_status.1d":
        return _require_sources(run_sources, publication)
    if publication.release_id is not None:
        values = set(
            session.scalars(
                select(CanonicalRecordLineage.source_batch_id).where(
                    CanonicalRecordLineage.release_id == publication.release_id,
                    CanonicalRecordLineage.source_batch_id.in_(run_sources),
                )
            ).all()
        )
        if values:
            return tuple(sorted(values, key=str))
    raise EquityReferenceGenerationError(
        "reference component cannot be bound to an exact command source"
    )


def _require_sources(
    values: set[UUID],
    publication: DatasetPublication,
) -> tuple[UUID, ...]:
    """要求组件至少有一个真实来源，并返回规范排序。"""
    if not values:
        raise EquityReferenceGenerationError(
            f"reference source evidence is empty: {publication.dataset}/{publication.partition_key}"
        )
    return tuple(sorted(values, key=str))


def _source_contract_hash(session: Session, source_ids: Sequence[UUID]) -> str:
    """按真实来源身份、版本、schema、载荷摘要和观察时间生成组件证据摘要。"""
    rows = session.execute(
        select(
            SourceBatch.source_batch_id,
            SourceBatch.provider_id,
            SourceBatch.capability,
            SourceBatch.upstream_source,
            SourceBatch.adapter_version,
            SourceBatch.schema_fingerprint,
            SourceBatch.payload_sha256,
            SourceBatch.observed_at,
        )
        .where(SourceBatch.source_batch_id.in_(source_ids))
        .order_by(SourceBatch.source_batch_id)
    ).all()
    if len(rows) != len(set(source_ids)):
        raise EquityReferenceGenerationError("reference SourceBatch descriptor is incomplete")
    return _hash_json(
        [
            {
                "sourceBatchId": str(row.source_batch_id),
                "providerId": row.provider_id,
                "capability": row.capability,
                "upstreamSource": row.upstream_source,
                "adapterVersion": row.adapter_version,
                "schemaFingerprint": row.schema_fingerprint,
                "payloadSha256": row.payload_sha256,
                "observedAt": row.observed_at.isoformat(),
            }
            for row in rows
        ]
    )


def _bundle_candidate(
    session: Session,
    *,
    attempt: EquityReferenceGenerationAttempt,
    manifest: list[dict[str, Any]],
    manifest_hash: str,
    created_at: datetime,
) -> CanonicalReleaseCandidate:
    """登记 derived 数据集、方法学和确定性内部运行，并构造真实来源血缘候选。"""
    dataset_id = uuid5(
        NAMESPACE_URL,
        "quant-v2:canonical-dataset:equity.workspace.reference-bundle:1",
    )
    session.execute(
        pg_insert(CanonicalDataset)
        .values(
            dataset_id=dataset_id,
            code=_BUNDLE_DATASET,
            schema_version=1,
            domain="equity",
            grain="reference dataset publication component",
            status="production",
            owner_service="service-data-sync",
            created_at=created_at,
        )
        .on_conflict_do_update(
            index_elements=("code", "schema_version"),
            set_={"status": "production"},
            where=CanonicalDataset.status.in_(("research", "candidate")),
        )
    )
    methodology_id = uuid5(
        NAMESPACE_URL,
        "quant-v2:methodology:equity.workspace.reference-bundle:1",
    )
    session.execute(
        pg_insert(MethodologyVersion)
        .values(
            methodology_version_id=methodology_id,
            code="equity.workspace.reference-bundle",
            version=1,
            semantic_family="derived-reference-publication-bundle",
            kind="derived",
            formula_hash=hashlib.sha256(_BUNDLE_MAPPING_VERSION.encode()).hexdigest(),
            effective_from=None,
            effective_to=None,
            status="validated",
            documentation_ref="internal://equity-reference-bundle-v1",
        )
        .on_conflict_do_nothing(index_elements=("code", "version"))
    )
    sync_run_id = uuid5(
        NAMESPACE_URL,
        f"quant-v2:sync-run:equity-reference-bundle:{attempt.attempt_id}:{manifest_hash}",
    )
    session.execute(
        pg_insert(SyncRun)
        .values(
            run_id=sync_run_id,
            capability=_BUNDLE_DATASET,
            mode="backfill",
            request_key=f"equity-reference-bundle:{attempt.attempt_id}:{manifest_hash}",
            target_date=attempt.snapshot_observed_on,
            status="succeeded",
            requested_at=created_at,
            started_at=created_at,
            finished_at=created_at,
            created_at=created_at,
        )
        .on_conflict_do_nothing(index_elements=("request_key",))
    )
    normalization_run_id = uuid5(
        NAMESPACE_URL,
        f"quant-v2:normalization-run:equity-reference-bundle:{manifest_hash}",
    )
    session.execute(
        pg_insert(NormalizationRun)
        .values(
            normalization_run_id=normalization_run_id,
            dataset_id=dataset_id,
            partition_key=_BUNDLE_PARTITION,
            run_id=sync_run_id,
            adapter_version="internal-reference-bundle-v1",
            schema_fingerprint=_BUNDLE_SCHEMA_FINGERPRINT,
            mapping_version=_BUNDLE_MAPPING_VERSION,
            input_set_hash=manifest_hash,
            status="passed",
            started_at=created_at,
            finished_at=created_at,
        )
        .on_conflict_do_nothing(
            index_elements=(
                "dataset_id",
                "partition_key",
                "input_set_hash",
                "mapping_version",
            )
        )
    )
    checkpoint_token = session.scalar(
        select(CanonicalCheckpoint.fencing_token).where(
            CanonicalCheckpoint.dataset_id == dataset_id,
            CanonicalCheckpoint.partition_key == _BUNDLE_PARTITION,
            CanonicalCheckpoint.checkpoint_kind == "published",
        )
    )
    records = tuple(
        CanonicalLineageRecord(
            record_key_hash=hashlib.sha256(
                (
                    f"{component['datasetCode']}|{component['partitionKey']}|{source_batch_id}"
                ).encode()
            ).hexdigest(),
            content_hash=_hash_json(component),
            source_batch_id=UUID(str(source_batch_id)),
            transform_hash=hashlib.sha256(_BUNDLE_MAPPING_VERSION.encode()).hexdigest(),
            role="input",
        )
        for component in manifest
        for source_batch_id in component["sourceBatchIds"]
    )
    return CanonicalReleaseCandidate(
        dataset_id=dataset_id,
        dataset_code=_BUNDLE_DATASET,
        partition_key=_BUNDLE_PARTITION,
        methodology_version_id=methodology_id,
        normalization_run_id=normalization_run_id,
        records=records,
        quality=CanonicalQualityDecision(
            status="passed",
            policy_code="equity-reference-bundle-completeness",
            policy_version=1,
            rules=(
                CanonicalQualityRule(
                    rule_code="all-reference-components-exactly-bound",
                    severity="blocking",
                    passed=True,
                ),
            ),
        ),
        fact_min=attempt.market_as_of,
        fact_max=attempt.snapshot_observed_on,
        checkpoint_kind="published",
        checkpoint_position={
            "manifestHash": manifest_hash,
            "snapshotObservedOn": attempt.snapshot_observed_on.isoformat(),
            "marketAsOf": attempt.market_as_of.isoformat(),
        },
        expected_fencing_token=0 if checkpoint_token is None else int(checkpoint_token),
        created_at=created_at,
        publication_effective_as_of=attempt.snapshot_observed_on,
    )


def _command_retryable(detail: dict[str, Any]) -> bool:
    """只有全部未成功 run 都明确标记可重试时才复制 command。"""
    errors = [
        child.get("error")
        for child in detail.get("childRuns", [])
        if child.get("status") in {"FAILED", "PARTIAL", "INTERRUPTED"}
    ]
    return bool(errors) and all(
        isinstance(error, dict) and error.get("retryable") is True for error in errors
    )


def _command_error(detail: dict[str, Any]) -> dict[str, Any]:
    """从公开 run 摘要构造稳定失败，不复制供应商正文。"""
    errors = [
        child.get("error")
        for child in detail.get("childRuns", [])
        if isinstance(child.get("error"), dict)
    ]
    return {
        "code": "equity-reference-step-failed",
        "retryable": _command_retryable(detail),
        "commandStatus": str(detail.get("status")),
        "runErrors": errors,
    }


def _hash_json(value: object) -> str:
    """对规范 JSON 计算稳定 SHA-256。"""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
