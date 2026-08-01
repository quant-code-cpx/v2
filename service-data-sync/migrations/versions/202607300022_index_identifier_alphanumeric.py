"""放宽中证、国证实证的六码至八码字母数字指数身份约束。

中证目录实际返回 ``H00999``、``L11150`` 和 ``SHHKSI`` 等六码大写字母数字代码；国证目录
还实际返回 ``AITCNYG`` 和 ``39926401`` 等七至八码代码。旧纯数字、`VARCHAR(6)` 约束会使
完整目录在身份写库阶段失败。本迁移只放宽 `index_definition` 的既有列宽和检查约束，不修改
历史 migration、目录语义或任何 release/publication 边界。回退前会拒绝仍存在的字母数字或
超六位身份，避免把已真实入库的观察数据静默删除或截断。

Revision ID: 202607300022
Revises: 202607300021
Create Date: 2026-08-01
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import String

# Alembic 使用的版本标识。
revision = "202607300022"
down_revision = "202607300021"
branch_labels = None
depends_on = None

_CONSTRAINT_NAME = "ck_index_definition_source_code"
_TABLE_NAME = "index_definition"
_ALPHANUMERIC_CODE_CHECK = "source_index_code ~ '^[A-Z0-9]{6,8}$'"
_NUMERIC_CODE_CHECK = "source_index_code ~ '^[0-9]{6}$'"


def upgrade() -> None:
    """将既有纯数字六位列扩大为已由中证、国证目录证实的六码至八码身份。"""
    op.drop_constraint(_CONSTRAINT_NAME, _TABLE_NAME, type_="check")
    op.alter_column(
        _TABLE_NAME,
        "source_index_code",
        existing_type=String(6),
        type_=String(8),
        existing_nullable=False,
    )
    op.create_check_constraint(_CONSTRAINT_NAME, _TABLE_NAME, _ALPHANUMERIC_CODE_CHECK)


def downgrade() -> None:
    """仅在全为六码纯数字时恢复旧列宽和约束，防止截断真实观察记录。"""
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM index_definition
            WHERE source_index_code !~ '^[0-9]{6}$'
          ) THEN
            RAISE EXCEPTION 'index_definition 存在非六码纯数字指数代码，不能恢复旧列宽和约束';
          END IF;
        END $$;
        """
    )
    op.drop_constraint(_CONSTRAINT_NAME, _TABLE_NAME, type_="check")
    op.alter_column(
        _TABLE_NAME,
        "source_index_code",
        existing_type=String(8),
        type_=String(6),
        existing_nullable=False,
    )
    op.create_check_constraint(_CONSTRAINT_NAME, _TABLE_NAME, _NUMERIC_CODE_CHECK)
