"""完成证券身份 Contract C1 并增加生命周期恢复检查点。

Revision ID: 202607280004
Revises: 202607280003
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

# Alembic 使用的版本标识。
revision = "202607280004"
down_revision = "202607280003"
branch_labels = None
depends_on = None

_SOURCE_EVIDENCE_COMMENT = "官方更正的来源证据引用；字段名为历史兼容名称。"
_LEGACY_APPROVAL_COMMENT = "官方更正必须携带的人工审批引用。"


def upgrade() -> None:
    """先验证双时间身份与事实外键，再删除旧绝对代码唯一约束。"""
    _create_lifecycle_checkpoint()
    _assert_identifier_contract()
    _assert_fact_identity_coverage()
    _set_correction_reference_comment(_SOURCE_EVIDENCE_COMMENT)
    op.drop_constraint(
        "equity_instrument_exchange_symbol_key",
        "equity_instrument",
        type_="unique",
    )


def downgrade() -> None:
    """仅在未产生代码复用和生命周期检查点时恢复旧唯一约束。"""
    bind = op.get_bind()
    checkpoint_exists = bind.execute(
        text("SELECT EXISTS (SELECT 1 FROM equity_lifecycle_checkpoint)")
    ).scalar_one()
    if checkpoint_exists:
        raise RuntimeError("cannot downgrade identity C1 after lifecycle checkpoint exists")
    duplicate_projection_exists = bind.execute(
        text(
            """
            SELECT EXISTS (
              SELECT 1
              FROM equity_instrument
              GROUP BY exchange, symbol
              HAVING COUNT(*) > 1
            )
            """
        )
    ).scalar_one()
    if duplicate_projection_exists:
        raise RuntimeError("cannot restore absolute equity code uniqueness after code reuse")
    op.create_unique_constraint(
        "equity_instrument_exchange_symbol_key",
        "equity_instrument",
        ["exchange", "symbol"],
    )
    op.drop_table("equity_lifecycle_checkpoint")
    _set_correction_reference_comment(_LEGACY_APPROVAL_COMMENT)


def _create_lifecycle_checkpoint() -> None:
    """创建每所最后成功 lifecycle publication 与可重放证据位置。"""
    op.execute(
        """
        CREATE TABLE equity_lifecycle_checkpoint (
          exchange VARCHAR(4) PRIMARY KEY
            CHECK (exchange IN ('SSE', 'SZSE', 'BSE')),
          target_date DATE NOT NULL,
          data_version UUID NOT NULL REFERENCES dataset_publication(data_version)
            ON DELETE RESTRICT,
          snapshot_id UUID NOT NULL REFERENCES equity_master_snapshot(snapshot_id)
            ON DELETE RESTRICT,
          source_batch_id UUID NOT NULL REFERENCES source_batch(source_batch_id)
            ON DELETE RESTRICT,
          raw_uri TEXT NOT NULL,
          normalized_uri TEXT NOT NULL,
          provider_id VARCHAR(100) NOT NULL,
          upstream_source VARCHAR(100) NOT NULL,
          adapter_version VARCHAR(64) NOT NULL,
          schema_fingerprint VARCHAR(64) NOT NULL
            CHECK (schema_fingerprint ~ '^[0-9a-f]{64}$'),
          observed_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE equity_lifecycle_checkpoint IS "
        "'显式上市生命周期最后成功发布与确定性恢复检查点。'"
    )
    comments = {
        "exchange": "检查点所属交易所。",
        "target_date": "本批生命周期证据对应的目标市场日。",
        "data_version": "成功发布后可见的目录数据版本。",
        "snapshot_id": "最后成功生命周期证据快照。",
        "source_batch_id": "最后成功发布所消费的来源观测批次。",
        "raw_uri": "供应商原始响应的不可变对象位置。",
        "normalized_uri": "provider-neutral 标准批次的不可变对象位置，供 replay 解码。",
        "provider_id": "产生原始证据的 adapter 身份。",
        "upstream_source": "adapter 记录的真实上游来源。",
        "adapter_version": "生成标准批次的固定 adapter 版本。",
        "schema_fingerprint": "最后成功批次的原始表头指纹。",
        "observed_at": "原始来源事实首次被观测的 UTC 时刻。",
        "updated_at": "检查点与 publication 同事务推进的 UTC 时刻。",
    }
    for column_name, comment in comments.items():
        escaped = comment.replace("'", "''")
        op.execute(f"COMMENT ON COLUMN equity_lifecycle_checkpoint.{column_name} IS '{escaped}'")


def _set_correction_reference_comment(comment: str) -> None:
    """同步历史兼容字段的数据库说明，避免将来源证据误写为流程审批。"""
    escaped = comment.replace("'", "''")
    op.execute(
        "COMMENT ON COLUMN equity_listing_status_version.correction_approval_reference "
        f"IS '{escaped}'"
    )


def _assert_identifier_contract() -> None:
    """验证开放标识唯一索引、双时间排斥约束及实际区间均无冲突。"""
    bind = op.get_bind()
    current_index_ready = bind.execute(
        text(
            """
            SELECT EXISTS (
              SELECT 1
              FROM pg_index i
              JOIN pg_class c ON c.oid = i.indexrelid
              WHERE c.relname = 'uq_equity_identifier_current_open'
                AND i.indisunique
                AND i.indisvalid
                AND i.indpred IS NOT NULL
            )
            """
        )
    ).scalar_one()
    if not current_index_ready:
        raise RuntimeError("identity C1 requires a valid current-open unique index")
    validated_exclusions = bind.execute(
        text(
            """
            SELECT COUNT(*)
            FROM pg_constraint
            WHERE conrelid = 'equity_identifier_version'::regclass
              AND contype = 'x'
              AND convalidated
            """
        )
    ).scalar_one()
    if int(validated_exclusions) < 2:
        raise RuntimeError("identity C1 requires validated effective/knowledge exclusions")
    overlaps = bind.execute(
        text(
            """
            SELECT EXISTS (
              SELECT 1
              FROM equity_identifier_version left_version
              JOIN equity_identifier_version right_version
                ON left_version.version_id < right_version.version_id
               AND left_version.effective_range && right_version.effective_range
               AND left_version.knowledge_range && right_version.knowledge_range
               AND (
                 left_version.security_id = right_version.security_id
                 OR (
                   left_version.exchange = right_version.exchange
                   AND left_version.symbol = right_version.symbol
                 )
               )
            )
            """
        )
    ).scalar_one()
    if overlaps:
        raise RuntimeError("identity C1 found overlapping effective/knowledge versions")


def _assert_fact_identity_coverage() -> None:
    """验证现有全部证券事实在自身适用日均能回到同一标识历史。"""
    uncovered = (
        op.get_bind()
        .execute(
            text(
                """
            WITH facts AS (
              SELECT security_id, trade_date AS fact_date FROM equity_daily_bar
              UNION ALL
              SELECT security_id, period_end FROM equity_weekly_bar
              UNION ALL
              SELECT security_id, period_end FROM equity_monthly_bar
              UNION ALL
              SELECT security_id, effective_date FROM equity_adjustment_factor
              UNION ALL
              SELECT security_id,
                     COALESCE(ex_date, record_date, announcement_date, report_period)
              FROM equity_corporate_action_version
              UNION ALL
              SELECT profile.security_id,
                     (batch.observed_at AT TIME ZONE 'Asia/Shanghai')::date
              FROM equity_profile_version profile
              JOIN source_batch batch ON batch.source_batch_id = profile.source_batch_id
              UNION ALL
              SELECT item.security_id, item.snapshot_date
              FROM sector_membership_item item
              UNION ALL
              SELECT report.security_id, revision.report_period
              FROM financial_report_revision revision
              JOIN financial_report report
                ON report.financial_report_id = revision.financial_report_id
              UNION ALL
              SELECT security_id, report_period
              FROM provider_financial_metric_revision
              UNION ALL
              SELECT security_id, observation_date
              FROM valuation_observation_revision
            )
            SELECT COUNT(*)
            FROM facts
            WHERE fact_date IS NULL
               OR NOT EXISTS (
                 SELECT 1
                 FROM equity_identifier_version identifier
                 WHERE identifier.security_id = facts.security_id
                   AND identifier.effective_range @> facts.fact_date
               )
            """
            )
        )
        .scalar_one()
    )
    if int(uncovered) > 0:
        raise RuntimeError("identity C1 found facts outside their canonical identifier range")
