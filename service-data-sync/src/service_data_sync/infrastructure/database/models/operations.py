"""数据运维控制面的权威账本模型。

本模块把命令、运行、全局执行槽、健康评估、计划和不可变事件保存在 data-sync
PostgreSQL。Celery/Redis 只负责唤醒，任何 worker 都必须先取得 `ExecutionSlot` 的
单调 fencing token，才能发布、推进 checkpoint 或写入运行终态。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class DataOperationIdempotency(Base):
    """保存内部写操作稳定幂等键与请求摘要，阻止未知结果制造第二个业务动作。"""

    __tablename__ = "data_operation_idempotency"
    __table_args__ = (
        UniqueConstraint("operation", "idempotency_key", name="uq_data_operation_idempotency"),
        CheckConstraint("length(request_hash) = 64", name="ck_data_operation_idempotency_hash"),
        {"comment": "数据运维内部写操作幂等账本；键相同但请求不同必须拒绝。"},
    )

    idempotency_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="幂等记录 UUID。"
    )
    operation: Mapped[str] = mapped_column(
        String(80), nullable=False, comment="内部写操作稳定名称。"
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="调用方稳定复用的内部幂等键。"
    )
    request_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="规范化请求 SHA-256，用于识别同键不同请求。"
    )
    resource_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="受理后返回的权威资源类型。"
    )
    resource_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="受理后返回的权威资源 UUID。"
    )
    response_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="首次受理的安全响应，幂等重试必须原样重放。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="幂等意图首次持久化时间。"
    )


class DataOperationPreflight(Base):
    """保存有界时效的无副作用预检快照，提交时必须再次验证请求与冻结目标。"""

    __tablename__ = "data_operation_preflight"
    __table_args__ = (
        CheckConstraint("length(request_hash) = 64", name="ck_data_operation_preflight_hash"),
        {"comment": "同步预检冻结结果；不是锁、批准或执行成功证明。"},
    )

    preflight_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="预检 UUID。"
    )
    request_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="预检目标规范化摘要。"
    )
    targets_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, comment="按用户提交顺序冻结的同步目标。"
    )
    result_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, comment="逐目标预检结果，不含供应商正文。"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="预检可用于提交的最晚时刻。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="预检生成时间。"
    )


class DataOperationCommand(Base):
    """记录一个最多一百个有序 child run 的同步命令及其聚合终态。"""

    __tablename__ = "data_operation_command"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','CANCEL_REQUESTED','SUCCEEDED','PARTIAL',"
            "'FAILED','CANCELLED','REJECTED')",
            name="ck_data_operation_command_status",
        ),
        Index("ix_data_operation_command_status_requested", "status", "requested_at"),
        {"comment": "数据运维同步 parent command；child run 的顺序由 target_index 固定。"},
    )

    command_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="同步命令 UUID。"
    )
    submission_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
        index=True,
        comment="API submission；系统、遗留或恢复命令为空。",
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, comment="命令聚合状态。")
    actor_ref: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="不透明操作主体引用，系统操作使用 `system`。"
    )
    actor_role: Mapped[str] = mapped_column(String(24), nullable=False, comment="提交者授权角色。")
    reason: Mapped[str] = mapped_column(Text, nullable=False, comment="强制操作原因。")
    request_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="跨服务关联请求标识。"
    )
    retry_of_command_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_operation_command.command_id"),
        nullable=True,
        comment="重试时关联原 parent command。",
    )
    error_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="脱敏稳定错误摘要。"
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="命令受理时间。"
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="首个 child run 实际开始时间。"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="全部 child run 进入终态时间。"
    )


class DataOperationRun(Base):
    """记录一个命令内唯一数据集目标的冻结来源、执行状态、进度和 fencing token。"""

    __tablename__ = "data_operation_run"
    __table_args__ = (
        UniqueConstraint("command_id", "target_index", name="uq_data_operation_run_target_index"),
        UniqueConstraint("command_id", "dataset_code", name="uq_data_operation_run_dataset"),
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','CANCEL_REQUESTED','SUCCEEDED','PARTIAL',"
            "'FAILED','CANCELLED','INTERRUPTED','SKIPPED')",
            name="ck_data_operation_run_status",
        ),
        CheckConstraint(
            "recovery_attempts >= 0",
            name="ck_data_operation_run_recovery_attempts",
        ),
        Index("ix_data_operation_run_dispatch", "status", "requested_at"),
        {
            "comment": (
                "同步 child run；source_snapshot 受理后不可变，fencing token 防止陈旧 worker 写入。"
            )
        },
    )

    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="child run UUID。"
    )
    command_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_operation_command.command_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属 parent command。",
    )
    target_index: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="提交顺序，从零开始且永不重排。"
    )
    dataset_code: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="目标 canonical datasetCode。"
    )
    mode: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="FULL 等用户请求同步模式。"
    )
    target_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="日期范围或观察日等冻结同步目标。"
    )
    source_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, comment="Provider、真实上游、adapter 与方法学冻结快照。"
    )
    execution_intent_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        default=None,
        comment="仅 SYSTEM 兼容入口使用的私有执行意图；不得通过内部 HTTP 或公开投影暴露。",
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, comment="运行状态。")
    queue_position: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="受理快照中的队列位置；运行后可为空。"
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, comment="已开始执行的尝试次数。")
    recovery_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="仅租约过期回收时递增的恢复失败次数，正常公平批次不消耗该预算。",
    )
    completed_partitions: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="已完成分区数。"
    )
    total_partitions: Mapped[int] = mapped_column(Integer, nullable=False, comment="计划总分区数。")
    processed_records: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="已处理记录数。"
    )
    estimated_records: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="上游可可靠估计时的记录数。"
    )
    fencing_token: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="当前或最后持有的单调 slot token。"
    )
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="合作式取消请求标志。"
    )
    error_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="脱敏错误摘要。"
    )
    quality_gate_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="发布前质量门结论，与发布后健康评估分离。"
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="运行入队时间。"
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="获取全局 slot 后开始时间。"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="进入终态时间。"
    )


class DataOperationRunSourceBatch(Base):
    """追加记录控制面 run 实际消费并进入 canonical 事务的来源批次。

    `DataOperationRun.source_snapshot` 是提交前冻结的预期来源契约，本表则是执行后的真实来源
    事实。两者必须分开保存，避免把配置存在误报为已经消费的数据，也让公平批次、崩溃恢复和
    重试命令都能按精确 `SourceBatch` 对账。
    """

    __tablename__ = "data_operation_run_source_batch"
    __table_args__ = (
        Index(
            "ix_data_operation_run_source_batch_source",
            "source_batch_id",
            "run_id",
        ),
        {
            "comment": (
                "控制面 run 与实际 SourceBatch 的不可变多对多事实；仅由 fencing 完成事务追加。"
            )
        },
    )

    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_operation_run.run_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="实际消费来源批次的控制面 run。",
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="已进入本 run canonical 写事务的真实来源批次。",
    )
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="fencing 完成事务首次封印关系的时间。",
    )


class DataOperationPartition(Base):
    """保存 run 内可恢复分区及只暴露摘要的 checkpoint。"""

    __tablename__ = "data_operation_partition"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','SUCCEEDED','PARTIAL','FAILED','CANCELLED',"
            "'INTERRUPTED','SKIPPED')",
            name="ck_data_operation_partition_status",
        ),
        {"comment": "数据运维 run 内分区；checkpoint 只存摘要，不保存 Provider cursor。"},
    )

    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_operation_run.run_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属 child run。",
    )
    partition_key: Mapped[str] = mapped_column(
        String(240), primary_key=True, nullable=False, comment="稳定分区键。"
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, comment="分区状态。")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, comment="分区尝试次数。")
    checkpoint_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="定长 SHA-256 位置摘要。"
    )
    checkpoint_kind: Mapped[str | None] = mapped_column(
        String(40), nullable=True, comment="位置摘要解释类型。"
    )
    checkpoint_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最后安全 checkpoint 写入时间。"
    )
    error_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="脱敏分区错误摘要。"
    )


class DataOperationExecutionSlot(Base):
    """保存全平台唯一执行槽、租约、心跳和单调 fencing token。"""

    __tablename__ = "data_operation_execution_slot"
    __table_args__ = (
        CheckConstraint("slot_key = 'global'", name="ck_data_operation_slot_global"),
        CheckConstraint(
            "state IN ('IDLE','RUNNING','RECOVERING')", name="ck_data_operation_slot_state"
        ),
        CheckConstraint("fencing_token >= 0", name="ck_data_operation_slot_fencing"),
        {"comment": "全平台同步唯一槽；租约失效后 token 递增，旧 worker 不得再提交。"},
    )

    slot_key: Mapped[str] = mapped_column(
        String(16), primary_key=True, nullable=False, comment="固定全局槽键 `global`。"
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, comment="执行槽状态。")
    run_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, comment="当前持槽 child run。"
    )
    dataset_code: Mapped[str | None] = mapped_column(
        String(160), nullable=True, comment="当前持槽数据集。"
    )
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="worker 必须续租的截止时刻。"
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最近有效心跳。"
    )
    fencing_token: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="每次取得槽时递增的单调 token。"
    )


class DataOperationEvent(Base):
    """记录命令、运行、健康检查与计划的不可变安全运维事件。"""

    __tablename__ = "data_operation_event"
    __table_args__ = (
        Index(
            "ix_data_operation_event_resource_time", "resource_type", "resource_id", "occurred_at"
        ),
        Index("ix_data_operation_event_actor_time", "actor_ref", "occurred_at"),
        {"comment": "不可变数据运维事件；错误只允许稳定脱敏摘要。"},
    )

    event_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="事件 UUID。"
    )
    resource_type: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="COMMAND、RUN、HEALTH_CHECK 或 SCHEDULE。"
    )
    resource_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="关联资源 UUID。"
    )
    action: Mapped[str] = mapped_column(String(80), nullable=False, comment="审计动作代码。")
    result: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="动作结果，不等同资源终态。"
    )
    actor_ref: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="不透明主体或 system。"
    )
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="请求链路标识。")
    error_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="稳定脱敏错误摘要。"
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="事件发生时间。"
    )


class DataOperationHealthCheck(Base):
    """记录主动健康检查批次及其独立于同步命令的权威聚合状态。"""

    __tablename__ = "data_operation_health_check"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','SUCCEEDED','PARTIAL','FAILED','CANCELLED','REJECTED')",
            name="ck_data_operation_health_check_status",
        ),
        {"comment": "主动发布后健康检查批次；不回滚既有 publication。"},
    )

    health_check_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="健康检查 UUID。"
    )
    submission_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, comment="API submission；系统检查可为空。"
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, comment="批次聚合状态。")
    actor_ref: Mapped[str] = mapped_column(String(128), nullable=False, comment="提交主体。")
    actor_role: Mapped[str] = mapped_column(String(24), nullable=False, comment="提交主体角色。")
    reason: Mapped[str] = mapped_column(Text, nullable=False, comment="主动检查原因。")
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="请求链路标识。")
    error_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="批次错误。"
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="受理时间。"
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始时间。"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="终态时间。"
    )


class DataOperationHealthCheckTarget(Base):
    """保存健康检查原 target 的顺序、解析版本和逐目标结果。"""

    __tablename__ = "data_operation_health_check_target"
    __table_args__ = (
        UniqueConstraint(
            "health_check_id", "target_index", name="uq_data_operation_health_target_index"
        ),
        UniqueConstraint(
            "health_check_id", "dataset_code", name="uq_data_operation_health_target_dataset"
        ),
        {"comment": "主动健康检查有序 target；成功时关联不可变 evaluation。"},
    )

    health_check_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_operation_health_check.health_check_id"),
        primary_key=True,
        nullable=False,
        comment="所属健康检查。",
    )
    target_index: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="原提交顺序。"
    )
    dataset_code: Mapped[str] = mapped_column(String(160), nullable=False, comment="目标数据集。")
    requested_data_version: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, comment="请求版本。"
    )
    resolved_data_version: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, comment="受理绑定版本。"
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, comment="目标状态。")
    evaluation_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, comment="成功评估 UUID。"
    )
    error_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="失败脱敏摘要。"
    )


class DataOperationHealthEvaluation(Base):
    """保存按数据版本和策略版本固定的不可变发布后健康评估事实。"""

    __tablename__ = "data_operation_health_evaluation"
    __table_args__ = (
        Index("ix_data_operation_health_evaluation_dataset_time", "dataset_code", "evaluated_at"),
        {"comment": "不可变健康评估事实；开放问题状态不嵌入本表。"},
    )

    evaluation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="评估 UUID。"
    )
    health_check_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, comment="主动检查批次；自动评估为空。"
    )
    dataset_code: Mapped[str] = mapped_column(String(160), nullable=False, comment="被评估数据集。")
    data_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="冻结 dataVersion。"
    )
    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="冻结 release。"
    )
    policy_code: Mapped[str] = mapped_column(String(120), nullable=False, comment="健康策略代码。")
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False, comment="健康策略版本。")
    status: Mapped[str] = mapped_column(String(24), nullable=False, comment="健康聚合状态。")
    score: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="可计算时的零到一百分。"
    )
    results_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, comment="有界脱敏规则结果。"
    )
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="评估时间。"
    )


class DataOperationHealthIssue(Base):
    """维护当前开放问题投影，不修改对应不可变 HealthEvaluation。"""

    __tablename__ = "data_operation_health_issue"
    __table_args__ = (
        Index("ix_data_operation_health_issue_dataset_status", "dataset_code", "status"),
        {"comment": "当前开放健康问题投影；历史评估仍保留原始事实。"},
    )

    issue_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="问题 UUID。"
    )
    dataset_code: Mapped[str] = mapped_column(String(160), nullable=False, comment="问题数据集。")
    rule_code: Mapped[str] = mapped_column(String(120), nullable=False, comment="来源规则代码。")
    dimension: Mapped[str] = mapped_column(String(24), nullable=False, comment="健康维度。")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, comment="WARN 或 CRITICAL。")
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="OPEN 或 ACKNOWLEDGED。"
    )
    first_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="首次发现时间。"
    )
    last_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="最近发现时间。"
    )
    affected_count: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="可估计受影响记录数。"
    )
    evidence_summary: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="脱敏聚合证据。"
    )


class DataOperationSchedule(Base):
    """保存每个 datasetCode 最多一个的受限结构化自动计划及乐观锁版本。"""

    __tablename__ = "data_operation_schedule"
    __table_args__ = (
        UniqueConstraint("dataset_code", name="uq_data_operation_schedule_dataset"),
        CheckConstraint("version >= 1", name="ck_data_operation_schedule_version"),
        {"comment": "数据库持久化结构化自动计划；禁止任意 cron 和并行同步。"},
    )

    schedule_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="计划 UUID。"
    )
    dataset_code: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="v1 唯一计划数据集。"
    )
    mode: Mapped[str] = mapped_column(String(24), nullable=False, comment="自动计划同步模式。")
    selector_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="已审核业务目标选择器；计划触发时冻结到 command。"
    )
    target_policy_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="版本化目标解析策略。"
    )
    frequency_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="受限结构化频率。"
    )
    misfire_policy: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="SKIP 或 RUN_ONCE。"
    )
    coalesce: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="错过触发是否合并。")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="计划启停状态。")
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment="乐观锁版本。")
    revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="当前修订 UUID。"
    )
    recent_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最近成功投递时间。"
    )
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="下一次计划触发。"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="最近更新。"
    )
    updated_by_actor_ref: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="最近更新主体。"
    )


class DataOperationScheduleRevision(Base):
    """保存计划每次创建、编辑或启停后的不可变配置快照与审计摘要。"""

    __tablename__ = "data_operation_schedule_revision"
    __table_args__ = (
        UniqueConstraint(
            "schedule_id", "version", name="uq_data_operation_schedule_revision_version"
        ),
        CheckConstraint("version >= 1", name="ck_data_operation_schedule_revision_version"),
        CheckConstraint(
            "length(before_hash) = 64", name="ck_data_operation_schedule_revision_before"
        ),
        CheckConstraint(
            "length(after_hash) = 64", name="ck_data_operation_schedule_revision_after"
        ),
        {"comment": "自动计划不可变 revision；每次变更冻结前后摘要和操作者。"},
    )

    revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="计划 revision UUID。"
    )
    schedule_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_operation_schedule.schedule_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属当前计划。",
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="该 revision 对应乐观锁版本。"
    )
    change_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="UPSERT、SET_ENABLED 或历史 BASELINE。"
    )
    dataset_code: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="冻结的数据集键，更新不得改绑。"
    )
    mode: Mapped[str] = mapped_column(String(24), nullable=False, comment="冻结的同步模式。")
    selector_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="冻结的严格业务 selector。"
    )
    target_policy_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="冻结的版本化目标策略。"
    )
    frequency_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="冻结的结构化频率。"
    )
    misfire_policy: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="冻结的错过触发策略。"
    )
    coalesce: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="冻结的漏跑合并开关。")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="冻结的启用状态。")
    before_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="变更前安全摘要；创建使用空对象摘要。"
    )
    after_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="变更后安全摘要。")
    actor_ref: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="执行该变更的不透明主体。"
    )
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="关联请求标识。")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="revision 持久化时间。"
    )


class DataOperationScheduleFire(Base):
    """保存每次持久化计划 fire 的去重键、冻结目标、合并信息和关联 command。"""

    __tablename__ = "data_operation_schedule_fire"
    __table_args__ = (
        UniqueConstraint(
            "schedule_id",
            "scheduled_for",
            "schedule_version",
            name="uq_data_operation_schedule_fire_occurrence",
        ),
        UniqueConstraint("command_id", name="uq_data_operation_schedule_fire_command"),
        CheckConstraint("schedule_version >= 1", name="ck_data_operation_schedule_fire_version"),
        CheckConstraint("coalesced_count >= 0", name="ck_data_operation_schedule_fire_coalesced"),
        CheckConstraint(
            "outcome IN ('QUEUED','SKIPPED','REJECTED')",
            name="ck_data_operation_schedule_fire_outcome",
        ),
        {"comment": "确定性计划 fire；重启或双 scheduler 不得产生第二个 command。"},
    )

    fire_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="UUIDv5 推导的稳定 fire 键。",
    )
    schedule_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_operation_schedule.schedule_id", ondelete="RESTRICT"),
        nullable=False,
        comment="触发所属计划。",
    )
    revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_operation_schedule_revision.revision_id", ondelete="RESTRICT"),
        nullable=False,
        comment="触发时冻结的不可变 revision。",
    )
    schedule_version: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="触发时计划乐观锁版本。"
    )
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="频率计算出的权威计划时刻。"
    )
    selector_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="fire 冻结的严格 selector。"
    )
    target_policy_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="fire 冻结的目标解析策略。"
    )
    target_policy_version: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="fire 冻结的 targetPolicy 版本。"
    )
    target_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="已解析的 command target；跳过或拒绝时为空。"
    )
    resolved_observation_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="OBSERVATION_DATE 解析出的业务日期。"
    )
    command_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_operation_command.command_id", ondelete="RESTRICT"),
        nullable=True,
        comment="成功入队的唯一 parent command。",
    )
    outcome: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="QUEUED、SKIPPED 或 REJECTED。"
    )
    reason_code: Mapped[str | None] = mapped_column(
        String(80), nullable=True, comment="跳过或拒绝时的稳定原因码。"
    )
    coalesced_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="该 fire 合并的漏跑数量。"
    )
    request_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="与创建 command 共用的关联请求标识。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="fire 持久化时间。"
    )
