"""允许仅有可信日线时建立最小衍生品合约身份。

某些来源会先提供可验证的合约日线，后补产品目录或挂牌日期。本迁移允许
`product_entity_id` 和 `listed_date` 暂缺，但不推断或伪造身份信息；到期日校验仍在
挂牌日已知时生效。回退先拒绝存在这类最小身份的数据，避免将可读取事实变成不满足严格
目录约束的记录。

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
    """放宽最小身份的两项目录字段，允许首笔可信日线先落入 `canonical` 身份层。

    不推断缺失的产品或挂牌日；到期日仅在挂牌日已知时才校验先后关系。
    """
    op.alter_column("derivative_contract", "product_entity_id", nullable=True)
    op.alter_column("derivative_contract", "listed_date", nullable=True)
    op.drop_constraint("ck_derivative_contract_dates", "derivative_contract", type_="check")
    op.create_check_constraint(
        "ck_derivative_contract_dates",
        "derivative_contract",
        "expiry_date IS NULL OR listed_date IS NULL OR expiry_date >= listed_date",
    )


def downgrade() -> None:
    """仅在不存在缺产品或挂牌日的最小身份时恢复严格目录约束。

    先检查可空字段，以免仍有待补目录的事实时试图恢复非空约束而失败。
    """
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
