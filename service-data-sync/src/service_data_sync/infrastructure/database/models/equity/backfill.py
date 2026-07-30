"""股票中心全量回填计划、冻结证据与可恢复状态模型。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class EquityReferenceGenerationAttempt(Base):
    """保存股票中心引用数据刷新、跨日滚转和 bundle 封印的权威状态。

    一次 attempt 只绑定一个上海自然日和一个最近完整交易日。跨越午夜或最终失败时保留
    原记录并创建下一 attempt；只有 `SEALED` 状态才允许被历史回填父计划引用。
    """

    __tablename__ = "equity_reference_generation_attempt"
    __table_args__ = (
        CheckConstraint(
            "status IN ('BUILDING','SEALED','ROLLED_FORWARD','FAILED')",
            name="ck_equity_reference_attempt_status",
        ),
        CheckConstraint(
            "attempt_no >= 1",
            name="ck_equity_reference_attempt_number",
        ),
        CheckConstraint(
            "market_as_of <= snapshot_observed_on",
            name="ck_equity_reference_attempt_dates",
        ),
        CheckConstraint(
            "(status = 'SEALED' AND bundle_publication_id IS NOT NULL "
            "AND bundle_data_version IS NOT NULL AND bundle_release_id IS NOT NULL "
            "AND manifest_json IS NOT NULL AND manifest_hash IS NOT NULL "
            "AND source_batch_ids_json IS NOT NULL AND source_batch_hash IS NOT NULL "
            "AND sealed_at IS NOT NULL) OR "
            "(status <> 'SEALED' AND bundle_publication_id IS NULL "
            "AND bundle_data_version IS NULL AND bundle_release_id IS NULL "
            "AND manifest_json IS NULL AND manifest_hash IS NULL "
            "AND source_batch_ids_json IS NULL AND source_batch_hash IS NULL "
            "AND sealed_at IS NULL)",
            name="ck_equity_reference_attempt_seal",
        ),
        CheckConstraint(
            "manifest_hash IS NULL OR length(manifest_hash) = 64",
            name="ck_equity_reference_attempt_manifest_hash",
        ),
        CheckConstraint(
            "source_batch_hash IS NULL OR length(source_batch_hash) = 64",
            name="ck_equity_reference_attempt_source_hash",
        ),
        UniqueConstraint(
            "campaign_key",
            "attempt_no",
            name="uq_equity_reference_attempt_number",
        ),
        Index(
            "uq_equity_reference_attempt_building",
            "campaign_key",
            unique=True,
            postgresql_where="status = 'BUILDING'",
        ),
        {"comment": "股票中心当前引用数据生成、跨日滚转与 canonical bundle 封印账本。"},
    )

    attempt_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="引用生成 attempt UUID。"
    )
    campaign_key: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="运维方稳定复用的引用生成批次键。"
    )
    attempt_no: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同一批次从一开始递增的真实尝试序号。"
    )
    snapshot_observed_on: Mapped[date] = mapped_column(
        Date, nullable=False, comment="目录、分类、公司当前态使用的上海自然日。"
    )
    market_as_of: Mapped[date] = mapped_column(
        Date, nullable=False, comment="交易状态使用的最近完整权威交易日。"
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="构建、封印、跨日滚转或最终失败状态。"
    )
    bundle_publication_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_publication.publication_id", ondelete="RESTRICT"),
        nullable=True,
        comment="封印后真实 `equity.workspace.reference-bundle` publication。",
    )
    bundle_data_version: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_publication.data_version", ondelete="RESTRICT"),
        nullable=True,
        comment="封印后 bundle 消费者稳定版本。",
    )
    bundle_release_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        nullable=True,
        comment="封印后 bundle 的不可变 canonical release。",
    )
    manifest_json: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB, nullable=True, comment="封印后精确组件 publication、版本和来源批次清单。"
    )
    manifest_hash: Mapped[str | None] = mapped_column(
        CHAR(64), nullable=True, comment="组件清单规范 SHA-256。"
    )
    source_batch_ids_json: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True, comment="bundle 全部真实 SourceBatch UUID 的排序去重清单。"
    )
    source_batch_hash: Mapped[str | None] = mapped_column(
        CHAR(64), nullable=True, comment="bundle 来源批次清单规范 SHA-256。"
    )
    last_error_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="失败或跨日滚转的稳定机器原因，不含供应商正文。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="attempt 创建时间。"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="attempt 最近状态更新时间。"
    )
    sealed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="bundle 与 attempt 原子封印时间。"
    )


class EquityReferenceGenerationStep(Base):
    """保存一个引用数据集命令的不可变目标与可恢复执行状态。"""

    __tablename__ = "equity_reference_generation_step"
    __table_args__ = (
        CheckConstraint(
            "ordinal >= 1 AND ordinal <= 7",
            name="ck_equity_reference_step_ordinal",
        ),
        CheckConstraint(
            "status IN ('HELD','SUBMITTED','RUNNING','SUCCEEDED','FAILED')",
            name="ck_equity_reference_step_status",
        ),
        CheckConstraint(
            "retry_count >= 0 AND retry_count <= 3",
            name="ck_equity_reference_step_retry_count",
        ),
        CheckConstraint(
            "(status = 'HELD' AND command_id IS NULL AND submitted_at IS NULL) OR "
            "(status <> 'HELD' AND command_id IS NOT NULL AND submitted_at IS NOT NULL)",
            name="ck_equity_reference_step_command",
        ),
        CheckConstraint(
            "(status IN ('SUCCEEDED','FAILED') AND finished_at IS NOT NULL) OR "
            "(status NOT IN ('SUCCEEDED','FAILED') AND finished_at IS NULL)",
            name="ck_equity_reference_step_finished",
        ),
        CheckConstraint(
            "(status = 'SUCCEEDED' AND output_publications_json IS NOT NULL "
            "AND source_batch_ids_json IS NOT NULL AND output_hash IS NOT NULL) OR "
            "(status <> 'SUCCEEDED' AND output_publications_json IS NULL "
            "AND source_batch_ids_json IS NULL AND output_hash IS NULL)",
            name="ck_equity_reference_step_output",
        ),
        CheckConstraint(
            "output_hash IS NULL OR length(output_hash) = 64",
            name="ck_equity_reference_step_output_hash",
        ),
        UniqueConstraint(
            "attempt_id",
            "step_key",
            name="uq_equity_reference_step_key",
        ),
        UniqueConstraint(
            "submission_id",
            name="uq_equity_reference_step_submission",
        ),
        Index(
            "ix_equity_reference_step_status",
            "attempt_id",
            "status",
            "ordinal",
        ),
        {"comment": "股票中心引用 bundle 七个真实控制面命令的可恢复状态。"},
    )

    attempt_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("equity_reference_generation_attempt.attempt_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属引用生成 attempt。",
    )
    ordinal: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, nullable=False, comment="依赖顺序固定的一到七。"
    )
    step_key: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="稳定步骤标识。"
    )
    dataset_code: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="真实控制面 datasetCode。"
    )
    target_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="冻结且可幂等重放的标准同步目标。"
    )
    submission_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="初次提交的稳定 submission UUID。"
    )
    command_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_operation_command.command_id", ondelete="RESTRICT"),
        nullable=True,
        comment="当前初次或重试控制面 command。",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="步骤持有、已提交、运行或终态。"
    )
    retry_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="自动创建重试 command 的次数。"
    )
    last_error_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="最近一次命令的稳定失败摘要。"
    )
    output_publications_json: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB, nullable=True, comment="成功后立刻冻结的精确组件 publication 清单。"
    )
    source_batch_ids_json: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True, comment="成功 command 实际使用的排序去重 SourceBatch UUID。"
    )
    output_hash: Mapped[str | None] = mapped_column(
        CHAR(64), nullable=True, comment="publication 与来源清单组合的规范 SHA-256。"
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="首次确认 command 的时间。"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="步骤成功或最终失败时间。"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="步骤最近持久化时间。"
    )


class EquityBackfillPlan(Base):
    """冻结一次股票中心全量回填的主数据版本、时间边界和完整 child 数量。

    本表由数据库触发器禁止更新与删除；执行状态单独写入 `EquityBackfillPlanState`，
    从而保证恢复、审计和最终验收始终解释同一份输入。
    """

    __tablename__ = "equity_backfill_plan"
    __table_args__ = (
        CheckConstraint("plan_version = 1", name="ck_equity_backfill_plan_version"),
        CheckConstraint("length(request_hash) = 64", name="ck_equity_backfill_plan_request_hash"),
        CheckConstraint("length(roster_hash) = 64", name="ck_equity_backfill_plan_roster_hash"),
        CheckConstraint(
            "length(reference_manifest_hash) = 64",
            name="ck_equity_backfill_plan_reference_manifest_hash",
        ),
        CheckConstraint(
            "length(source_evidence_hash) = 64",
            name="ck_equity_backfill_plan_source_evidence_hash",
        ),
        CheckConstraint("roster_count > 0", name="ck_equity_backfill_plan_roster_count"),
        CheckConstraint("child_count > 0", name="ck_equity_backfill_plan_child_count"),
        CheckConstraint(
            "market_as_of <= snapshot_observed_on",
            name="ck_equity_backfill_plan_date_boundaries",
        ),
        UniqueConstraint("campaign_key", name="uq_equity_backfill_plan_campaign"),
        {"comment": "股票中心全量回填的不可变父计划；数据库是唯一权威。"},
    )

    plan_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="父计划稳定 UUID。"
    )
    campaign_key: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="运维方复用的稳定回填批次键。"
    )
    plan_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="计划 schema 版本，当前固定为一。"
    )
    request_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="不含数据库现状的规范化创建输入摘要。"
    )
    aggregate_publication_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_publication.publication_id", ondelete="RESTRICT"),
        nullable=False,
        comment="创建时冻结的 `equity.master.cn-a` 聚合发布记录。",
    )
    aggregate_data_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_publication.data_version", ondelete="RESTRICT"),
        nullable=False,
        comment="冻结聚合发布的消费者 `dataVersion`。",
    )
    aggregate_components_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, comment="三所 child publication 的分区、版本与知识截止。"
    )
    lifecycle_publications_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        comment="三所 `equity.lifecycle.explicit` 当前发布的分区、publication 与版本。",
    )
    reference_bundle_publication_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_publication.publication_id", ondelete="RESTRICT"),
        nullable=False,
        comment="先于历史计划封印的完整引用 bundle publication。",
    )
    reference_bundle_data_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_publication.data_version", ondelete="RESTRICT"),
        nullable=False,
        comment="引用 bundle 的消费者稳定版本。",
    )
    reference_manifest_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        comment="主目录、生命周期、分类、成分和停牌状态的精确组件清单。",
    )
    reference_manifest_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="引用组件清单规范 SHA-256。"
    )
    snapshot_observed_on: Mapped[date] = mapped_column(
        Date, nullable=False, comment="当前目录、成员、公司与公告快照使用的上海自然日。"
    )
    market_as_of: Mapped[date] = mapped_column(
        Date, nullable=False, comment="行情、交易状态与发现页使用的最近完整交易日。"
    )
    known_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="身份与来源证据冻结的 UTC 知识时间。"
    )
    roster_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="全部确认身份版本规范化列表的 SHA-256。"
    )
    roster_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="冻结确认身份版本数量。"
    )
    source_evidence_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="全部数据集来源开始与版本证据的组合摘要。"
    )
    exclusions_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, comment="明确不生成 child 的能力与稳定原因；不得算作成功。"
    )
    child_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="原子创建时必须完整落库的 child spec 总数。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="计划冻结并完整持久化时间。"
    )


class EquityBackfillPlanState(Base):
    """保存父计划可变执行状态；不承载任何会改变回填含义的规格字段。"""

    __tablename__ = "equity_backfill_plan_state"
    __table_args__ = (
        CheckConstraint(
            "status IN ('BUILDING','HELD','RUNNING','SUCCEEDED','PARTIAL','FAILED','BLOCKED')",
            name="ck_equity_backfill_plan_state_status",
        ),
        CheckConstraint("revision >= 1", name="ck_equity_backfill_plan_state_revision"),
        {"comment": "股票中心回填父计划的可恢复状态与最终机器审计摘要。"},
    )

    plan_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("equity_backfill_plan.plan_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属不可变父计划。",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="父计划聚合状态。"
    )
    current_phase: Mapped[str | None] = mapped_column(
        String(48), nullable=True, comment="当前可提交或正在执行的阶段。"
    )
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="状态乐观并发版本。"
    )
    last_error_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="不含供应商原文的稳定失败摘要。"
    )
    audit_summary_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="终态覆盖、版本、来源、方法学与事实数审计摘要。"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="状态最近持久化时间。"
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="成功或失败终态时间。"
    )


class EquityBackfillPlanIdentity(Base):
    """冻结聚合 publication 切片内一条确认证券代码身份版本。"""

    __tablename__ = "equity_backfill_plan_identity"
    __table_args__ = (
        CheckConstraint(
            "exchange IN ('SSE','SZSE','BSE')",
            name="ck_equity_backfill_identity_exchange",
        ),
        CheckConstraint(
            "symbol ~ '^[0-9]{6}$'",
            name="ck_equity_backfill_identity_symbol",
        ),
        CheckConstraint(
            "effective_date_precision IN ('OFFICIAL_DATE','OBSERVATION_DATE')",
            name="ck_equity_backfill_identity_precision",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_equity_backfill_identity_effective_range",
        ),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from",
            name="ck_equity_backfill_identity_known_range",
        ),
        UniqueConstraint(
            "plan_id",
            "identifier_version_id",
            name="uq_equity_backfill_identity_version",
        ),
        Index(
            "ix_equity_backfill_identity_security",
            "plan_id",
            "security_id",
            "identifier_version_id",
        ),
        {"comment": "父计划创建时可见的完整 CONFIRMED A 股身份名单。"},
    )

    plan_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("equity_backfill_plan.plan_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属不可变父计划。",
    )
    ordinal: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="按交易所、代码、身份 UUID 的稳定序号。"
    )
    identifier_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="双时间身份版本永久 UUID。"
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="canonical 大表关联的永久证券键。"
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="跨服务可审计的证券永久 UUID。"
    )
    exchange: Mapped[str] = mapped_column(
        String(4), nullable=False, comment="冻结版本所属交易所。"
    )
    symbol: Mapped[str] = mapped_column(
        String(6), nullable=False, comment="冻结版本的六位代码。"
    )
    effective_from: Mapped[date] = mapped_column(
        Date, nullable=False, comment="代码版本业务有效半开区间起点。"
    )
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="代码版本业务有效半开区间终点。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="平台知识半开区间起点。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="平台知识半开区间终点。"
    )
    effective_date_precision: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="业务日期来自官方证据或观察日期。"
    )


class EquityBackfillPlanSource(Base):
    """冻结一个计划数据集的控制面来源快照与外部可审计版本证据。"""

    __tablename__ = "equity_backfill_plan_source"
    __table_args__ = (
        CheckConstraint(
            "length(source_snapshot_hash) = 64",
            name="ck_equity_backfill_source_snapshot_hash",
        ),
        CheckConstraint(
            "length(evidence_sha256) = 64",
            name="ck_equity_backfill_source_evidence_hash",
        ),
        CheckConstraint(
            "length(source_contract_hash) = 64",
            name="ck_equity_backfill_source_contract_hash",
        ),
        CheckConstraint(
            "length(expected_schema_fingerprint) = 64",
            name="ck_equity_backfill_source_schema_hash",
        ),
        CheckConstraint(
            "methodology_version >= 1",
            name="ck_equity_backfill_source_methodology_version",
        ),
        CheckConstraint(
            "source_kind IN ('EXTERNAL_PROVIDER','INTERNAL_EXECUTOR')",
            name="ck_equity_backfill_source_kind",
        ),
        CheckConstraint(
            "(source_kind = 'EXTERNAL_PROVIDER' AND internal_executor_code IS NULL) OR "
            "(source_kind = 'INTERNAL_EXECUTOR' AND internal_executor_code IS NOT NULL)",
            name="ck_equity_backfill_source_executor",
        ),
        CheckConstraint(
            "length(input_contract_hash) = 64",
            name="ck_equity_backfill_source_input_hash",
        ),
        {"comment": "每数据集来源边界、adapter/schema/mapping 与方法学冻结证据。"},
    )

    plan_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("equity_backfill_plan.plan_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属不可变父计划。",
    )
    dataset_code: Mapped[str] = mapped_column(
        String(160), primary_key=True, nullable=False, comment="控制面 canonical 数据集编码。"
    )
    source_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="外部 Provider 或平台内部 executor 来源类型。"
    )
    publication_dataset_code: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="最终 publication 审计使用的精确数据集编码。"
    )
    source_snapshot_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, comment="控制面将写入 child run 的来源绑定快照。"
    )
    source_snapshot_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="规范化来源绑定快照 SHA-256。"
    )
    earliest_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="来源可证明的最早合法业务日期；当前快照能力为空。"
    )
    earliest_date_method: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="来源边界探测或官方清单方法代码。"
    )
    evidence_ref: Mapped[str] = mapped_column(
        Text, nullable=False, comment="不含凭据的来源边界证据定位引用。"
    )
    evidence_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="来源边界证据正文摘要。"
    )
    source_contract_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="边界、版本、schema、映射与方法学字段的组合摘要。"
    )
    evidence_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="实际验证来源边界和版本的 UTC 时间。"
    )
    expected_provider_id: Mapped[str] = mapped_column(
        String(120), nullable=False, comment="最终来源批次必须匹配的 adapter/provider 身份。"
    )
    expected_capability: Mapped[str] = mapped_column(
        String(160),
        nullable=False,
        comment="最终来源批次或派生输入必须匹配的 provider-neutral 能力。",
    )
    expected_upstream_source: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="最终来源批次必须匹配的真实上游身份。"
    )
    expected_adapter_version: Mapped[str] = mapped_column(
        String(96), nullable=False, comment="来源证据冻结的真实 adapter 代码版本。"
    )
    expected_schema_fingerprint: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="来源证据冻结的响应 schema 指纹。"
    )
    supported_exchanges_json: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        comment="经同次真实能力探测证明可用的交易所清单；未列入者不得生成证券 child。",
    )
    internal_executor_code: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="内部派生能力的具体 executor 模块与限定名。"
    )
    input_contract_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, comment="内部派生输入 publication 类别或外部能力的空清单。"
    )
    input_contract_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="输入 publication 合同规范 SHA-256。"
    )
    methodology_code: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="数据口径的稳定方法学编码。"
    )
    methodology_version: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="数据口径的不可变方法学版本。"
    )
    mapping_version: Mapped[str] = mapped_column(
        String(96), nullable=False, comment="adapter 到 canonical 的字段映射版本。"
    )


class EquityBackfillPlanPage(Base):
    """记录最多一千 child 的确定性持久化页，支持创建崩溃后按摘要恢复。"""

    __tablename__ = "equity_backfill_plan_page"
    __table_args__ = (
        CheckConstraint("page_number >= 1", name="ck_equity_backfill_page_number"),
        CheckConstraint(
            "child_count >= 1 AND child_count <= 1000",
            name="ck_equity_backfill_page_child_count",
        ),
        CheckConstraint(
            "last_ordinal >= first_ordinal "
            "AND child_count = last_ordinal - first_ordinal + 1",
            name="ck_equity_backfill_page_ordinal_range",
        ),
        CheckConstraint(
            "payload_bytes > 0 AND payload_bytes <= 8388608",
            name="ck_equity_backfill_page_payload_bytes",
        ),
        CheckConstraint(
            "length(page_hash) = 64",
            name="ck_equity_backfill_page_hash",
        ),
        UniqueConstraint(
            "plan_id",
            "first_ordinal",
            name="uq_equity_backfill_page_first_ordinal",
        ),
        {"comment": "回填 child 规格的确定性分页摘要；seal 前不得提交执行。"},
    )

    plan_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("equity_backfill_plan.plan_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属不可变父计划。",
    )
    page_number: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="从一开始的连续页号。"
    )
    first_ordinal: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="本页第一个 child 稳定序号。"
    )
    last_ordinal: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="本页最后一个 child 稳定序号。"
    )
    child_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="本页 child 数量，硬上限一千。"
    )
    payload_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="规范 child 规格 JSON 字节数。"
    )
    page_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="规范 child 规格页 SHA-256。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="本页完整落库时间。"
    )


class EquityBackfillPlanSeal(Base):
    """在全部分页、来源、身份和 child 校验完成后一次性封印可提交计划。"""

    __tablename__ = "equity_backfill_plan_seal"
    __table_args__ = (
        CheckConstraint("page_count >= 1", name="ck_equity_backfill_seal_page_count"),
        CheckConstraint("child_count >= 1", name="ck_equity_backfill_seal_child_count"),
        CheckConstraint(
            "length(topology_hash) = 64",
            name="ck_equity_backfill_seal_topology_hash",
        ),
        CheckConstraint(
            "length(page_roster_hash) = 64",
            name="ck_equity_backfill_seal_page_roster_hash",
        ),
        {"comment": "存在且重算一致时父计划才允许从 BUILDING 进入 HELD。"},
    )

    plan_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("equity_backfill_plan.plan_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="已完整封印的父计划。",
    )
    page_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="连续分页总数。"
    )
    child_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="与父计划和全部分页一致的 child 总数。"
    )
    topology_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="全部 child 与 exclusion 顺序规范摘要。"
    )
    page_roster_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="按页号排列的 pageHash 清单摘要。"
    )
    sealed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="计划通过最终完整性校验的时间。"
    )


class EquityBackfillChildSpec(Base):
    """冻结一个可独立幂等提交的 command 规格、依赖与稳定身份。"""

    __tablename__ = "equity_backfill_child_spec"
    __table_args__ = (
        CheckConstraint(
            "phase IN ('RAW_SECURITY','CORPORATE_ACTION','DERIVED_SECURITY','GLOBAL_EVENT',"
            "'DISCOVERY_BUILD')",
            name="ck_equity_backfill_child_phase",
        ),
        CheckConstraint(
            "requirement IN ('BASE_REQUIRED','OPTIONAL','FINAL_REQUIRED')",
            name="ck_equity_backfill_child_requirement",
        ),
        CheckConstraint(
            "length(child_key) = 64",
            name="ck_equity_backfill_child_key",
        ),
        CheckConstraint(
            "window_to IS NULL OR (window_from IS NOT NULL AND window_to >= window_from)",
            name="ck_equity_backfill_child_window",
        ),
        CheckConstraint(
            "target_count > 0 AND target_count <= 100",
            name="ck_equity_backfill_child_target_count",
        ),
        UniqueConstraint("plan_id", "ordinal", name="uq_equity_backfill_child_ordinal"),
        UniqueConstraint("plan_id", "child_key", name="uq_equity_backfill_child_key"),
        UniqueConstraint("submission_id", name="uq_equity_backfill_child_submission"),
        ForeignKeyConstraint(
            ["plan_id", "identity_ordinal"],
            [
                "equity_backfill_plan_identity.plan_id",
                "equity_backfill_plan_identity.ordinal",
            ],
            name="fk_equity_backfill_child_identity",
            ondelete="RESTRICT",
        ),
        Index("ix_equity_backfill_child_phase", "plan_id", "phase", "ordinal"),
        {"comment": "全部先以 HELD 状态原子创建、随后按阶段提交的不可变 command 规格。"},
    )

    child_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="稳定 child UUID。"
    )
    plan_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("equity_backfill_plan.plan_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属不可变父计划。",
    )
    ordinal: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="跨阶段稳定提交顺序。"
    )
    phase: Mapped[str] = mapped_column(
        String(48), nullable=False, comment="显式依赖阶段。"
    )
    requirement: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        comment="基础硬门、可选覆盖或最终 publication 硬门；决定阶段推进语义。",
    )
    child_key: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="计划、阶段、身份、窗口与来源共同形成的 SHA-256。"
    )
    identity_ordinal: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="逐证券 child 对应的冻结身份序号；全局 child 为空。"
    )
    window_from: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="日期窗包含端起点；无日期能力为空。"
    )
    window_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="日期窗包含端终点；无日期能力为空。"
    )
    targets_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, comment="数据集编码互异的规范化控制面目标。"
    )
    intents_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, comment="逐目标严格 `EQUITY_BACKFILL` 私有执行意图。"
    )
    dependency_keys_json: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="必须成功的直接 child key；阶段依赖另由父状态机校验。"
    )
    completion_dependency_keys_json: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        comment="只要求到达终态的直接 child key；失败会形成 partial 而非阻断。",
    )
    source_hashes_json: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, comment="逐数据集冻结 sourceSnapshot 摘要。"
    )
    submission_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        comment="网络重试和崩溃恢复共用的稳定 submission UUID。",
    )
    request_prefix: Mapped[str] = mapped_column(
        String(96), nullable=False, comment="构造控制面幂等键和请求标识的稳定前缀。"
    )
    target_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="本 command 内唯一数据集目标数量。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="child spec 随父计划原子冻结时间。"
    )


class EquityBackfillChildState(Base):
    """保存 child command 绑定、恢复、失败与审计状态。"""

    __tablename__ = "equity_backfill_child_state"
    __table_args__ = (
        CheckConstraint(
            "status IN ('HELD','SUBMITTING','SUBMITTED','RUNNING','SUCCEEDED','PARTIAL',"
            "'FAILED','CANCELLED','BLOCKED')",
            name="ck_equity_backfill_child_state_status",
        ),
        CheckConstraint(
            "(status IN ('HELD','SUBMITTING') AND command_id IS NULL) OR "
            "(status = 'BLOCKED') OR "
            "(status NOT IN ('HELD','SUBMITTING','BLOCKED') AND command_id IS NOT NULL)",
            name="ck_equity_backfill_child_state_command",
        ),
        CheckConstraint(
            "(command_id IS NULL AND submitted_at IS NULL) OR "
            "(command_id IS NOT NULL AND submitted_at IS NOT NULL)",
            name="ck_equity_backfill_child_state_submitted",
        ),
        CheckConstraint(
            "(status IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED','BLOCKED') "
            "AND finished_at IS NOT NULL) "
            "OR (status NOT IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED','BLOCKED') "
            "AND finished_at IS NULL)",
            name="ck_equity_backfill_child_state_finished",
        ),
        CheckConstraint("resume_count >= 0", name="ck_equity_backfill_child_resume_count"),
        Index("ix_equity_backfill_child_state_status", "status", "updated_at"),
        {"comment": "可恢复 child 状态；规格、来源、窗口和身份均不在此表修改。"},
    )

    child_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("equity_backfill_child_spec.child_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属不可变 child spec。",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="child 聚合控制面状态。"
    )
    command_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_operation_command.command_id", ondelete="RESTRICT"),
        unique=True,
        nullable=True,
        comment="控制面 parent command；崩溃恢复可按 submission UUID 补绑。",
    )
    resume_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="父编排器重新检查本 child 的次数。"
    )
    last_error_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="最近稳定失败摘要。"
    )
    audit_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="运行、publication、来源和事实数的机器审计结果。"
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="首次确认 control-plane command 的时间。"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="child 进入终态的时间。"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="状态最近持久化时间。"
    )


class EquityBackfillPartitionCheckpoint(Base):
    """保存长历史 child 已成功分区的精确 publication、来源批次与恢复水位。

    仅成功且完成质量门的分区可写入本表；同一 child target 的分区键唯一且数据库禁止更新删除。
    worker 崩溃后只跳过这些已封印分区，最终 child result 必须重算完整分区 roster 与摘要。
    """

    __tablename__ = "equity_backfill_partition_checkpoint"
    __table_args__ = (
        CheckConstraint(
            "target_index >= 0 AND target_index < 100",
            name="ck_equity_backfill_partition_target",
        ),
        CheckConstraint(
            "window_to >= window_from",
            name="ck_equity_backfill_partition_window",
        ),
        CheckConstraint(
            "checkpoint_kind IN "
            "('DATA_VERSION','BAR_COVERAGE_VERSION','EVENT_COVERAGE_VERSION')",
            name="ck_equity_backfill_partition_kind",
        ),
        CheckConstraint(
            "(checkpoint_kind = 'DATA_VERSION' AND coverage_version IS NULL) OR "
            "(checkpoint_kind = 'BAR_COVERAGE_VERSION' AND coverage_version IS NOT NULL)",
            name="ck_equity_backfill_partition_coverage",
        ),
        CheckConstraint(
            "publication_kind IN ('DATA','ZERO_RECORD_COVERAGE')",
            name="ck_equity_backfill_partition_publication_kind",
        ),
        CheckConstraint(
            "(publication_kind = 'DATA' AND record_count >= 0) OR "
            "(publication_kind = 'ZERO_RECORD_COVERAGE' AND record_count = 0)",
            name="ck_equity_backfill_partition_record_count",
        ),
        CheckConstraint(
            "length(source_batch_hash) = 64",
            name="ck_equity_backfill_partition_source_hash",
        ),
        CheckConstraint(
            "length(output_hash) = 64",
            name="ck_equity_backfill_partition_output_hash",
        ),
        UniqueConstraint(
            "child_id",
            "target_index",
            "partition_key",
            name="uq_equity_backfill_partition_checkpoint",
        ),
        Index(
            "ix_equity_backfill_partition_roster",
            "child_id",
            "target_index",
            "window_from",
            "window_to",
        ),
        {"comment": "长历史 child 的不可变成功分区、精确输出与崩溃恢复水位。"},
    )

    checkpoint_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="分区成功事实稳定 UUID。"
    )
    child_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("equity_backfill_child_spec.child_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属不可变 child spec。",
    )
    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_operation_run.run_id", ondelete="RESTRICT"),
        nullable=False,
        comment="首次成功发布本分区的控制面 run。",
    )
    command_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_operation_command.command_id", ondelete="RESTRICT"),
        nullable=False,
        comment="首次成功发布本分区的 child command。",
    )
    target_index: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="与 child targets 顺序一致的 run 索引。"
    )
    dataset_code: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="本分区实际发布的控制面数据集编码。"
    )
    partition_key: Mapped[str] = mapped_column(
        String(256), nullable=False, comment="由数据集、身份和包含端窗口形成的稳定键。"
    )
    window_from: Mapped[date] = mapped_column(
        Date, nullable=False, comment="已成功执行分区的包含端起点。"
    )
    window_to: Mapped[date] = mapped_column(
        Date, nullable=False, comment="已成功执行分区的包含端终点。"
    )
    checkpoint_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="精确 dataVersion 或行情 coverage 版本类型。"
    )
    publication_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_publication.publication_id", ondelete="RESTRICT"),
        nullable=False,
        comment="本分区提交后可验证的 canonical publication。",
    )
    data_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_publication.data_version", ondelete="RESTRICT"),
        nullable=False,
        comment="publication 的消费者稳定版本。",
    )
    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        nullable=False,
        comment="publication 绑定的不可变 canonical release。",
    )
    coverage_version: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
        comment="行情单版本或事件 coverage roster 的稳定聚合 UUID；普通数据集为空。",
    )
    coverage_versions_json: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        comment="分区绑定的全部不可变 coverage UUID；普通 dataVersion checkpoint 使用空清单。",
    )
    publication_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="真实数据或经质量门确认的零记录覆盖。"
    )
    record_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="该分区来源响应的标准事实数量。"
    )
    source_batch_ids_json: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="支撑本分区输出的全部精确真实 SourceBatch UUID。"
    )
    source_batch_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="排序 SourceBatch UUID 清单的规范 SHA-256。"
    )
    output_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="窗口、publication、release、coverage 与来源的组合摘要。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="分区成功事实首次封印时间。"
    )


class EquityBackfillChildResult(Base):
    """按 target 追加保存 child run 的精确输入、输出 publication 与血缘审计。

    `providerless` executor 必须从依赖 child 的本表结果解析精确 `dataVersion`，
    不得在 dispatch 时读取后来推进的 current publication。
    """

    __tablename__ = "equity_backfill_child_result"
    __table_args__ = (
        CheckConstraint(
            "terminal_status IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED')",
            name="ck_equity_backfill_child_result_status",
        ),
        CheckConstraint(
            "target_index >= 0 AND target_index < 100",
            name="ck_equity_backfill_child_result_target",
        ),
        CheckConstraint(
            "length(input_manifest_hash) = 64",
            name="ck_equity_backfill_child_result_input_hash",
        ),
        CheckConstraint(
            "length(output_manifest_hash) = 64",
            name="ck_equity_backfill_child_result_output_hash",
        ),
        CheckConstraint(
            "length(audit_hash) = 64",
            name="ck_equity_backfill_child_result_audit_hash",
        ),
        UniqueConstraint(
            "child_id",
            "command_id",
            "target_index",
            name="uq_equity_backfill_child_result_attempt_target",
        ),
        UniqueConstraint("run_id", name="uq_equity_backfill_child_result_run"),
        Index("ix_equity_backfill_child_result_command", "command_id"),
        {"comment": "每个 child target run 终态一次性写入的精确输入、输出与审计事实。"},
    )

    result_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="终态结果 UUID。"
    )
    child_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("equity_backfill_child_spec.child_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属不可变 child。",
    )
    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_operation_run.run_id", ondelete="RESTRICT"),
        nullable=False,
        comment="产生该结果的精确控制面 run。",
    )
    command_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_operation_command.command_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属 child 的控制面 command。",
    )
    target_index: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="与不可变 child targets 顺序一致的索引。"
    )
    terminal_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="与 child state 对齐的终态。"
    )
    input_manifest_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        comment="由依赖 child result 解析出的精确 publication/dataVersion 输入清单。",
    )
    input_manifest_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="输入清单规范 SHA-256。"
    )
    output_manifest_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        comment="逐目标输出 publication、dataVersion、coverage、来源批次与事实数。",
    )
    output_manifest_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="输出清单规范 SHA-256。"
    )
    audit_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        comment="executor/mapping/methodology、边界与质量门逐项核验结论。",
    )
    audit_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="机器审计正文规范 SHA-256。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="与终态 canonical 事务一致的写入时间。"
    )
