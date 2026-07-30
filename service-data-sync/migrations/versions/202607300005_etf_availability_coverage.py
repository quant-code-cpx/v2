"""为 ETF 空态观察增加实体与覆盖窗口列。

Revision ID: 202607300005
Revises: 202607300004
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "202607300005"
down_revision = "202607300004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加可索引的实体和日期覆盖范围，使短窗口失败可提示长窗口旧 publication。"""
    op.execute(
        """
        ALTER TABLE dataset_availability_observation
          ADD COLUMN IF NOT EXISTS entity_partition VARCHAR(160),
          ADD COLUMN IF NOT EXISTS coverage_from DATE,
          ADD COLUMN IF NOT EXISTS coverage_to DATE;

        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conname = 'ck_dataset_availability_observation_coverage'
              AND conrelid = 'dataset_availability_observation'::regclass
          ) THEN
            ALTER TABLE dataset_availability_observation
              ADD CONSTRAINT ck_dataset_availability_observation_coverage
              CHECK (
                (
                  entity_partition IS NULL
                  AND coverage_from IS NULL
                  AND coverage_to IS NULL
                )
                OR
                (
                  entity_partition IS NOT NULL
                  AND coverage_from IS NOT NULL
                  AND coverage_to IS NOT NULL
                  AND coverage_from <= coverage_to
                )
              );
          END IF;
        END
        $$;

        CREATE INDEX IF NOT EXISTS ix_dataset_availability_observation_entity_coverage
          ON dataset_availability_observation
            (dataset, entity_partition, coverage_to, coverage_from, observed_at DESC)
          WHERE superseded_at IS NULL AND entity_partition IS NOT NULL;

        COMMENT ON COLUMN dataset_availability_observation.entity_partition IS
          'ETF 上市工具或交易所目录的受控实体分区；旧观察未回填时为空。';
        COMMENT ON COLUMN dataset_availability_observation.coverage_from IS
          '本次空态或失败观察覆盖的包含式起始业务日期。';
        COMMENT ON COLUMN dataset_availability_observation.coverage_to IS
          '本次空态或失败观察覆盖的包含式结束业务日期。';
        """
    )


def downgrade() -> None:
    """仅在尚无结构化覆盖观察时移除列，避免丢失延迟和来源失败证据。"""
    populated = (
        op.get_bind()
        .execute(
            text(
                """
                SELECT EXISTS (
                  SELECT 1
                  FROM dataset_availability_observation
                  WHERE entity_partition IS NOT NULL
                     OR coverage_from IS NOT NULL
                     OR coverage_to IS NOT NULL
                )
                """
            )
        )
        .scalar_one()
    )
    if populated:
        raise RuntimeError("cannot remove ETF availability coverage after observations exist")
    op.execute(
        """
        DROP INDEX IF EXISTS ix_dataset_availability_observation_entity_coverage;
        ALTER TABLE dataset_availability_observation
          DROP CONSTRAINT IF EXISTS ck_dataset_availability_observation_coverage,
          DROP COLUMN IF EXISTS coverage_to,
          DROP COLUMN IF EXISTS coverage_from,
          DROP COLUMN IF EXISTS entity_partition;
        """
    )
