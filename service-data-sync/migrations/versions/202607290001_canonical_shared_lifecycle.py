"""创建 canonical 共享来源、质量、release 与血缘支撑表。

Revision ID: 202607290001
Revises: 202607280007
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import Column, func, select, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase

from service_data_sync.infrastructure.database.models.canonical import (
    CanonicalCheckpoint,
    CanonicalDataset,
    CanonicalRecordLineage,
    DatasetRelease,
    DataSource,
    MethodologyVersion,
    NormalizationRun,
    NormalizedRecordManifest,
    QualityEvaluation,
    QualityResult,
    QuarantineRecord,
    RawPayloadManifest,
    SourceDataset,
)

# Alembic 使用的版本标识。
revision = "202607290001"
down_revision = "202607280007"
branch_labels = None
depends_on = None

_MODELS: tuple[type[DeclarativeBase], ...] = (
    CanonicalDataset,
    DataSource,
    SourceDataset,
    MethodologyVersion,
    RawPayloadManifest,
    NormalizationRun,
    NormalizedRecordManifest,
    QualityEvaluation,
    QualityResult,
    QuarantineRecord,
    DatasetRelease,
    CanonicalCheckpoint,
    CanonicalRecordLineage,
)


def upgrade() -> None:
    """新增共享生命周期表，并只给现有表追加兼容期允许为空的外键。"""
    bind = op.get_bind()
    for model in _MODELS:
        model.__table__.create(bind=bind, checkfirst=False)

    op.add_column(
        "source_batch",
        Column(
            "source_dataset_id",
            PG_UUID(as_uuid=True),
            nullable=True,
            comment="真实上游数据产品；历史观察兼容期内允许为空。",
        ),
    )
    op.create_foreign_key(
        "fk_source_batch_source_dataset",
        "source_batch",
        "source_dataset",
        ["source_dataset_id"],
        ["source_dataset_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_source_batch_source_dataset",
        "source_batch",
        ["source_dataset_id"],
    )

    op.add_column(
        "dataset_publication",
        Column(
            "release_id",
            PG_UUID(as_uuid=True),
            nullable=True,
            comment="新 canonical 数据集发布绑定的 immutable release；历史发布兼容期内为空。",
        ),
    )
    op.create_foreign_key(
        "fk_dataset_publication_release",
        "dataset_publication",
        "dataset_release",
        ["release_id"],
        ["release_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_dataset_publication_release",
        "dataset_publication",
        ["release_id"],
        unique=True,
        postgresql_where=text("release_id IS NOT NULL"),
    )


def downgrade() -> None:
    """仅在没有共享生命周期状态或新增关联时删除本次 additive schema。"""
    bind = op.get_bind()
    populated = [
        model.__tablename__
        for model in _MODELS
        if bind.execute(select(func.count()).select_from(model.__table__)).scalar_one() > 0
    ]
    linked_existing_state = bind.execute(
        text(
            """
            SELECT EXISTS (
              SELECT 1 FROM source_batch WHERE source_dataset_id IS NOT NULL
              UNION ALL
              SELECT 1 FROM dataset_publication WHERE release_id IS NOT NULL
            )
            """
        )
    ).scalar_one()
    if populated or linked_existing_state:
        details = ", ".join(populated) if populated else "existing-table links"
        raise RuntimeError(
            "cannot downgrade canonical lifecycle schema after state exists: " + details
        )

    op.drop_index("uq_dataset_publication_release", table_name="dataset_publication")
    op.drop_constraint("fk_dataset_publication_release", "dataset_publication", type_="foreignkey")
    op.drop_column("dataset_publication", "release_id")
    op.drop_index("ix_source_batch_source_dataset", table_name="source_batch")
    op.drop_constraint("fk_source_batch_source_dataset", "source_batch", type_="foreignkey")
    op.drop_column("source_batch", "source_dataset_id")
    for model in reversed(_MODELS):
        model.__table__.drop(bind=bind, checkfirst=False)
