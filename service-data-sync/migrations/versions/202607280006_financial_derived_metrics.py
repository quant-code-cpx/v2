"""建立平台派生公式种子与逐项输入血缘。

Revision ID: 202607280006
Revises: 202607280005
Create Date: 2026-07-28
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from alembic import op
from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from service_data_sync.infrastructure.database.models.financial.financial_methodology import (
    FinancialMethodology,
)
from service_data_sync.infrastructure.database.models.financial.financial_metric_definition import (
    FinancialMetricDefinition,
)

# Alembic 使用的版本标识。
revision = "202607280006"
down_revision = "202607280005"
branch_labels = None
depends_on = None

_METHODOLOGY_CODE = "platform.financial-derivation"
_METHODOLOGY_VERSION = 1
_METHODOLOGY_ID = uuid5(NAMESPACE_URL, f"quant-v2:{_METHODOLOGY_CODE}:{_METHODOLOGY_VERSION}")
_FORMULA_DEFINITIONS = (
    (
        "platform.operating_revenue.single_quarter",
        "营业收入（单季）",
        "statement.income_statement.total-operate-income",
        "SINGLE_QUARTER",
    ),
    (
        "platform.operating_revenue.ttm",
        "营业收入（TTM）",
        "statement.income_statement.total-operate-income",
        "TTM",
    ),
    (
        "platform.net_profit_parent.single_quarter",
        "归母净利润（单季）",
        "statement.income_statement.parent-netprofit",
        "SINGLE_QUARTER",
    ),
    (
        "platform.net_profit_parent.ttm",
        "归母净利润（TTM）",
        "statement.income_statement.parent-netprofit",
        "TTM",
    ),
)


def upgrade() -> None:
    """创建可回链 raw 证据的输入 manifest，并登记不可变公式与字段字典。"""
    op.execute(
        """
        CREATE TABLE financial_derivation_input (
          derived_report_period DATE NOT NULL,
          derived_metric_revision_id UUID NOT NULL,
          input_sequence INTEGER NOT NULL CHECK (input_sequence > 0),
          input_role VARCHAR(24) NOT NULL CHECK (input_role IN (
            'CURRENT_YTD', 'PREVIOUS_YTD', 'PRIOR_ANNUAL', 'PRIOR_SAME_QUARTER'
          )),
          input_report_period DATE NOT NULL,
          input_revision_id UUID NOT NULL,
          input_metric_id BIGINT NOT NULL
            REFERENCES financial_metric_definition(metric_id) ON DELETE RESTRICT,
          input_source_batch_id UUID NOT NULL
            REFERENCES source_batch(source_batch_id) ON DELETE RESTRICT,
          input_data_version UUID NOT NULL
            REFERENCES dataset_publication(data_version) ON DELETE RESTRICT,
          input_value NUMERIC(38, 12) NOT NULL,
          input_unit VARCHAR(32) NOT NULL,
          input_currency VARCHAR(3),
          input_currency_null_reason VARCHAR(24),
          created_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY (
            derived_report_period, derived_metric_revision_id, input_sequence
          ),
          CONSTRAINT fk_financial_derivation_input_derived_revision
            FOREIGN KEY (derived_report_period, derived_metric_revision_id)
            REFERENCES derived_financial_metric_revision(report_period, metric_revision_id)
            ON DELETE RESTRICT,
          CONSTRAINT fk_financial_derivation_input_report_revision
            FOREIGN KEY (input_report_period, input_revision_id)
            REFERENCES financial_report_revision(report_period, revision_id)
            ON DELETE RESTRICT,
          CONSTRAINT ck_financial_derivation_input_currency CHECK (
            (input_currency IS NOT NULL AND input_currency_null_reason IS NULL)
            OR (
              input_currency IS NULL
              AND input_currency_null_reason IN (
                'NOT_APPLICABLE', 'UNKNOWN_SOURCE', 'MIXED_CURRENCIES'
              )
            )
          )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_financial_derivation_input_source
        ON financial_derivation_input (input_source_batch_id, input_report_period)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_financial_derivation_input_publication
        ON financial_derivation_input (input_data_version, derived_report_period)
        """
    )
    _seed_methodology_and_metrics()
    _comment_derivation_input()


def downgrade() -> None:
    """仅在没有派生 revision 或 publication 时删除血缘表与本版本治理种子。"""
    connection = op.get_bind()
    has_state = connection.execute(
        text(
            """
            SELECT EXISTS (
              SELECT 1 FROM financial_derivation_input
              UNION ALL
              SELECT 1 FROM derived_financial_metric_revision
                WHERE methodology_id = :methodology_id
              UNION ALL
              SELECT 1 FROM financial_publication
                WHERE methodology_id = :methodology_id
            )
            """
        ),
        {"methodology_id": _METHODOLOGY_ID},
    ).scalar_one()
    if has_state:
        raise RuntimeError("cannot downgrade financial derivation after derived state exists")
    op.execute("DROP TABLE financial_derivation_input")
    codes = tuple(item[0] for item in _FORMULA_DEFINITIONS)
    connection.execute(
        delete(FinancialMetricDefinition).where(
            FinancialMetricDefinition.code.in_(codes),
            FinancialMetricDefinition.dictionary_version == 1,
        )
    )
    connection.execute(
        delete(FinancialMethodology).where(FinancialMethodology.methodology_id == _METHODOLOGY_ID)
    )


def _seed_methodology_and_metrics() -> None:
    """登记四个公式输出和唯一平台方法学，运行时只按该不可变版本发布。"""
    connection = op.get_bind()
    semantic_spec = json.dumps(
        {
            "formulaVersion": 1,
            "nullSemantics": "missing_any_input_omits_output",
            "unitSemantics": "all_inputs_must_share_unit_and_currency",
            "formulas": _FORMULA_DEFINITIONS,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute(
        pg_insert(FinancialMethodology)
        .values(
            methodology_id=_METHODOLOGY_ID,
            code=_METHODOLOGY_CODE,
            version=_METHODOLOGY_VERSION,
            capability="financial.derived-metric",
            source_code="quant-v2.platform",
            status="validated",
            semantic_spec_sha256=hashlib.sha256(semantic_spec.encode()).hexdigest(),
            created_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=("code", "version"))
    )
    rows = [
        {
            "code": code,
            "label": label,
            "origin": "platform_derived",
            "statement_type": None,
            "value_domain": "monetary",
            "canonical_unit": "inherited_source_unit",
            "currency_required": True,
            "sign_convention": "formula_subtraction_v1",
            "dictionary_version": 1,
            "status": "active",
        }
        for code, label, _input_code, _basis in _FORMULA_DEFINITIONS
    ]
    connection.execute(
        pg_insert(FinancialMetricDefinition)
        .values(rows)
        .on_conflict_do_nothing(index_elements=("code", "dictionary_version"))
    )


def _comment_derivation_input() -> None:
    """为新增血缘表和列写入中文数据字典说明。"""
    comments = {
        "derived_report_period": "派生指标所在报告期。",
        "derived_metric_revision_id": "派生指标 revision UUID。",
        "input_sequence": "公式输入的稳定顺序，从一开始。",
        "input_role": "当前累计、上期累计、上年全年或上年同期角色。",
        "input_report_period": "来源报表事实的报告期。",
        "input_revision_id": "来源报表 canonical revision UUID。",
        "input_metric_id": "来源治理字段字典键。",
        "input_source_batch_id": "可继续回链 raw URI 的来源观察批次。",
        "input_data_version": "计算时冻结的报表 publication 版本。",
        "input_value": "参与公式的精确输入值。",
        "input_unit": "输入事实规范单位；所有输入必须可比。",
        "input_currency": "输入事实已知时的 ISO 4217 币种。",
        "input_currency_null_reason": "输入币种为空时的受控原因。",
        "created_at": "输入 manifest 写入时间。",
    }
    op.execute(
        "COMMENT ON TABLE financial_derivation_input IS "
        "'平台派生指标逐项输入 manifest；可回链报表 revision、raw batch 和发布版本。'"
    )
    for column_name, comment in comments.items():
        escaped = comment.replace("'", "''")
        op.execute(f"COMMENT ON COLUMN financial_derivation_input.{column_name} IS '{escaped}'")
