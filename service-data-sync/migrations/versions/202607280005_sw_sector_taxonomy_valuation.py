"""创建申万三级 `taxonomy`、估值、发布与恢复表。

该独立数据域保存方法学版本、行业层级与估值的双时间修订、面向消费者的不可变发布、
闭包关系、质量证据及可重放 `checkpoint`；不改变既有板块消费者。每个发布以
`data_version` 关联通用发布指针，使读取方使用稳定完整快照而非半成品同步结果。

Revision ID: 202607280005
Revises: 202607280004
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "202607280005"
down_revision: str | None = "202607280004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """以 `expand` 方式创建申万专属表与索引，不改变既有板块消费者或其历史数据。"""
    op.create_table(
        "sw_sector_methodology",
        sa.Column(
            "methodology_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="方法学永久 UUID。",
        ),
        sa.Column(
            "code", sa.String(length=80), nullable=False, comment="不含 URL 的稳定方法学代码。"
        ),
        sa.Column(
            "version",
            sa.SmallInteger(),
            nullable=False,
            comment="同一方法学代码内递增的不可变版本。",
        ),
        sa.Column("status", sa.String(length=24), nullable=False, comment="来源报告或已退役状态。"),
        sa.Column(
            "upstream_source",
            sa.String(length=120),
            nullable=False,
            comment="展示 taxonomy 与估值的上游来源身份。",
        ),
        sa.Column(
            "semantic_spec_sha256",
            sa.CHAR(length=64),
            nullable=False,
            comment="层级、单位、最终态和字段语义说明摘要。",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="方法学版本首次登记时间。",
        ),
        sa.CheckConstraint("version > 0", name="ck_sw_sector_methodology_version"),
        sa.CheckConstraint(
            "status IN ('source_reported', 'retired')",
            name="ck_sw_sector_methodology_status",
        ),
        sa.CheckConstraint(
            "semantic_spec_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_sw_sector_methodology_semantic_sha256",
        ),
        sa.PrimaryKeyConstraint("methodology_id"),
        sa.UniqueConstraint("code", "version", name="uq_sw_sector_methodology_code_version"),
        comment="申万行业 taxonomy 与估值上游展示方法学的不可变版本身份。",
    )
    op.create_table(
        "sw_sector_node_revision",
        sa.Column(
            "node_revision_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="节点修订永久 UUID。",
        ),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="由申万代码确定的跨快照稳定节点 UUID。",
        ),
        sa.Column(
            "snapshot_date",
            sa.Date(),
            nullable=False,
            comment="该完整 taxonomy 快照的上海日历观测日期。",
        ),
        sa.Column(
            "sector_code",
            sa.String(length=16),
            nullable=False,
            comment="带 `.SI` 后缀的申万稳定行业代码。",
        ),
        sa.Column(
            "name",
            sa.String(length=200),
            nullable=False,
            comment="该修订中的申万行业显示名称。",
        ),
        sa.Column("level", sa.SmallInteger(), nullable=False, comment="申万一级、二级或三级层级。"),
        sa.Column(
            "parent_code",
            sa.String(length=16),
            nullable=True,
            comment="二级或三级节点的直接父级申万代码。",
        ),
        sa.Column(
            "component_count",
            sa.Integer(),
            nullable=False,
            comment="上游页面在该观测日展示的成分数量。",
        ),
        sa.Column(
            "methodology_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="支撑本节点语义的不可变方法学版本。",
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            comment="同一观测日与代码内递增的知识修订号。",
        ),
        sa.Column(
            "known_from",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="平台首次可使用本修订的 UTC 时间。",
        ),
        sa.Column(
            "known_to",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="本知识修订被替代的半开区间结束时间。",
        ),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="实际取得上游完整快照的 UTC 时间。",
        ),
        sa.Column(
            "source_batch_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="支撑本节点修订的 raw evidence 来源批次。",
        ),
        sa.Column(
            "content_sha256",
            sa.CHAR(length=64),
            nullable=False,
            comment="节点名称、层级、父级和成分数的稳定摘要。",
        ),
        sa.Column(
            "quality_status",
            sa.String(length=16),
            nullable=False,
            comment="通过、警告或隔离的质量处置。",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="本 canonical 修订写入时间。",
        ),
        sa.CheckConstraint("level BETWEEN 1 AND 3", name="ck_sw_sector_node_level"),
        sa.CheckConstraint("component_count >= 0", name="ck_sw_sector_node_component_count"),
        sa.CheckConstraint("revision > 0", name="ck_sw_sector_node_revision_number"),
        sa.CheckConstraint(
            "(level = 1 AND parent_code IS NULL) OR (level > 1 AND parent_code IS NOT NULL)",
            name="ck_sw_sector_node_parent_level",
        ),
        sa.CheckConstraint(
            "known_to IS NULL OR known_to > known_from",
            name="ck_sw_sector_node_known_range",
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_sw_sector_node_content_sha256",
        ),
        sa.CheckConstraint(
            "quality_status IN ('passed', 'warned', 'quarantined')",
            name="ck_sw_sector_node_quality_status",
        ),
        sa.ForeignKeyConstraint(
            ["methodology_id"], ["sw_sector_methodology.methodology_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_batch_id"], ["source_batch.source_batch_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("node_revision_id"),
        sa.UniqueConstraint(
            "snapshot_date",
            "sector_code",
            "revision",
            name="uq_sw_sector_node_snapshot_code_revision",
        ),
        comment="申万三级 taxonomy 节点的按观测日双时间知识修订。",
    )
    op.create_index(
        "uq_sw_sector_node_current",
        "sw_sector_node_revision",
        ["snapshot_date", "sector_code"],
        unique=True,
        postgresql_where=sa.text("known_to IS NULL"),
    )
    op.create_index(
        "ix_sw_sector_node_hierarchy",
        "sw_sector_node_revision",
        ["snapshot_date", "level", "parent_code", "sector_code"],
    )
    op.create_table(
        "sw_sector_valuation_revision",
        sa.Column(
            "valuation_revision_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="估值修订永久 UUID。",
        ),
        sa.Column(
            "snapshot_date",
            sa.Date(),
            nullable=False,
            comment="估值所属的上海日历观测日期。",
        ),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="由申万代码确定的稳定节点 UUID。",
        ),
        sa.Column(
            "sector_code",
            sa.String(length=16),
            nullable=False,
            comment="估值所属申万行业代码。",
        ),
        sa.Column(
            "methodology_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="本估值观察的来源展示方法学版本。",
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            comment="同日同行业同方法学内递增的修订号。",
        ),
        sa.Column(
            "static_pe",
            sa.Numeric(precision=38, scale=12),
            nullable=True,
            comment="来源展示的静态市盈率；缺失保持空值。",
        ),
        sa.Column(
            "ttm_pe",
            sa.Numeric(precision=38, scale=12),
            nullable=True,
            comment="来源展示的 TTM 滚动市盈率。",
        ),
        sa.Column(
            "pb",
            sa.Numeric(precision=38, scale=12),
            nullable=True,
            comment="来源展示的市净率。",
        ),
        sa.Column(
            "dividend_yield_ratio",
            sa.Numeric(precision=38, scale=12),
            nullable=True,
            comment="来源百分数除以一百后的股息率比例。",
        ),
        sa.Column(
            "finality",
            sa.String(length=32),
            nullable=False,
            comment="固定为供应商观察，不宣称官方最终值。",
        ),
        sa.Column(
            "known_from",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="平台首次可使用本修订的 UTC 时间。",
        ),
        sa.Column(
            "known_to",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="本知识修订被替代的半开区间结束时间。",
        ),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="实际取得来源响应的 UTC 时间。",
        ),
        sa.Column(
            "source_batch_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="支撑本估值修订的 raw evidence 来源批次。",
        ),
        sa.Column(
            "content_sha256",
            sa.CHAR(length=64),
            nullable=False,
            comment="估值字段与单位语义的稳定摘要。",
        ),
        sa.Column(
            "quality_status",
            sa.String(length=16),
            nullable=False,
            comment="通过、警告或隔离的质量处置。",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="本 canonical 修订写入时间。",
        ),
        sa.CheckConstraint("revision > 0", name="ck_sw_sector_valuation_revision_number"),
        sa.CheckConstraint(
            "known_to IS NULL OR known_to > known_from",
            name="ck_sw_sector_valuation_known_range",
        ),
        sa.CheckConstraint(
            "finality = 'PROVIDER_OBSERVATION'",
            name="ck_sw_sector_valuation_finality",
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_sw_sector_valuation_content_sha256",
        ),
        sa.CheckConstraint(
            "quality_status IN ('passed', 'warned', 'quarantined')",
            name="ck_sw_sector_valuation_quality_status",
        ),
        sa.ForeignKeyConstraint(
            ["methodology_id"], ["sw_sector_methodology.methodology_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_batch_id"], ["source_batch.source_batch_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("valuation_revision_id"),
        sa.UniqueConstraint(
            "snapshot_date",
            "sector_code",
            "methodology_id",
            "revision",
            name="uq_sw_sector_valuation_revision",
        ),
        comment="申万行业估值供应商日期观察的双时间知识修订。",
    )
    op.create_index(
        "uq_sw_sector_valuation_current",
        "sw_sector_valuation_revision",
        ["snapshot_date", "sector_code", "methodology_id"],
        unique=True,
        postgresql_where=sa.text("known_to IS NULL"),
    )
    op.create_index(
        "ix_sw_sector_valuation_date_code",
        "sw_sector_valuation_revision",
        ["snapshot_date", "sector_code"],
    )
    op.create_table(
        "sw_sector_publication",
        sa.Column(
            "data_version",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="同时关联通用发布指针的消费者不可变版本。",
        ),
        sa.Column(
            "capability",
            sa.String(length=64),
            nullable=False,
            comment="申万 taxonomy 或行业估值能力。",
        ),
        sa.Column(
            "snapshot_date",
            sa.Date(),
            nullable=False,
            comment="本发布完整覆盖的上海日历观测日期。",
        ),
        sa.Column(
            "methodology_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="本发布冻结的来源展示方法学版本。",
        ),
        sa.Column(
            "row_count",
            sa.Integer(),
            nullable=False,
            comment="本发布包含的完整行业行数。",
        ),
        sa.Column(
            "content_sha256",
            sa.CHAR(length=64),
            nullable=False,
            comment="本发布规范化完整内容稳定摘要。",
        ),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="本版本开始对内部消费者可见的时间。",
        ),
        sa.CheckConstraint(
            "capability IN ('sector.sw.taxonomy', 'sector.sw.valuation')",
            name="ck_sw_sector_publication_capability",
        ),
        sa.CheckConstraint("row_count > 0", name="ck_sw_sector_publication_row_count"),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_sw_sector_publication_content_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["data_version"], ["dataset_publication.data_version"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["methodology_id"], ["sw_sector_methodology.methodology_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("data_version"),
        comment="申万 taxonomy 或估值消费者发布的不可变明细。",
    )
    op.create_table(
        "sw_sector_closure",
        sa.Column(
            "data_version",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="闭包所属 taxonomy 不可变发布版本。",
        ),
        sa.Column(
            "ancestor_code",
            sa.String(length=16),
            nullable=False,
            comment="祖先申万行业代码。",
        ),
        sa.Column(
            "descendant_code",
            sa.String(length=16),
            nullable=False,
            comment="后代申万行业代码。",
        ),
        sa.Column(
            "depth",
            sa.SmallInteger(),
            nullable=False,
            comment="零表示自反边，一或二表示父级距离。",
        ),
        sa.CheckConstraint("depth BETWEEN 0 AND 2", name="ck_sw_sector_closure_depth"),
        sa.CheckConstraint(
            "(depth = 0 AND ancestor_code = descendant_code) OR depth > 0",
            name="ck_sw_sector_closure_self_edge",
        ),
        sa.ForeignKeyConstraint(
            ["data_version"], ["sw_sector_publication.data_version"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("data_version", "ancestor_code", "descendant_code"),
        comment="申万 taxonomy 不可变发布中的自反及父级闭包边。",
    )
    op.create_index(
        "ix_sw_sector_closure_descendant",
        "sw_sector_closure",
        ["data_version", "descendant_code", "depth"],
    )
    op.create_table(
        "sw_sector_sync_checkpoint",
        sa.Column(
            "capability",
            sa.String(length=64),
            nullable=False,
            comment="完整申万快照中立抓取能力。",
        ),
        sa.Column(
            "partition_key",
            sa.String(length=64),
            nullable=False,
            comment="固定 scheme 与观测日组成的恢复分区。",
        ),
        sa.Column(
            "snapshot_date",
            sa.Date(),
            nullable=False,
            comment="checkpoint 对应的上海日历观测日期。",
        ),
        sa.Column(
            "summary_sha256",
            sa.CHAR(length=64),
            nullable=False,
            comment="可重放中立载荷的 SHA-256 摘要。",
        ),
        sa.Column(
            "raw_sha256",
            sa.CHAR(length=64),
            nullable=False,
            comment="供应商原始响应的 SHA-256 摘要。",
        ),
        sa.Column(
            "raw_uri",
            sa.String(length=1024),
            nullable=False,
            comment="服务私有桶中的供应商原始响应 URI。",
        ),
        sa.Column(
            "normalized_uri",
            sa.String(length=1024),
            nullable=False,
            comment="服务私有桶中的中立可重放载荷 URI。",
        ),
        sa.Column(
            "provider_id",
            sa.String(length=120),
            nullable=False,
            comment="产生该快照的 adapter provider 身份。",
        ),
        sa.Column(
            "upstream_source",
            sa.String(length=120),
            nullable=False,
            comment="快照实际展示来源身份。",
        ),
        sa.Column(
            "adapter_version",
            sa.String(length=120),
            nullable=False,
            comment="生成中立载荷的 adapter 版本。",
        ),
        sa.Column(
            "schema_fingerprint",
            sa.CHAR(length=64),
            nullable=False,
            comment="该来源列集合的冻结 fingerprint。",
        ),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="实际取得来源响应的 UTC 时间。",
        ),
        sa.Column(
            "last_data_version",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="最近成功 taxonomy publication 的不可变版本。",
        ),
        sa.Column(
            "last_success_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="两项发布均成功完成的时间。",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="checkpoint 最近推进时间。",
        ),
        sa.CheckConstraint(
            "summary_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_sw_sector_checkpoint_summary_sha256",
        ),
        sa.CheckConstraint(
            "raw_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_sw_sector_checkpoint_raw_sha256",
        ),
        sa.CheckConstraint(
            "schema_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_sw_sector_checkpoint_schema_fingerprint",
        ),
        sa.ForeignKeyConstraint(
            ["last_data_version"], ["sw_sector_publication.data_version"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("capability", "partition_key"),
        comment="申万快照按日恢复 checkpoint；只在两项 publication 成功后推进。",
    )
    op.create_table(
        "sw_sector_quality_result",
        sa.Column(
            "quality_result_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="质量结果永久 UUID。",
        ),
        sa.Column(
            "source_batch_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="被校验的 raw evidence 来源批次。",
        ),
        sa.Column(
            "data_version",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="该规则支撑的不可变消费者发布。",
        ),
        sa.Column(
            "capability",
            sa.String(length=64),
            nullable=False,
            comment="taxonomy 或估值质量规则所属能力。",
        ),
        sa.Column(
            "snapshot_date",
            sa.Date(),
            nullable=False,
            comment="被校验完整快照的上海日历日期。",
        ),
        sa.Column(
            "rule_code",
            sa.String(length=80),
            nullable=False,
            comment="稳定低基数质量规则代码。",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            comment="通过、警告或失败结论。",
        ),
        sa.Column(
            "actual",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="不含原始响应的实际计数或摘要。",
        ),
        sa.Column(
            "expected",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="该规则固定的期望范围或不变量。",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="质量规则执行完成时间。",
        ),
        sa.CheckConstraint(
            "capability IN ('sector.sw.taxonomy', 'sector.sw.valuation')",
            name="ck_sw_sector_quality_capability",
        ),
        sa.CheckConstraint(
            "status IN ('passed', 'warned', 'failed')",
            name="ck_sw_sector_quality_status",
        ),
        sa.ForeignKeyConstraint(
            ["source_batch_id"], ["source_batch.source_batch_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["data_version"], ["sw_sector_publication.data_version"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("quality_result_id"),
        comment="申万完整快照的低基数质量规则证据与发布处置。",
    )


def downgrade() -> None:
    """按依赖逆序删除本迁移创建的申万对象。

    这会永久删除申万方法学、层级、估值、发布、质量与恢复数据；只可在尚未写入业务状态，
    或已完成备份且明确允许销毁时执行。既有板块对象及其发布历史不在本操作范围。
    """
    op.drop_table("sw_sector_quality_result")
    op.drop_table("sw_sector_sync_checkpoint")
    op.drop_index("ix_sw_sector_closure_descendant", table_name="sw_sector_closure")
    op.drop_table("sw_sector_closure")
    op.drop_table("sw_sector_publication")
    op.drop_index("ix_sw_sector_valuation_date_code", table_name="sw_sector_valuation_revision")
    op.drop_index("uq_sw_sector_valuation_current", table_name="sw_sector_valuation_revision")
    op.drop_table("sw_sector_valuation_revision")
    op.drop_index("ix_sw_sector_node_hierarchy", table_name="sw_sector_node_revision")
    op.drop_index("uq_sw_sector_node_current", table_name="sw_sector_node_revision")
    op.drop_table("sw_sector_node_revision")
    op.drop_table("sw_sector_methodology")
