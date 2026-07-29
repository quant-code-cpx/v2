"""允许仅有可信日线时建立最小衍生品合约身份。

Revision ID: 202607290010
Revises: 202607290009
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op

# Alembic 使用的版本标识。
revision = "202607290010"
down_revision = "202607290009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """允许首笔真实日线在产品目录或挂牌日期尚未披露时落入 canonical 身份层。"""
    op.alter_column("derivative_contract", "product_entity_id", nullable=True)
    op.alter_column("derivative_contract", "listed_date", nullable=True)
    op.drop_constraint("ck_derivative_contract_dates", "derivative_contract", type_="check")
    op.create_check_constraint(
        "ck_derivative_contract_dates",
        "derivative_contract",
        "expiry_date IS NULL OR listed_date IS NULL OR expiry_date >= listed_date",
    )


def downgrade() -> None:
    """仅当不存在缺产品或挂牌日的身份时恢复严格目录约束。"""
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM derivative_contract
            WHERE product_entity_id IS NULL OR listed_date IS NULL
          ) THEN
            RAISE EXCEPTION 'minimal identities prevent strict rollback';
          END IF;
        END $$;
        """
    )
    op.drop_constraint("ck_derivative_contract_dates", "derivative_contract", type_="check")
    op.create_check_constraint(
        "ck_derivative_contract_dates",
        "derivative_contract",
        "expiry_date IS NULL OR expiry_date >= listed_date",
    )
    op.alter_column("derivative_contract", "listed_date", nullable=False)
    op.alter_column("derivative_contract", "product_entity_id", nullable=False)
