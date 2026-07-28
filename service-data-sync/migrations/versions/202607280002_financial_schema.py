"""创建财务报表、指标和估值的空 canonical schema。

Revision ID: 202607280002
Revises: 202607280001
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Iterable

from alembic import op
from sqlalchemy import Connection, text
from sqlalchemy.orm import DeclarativeBase

from service_data_sync.infrastructure.database.models.financial import (
    derived_financial_metric_revision as derived_financial_metric_revision_model,
)
from service_data_sync.infrastructure.database.models.financial import (
    provider_financial_metric_revision as provider_financial_metric_revision_model,
)
from service_data_sync.infrastructure.database.models.financial import (
    valuation_observation_revision as valuation_observation_revision_model,
)
from service_data_sync.infrastructure.database.models.financial.financial_change_checkpoint import (
    FinancialChangeCheckpoint,
)
from service_data_sync.infrastructure.database.models.financial.financial_field_quarantine import (
    FinancialFieldQuarantine,
)
from service_data_sync.infrastructure.database.models.financial.financial_methodology import (
    FinancialMethodology,
)
from service_data_sync.infrastructure.database.models.financial.financial_metric_definition import (
    FinancialMetricDefinition,
)
from service_data_sync.infrastructure.database.models.financial.financial_publication import (
    FinancialPublication,
)
from service_data_sync.infrastructure.database.models.financial.financial_quality_result import (
    FinancialQualityResult,
)
from service_data_sync.infrastructure.database.models.financial.financial_report import (
    FinancialReport,
)
from service_data_sync.infrastructure.database.models.financial.financial_report_revision import (
    FinancialReportRevision,
)
from service_data_sync.infrastructure.database.models.financial.financial_statement_fact import (
    FinancialStatementFact,
)

# Alembic 使用的版本标识。
revision = "202607280002"
down_revision = "202607280001"
branch_labels = None
depends_on = None

_FINANCIAL_MODELS: tuple[type[DeclarativeBase], ...] = (
    FinancialMethodology,
    FinancialMetricDefinition,
    FinancialReport,
    FinancialReportRevision,
    FinancialStatementFact,
    provider_financial_metric_revision_model.ProviderFinancialMetricRevision,
    derived_financial_metric_revision_model.DerivedFinancialMetricRevision,
    valuation_observation_revision_model.ValuationObservationRevision,
    FinancialFieldQuarantine,
    FinancialQualityResult,
    FinancialPublication,
    FinancialChangeCheckpoint,
)


def upgrade() -> None:
    """创建默认空置的财务 schema，并预建近期历史与下一年度分区。"""
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute(
        "ALTER TABLE dataset_publication "
        "ADD CONSTRAINT uq_dataset_publication_data_version UNIQUE (data_version)"
    )
    _create_reference_tables()
    _create_revision_tables()
    _create_operational_tables()
    _create_initial_partitions()
    _sync_model_comments(_FINANCIAL_MODELS)


def downgrade() -> None:
    """仅在所有新增财务表为空时回退，避免删除 canonical 或治理证据。"""
    has_rows = (
        op.get_bind()
        .execute(
            text(
                """
            SELECT EXISTS (
              SELECT 1 FROM financial_methodology
              UNION ALL SELECT 1 FROM financial_metric_definition
              UNION ALL SELECT 1 FROM financial_report
              UNION ALL SELECT 1 FROM financial_report_revision
              UNION ALL SELECT 1 FROM financial_statement_fact
              UNION ALL SELECT 1 FROM provider_financial_metric_revision
              UNION ALL SELECT 1 FROM derived_financial_metric_revision
              UNION ALL SELECT 1 FROM valuation_observation_revision
              UNION ALL SELECT 1 FROM financial_field_quarantine
              UNION ALL SELECT 1 FROM financial_quality_result
              UNION ALL SELECT 1 FROM financial_publication
              UNION ALL SELECT 1 FROM financial_change_checkpoint
            )
            """
            )
        )
        .scalar_one()
    )
    if has_rows:
        raise RuntimeError("cannot downgrade financial schema after financial state exists")

    op.execute("DROP TABLE financial_change_checkpoint")
    op.execute("DROP TABLE financial_publication")
    op.execute("DROP TABLE financial_quality_result")
    op.execute("DROP TABLE financial_field_quarantine")
    op.execute("DROP TABLE valuation_observation_revision")
    op.execute("DROP TABLE derived_financial_metric_revision")
    op.execute("DROP TABLE provider_financial_metric_revision")
    op.execute("DROP TABLE financial_statement_fact")
    op.execute("DROP TABLE financial_report_revision")
    op.execute("DROP TABLE financial_report")
    op.execute("DROP TABLE financial_metric_definition")
    op.execute("DROP TABLE financial_methodology")
    op.execute(
        "ALTER TABLE dataset_publication DROP CONSTRAINT uq_dataset_publication_data_version"
    )


def _create_reference_tables() -> None:
    """创建方法学、字段字典和报表逻辑身份，所有业务值仍写入 revision 表。"""
    op.execute(
        """
        CREATE TABLE financial_methodology (
          methodology_id UUID PRIMARY KEY,
          code VARCHAR(80) NOT NULL,
          version SMALLINT NOT NULL CHECK (version > 0),
          capability VARCHAR(64) NOT NULL CHECK (capability IN (
            'financial.report', 'financial.provider-metric',
            'financial.derived-metric', 'financial.valuation'
          )),
          source_code VARCHAR(80) NOT NULL,
          status VARCHAR(16) NOT NULL CHECK (status IN ('draft', 'validated', 'retired')),
          semantic_spec_sha256 CHAR(64) NOT NULL CHECK (
            semantic_spec_sha256 ~ '^[0-9a-f]{64}$'
          ),
          created_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT uq_financial_methodology_code_version UNIQUE (code, version)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE financial_metric_definition (
          metric_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
          code VARCHAR(80) NOT NULL,
          label VARCHAR(160) NOT NULL,
          origin VARCHAR(32) NOT NULL CHECK (origin IN (
            'statement_fact', 'provider_reported', 'platform_derived', 'valuation'
          )),
          statement_type VARCHAR(32) CHECK (
            statement_type IS NULL OR statement_type IN (
              'BALANCE_SHEET', 'INCOME_STATEMENT', 'CASH_FLOW_STATEMENT'
            )
          ),
          value_domain VARCHAR(24) NOT NULL CHECK (
            value_domain IN ('monetary', 'ratio', 'count', 'per_share', 'other')
          ),
          canonical_unit VARCHAR(32) NOT NULL,
          currency_required BOOLEAN NOT NULL,
          sign_convention VARCHAR(32) NOT NULL,
          dictionary_version INTEGER NOT NULL CHECK (dictionary_version > 0),
          status VARCHAR(16) NOT NULL CHECK (status IN ('draft', 'active', 'retired')),
          CONSTRAINT ck_financial_metric_definition_origin_statement_type CHECK (
            (origin = 'statement_fact' AND statement_type IS NOT NULL)
            OR (origin <> 'statement_fact' AND statement_type IS NULL)
          ),
          CONSTRAINT uq_financial_metric_definition_code UNIQUE (code, dictionary_version)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_financial_metric_definition_active_code
        ON financial_metric_definition (code) WHERE status = 'active'
        """
    )
    op.execute(
        """
        CREATE TABLE financial_report (
          financial_report_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
          report_ref UUID NOT NULL UNIQUE,
          security_id BIGINT NOT NULL REFERENCES equity_instrument(security_id) ON DELETE RESTRICT,
          methodology_id UUID NOT NULL REFERENCES financial_methodology(methodology_id)
            ON DELETE RESTRICT,
          statement_type VARCHAR(32) NOT NULL CHECK (statement_type IN (
            'BALANCE_SHEET', 'INCOME_STATEMENT', 'CASH_FLOW_STATEMENT'
          )),
          report_period DATE NOT NULL,
          period_basis VARCHAR(24) NOT NULL CHECK (period_basis IN (
            'POINT_IN_TIME', 'YEAR_TO_DATE', 'SINGLE_QUARTER', 'TTM'
          )),
          statement_scope VARCHAR(16) NOT NULL CHECK (statement_scope IN (
            'CONSOLIDATED', 'PARENT', 'UNKNOWN'
          )),
          currency CHAR(3),
          currency_null_reason VARCHAR(24),
          report_type VARCHAR(64) NOT NULL,
          superseded_by BIGINT REFERENCES financial_report(financial_report_id) ON DELETE RESTRICT,
          CONSTRAINT ck_financial_report_currency CHECK (
            (currency IS NOT NULL AND currency_null_reason IS NULL)
            OR (currency IS NULL AND currency_null_reason IN (
              'NOT_APPLICABLE', 'UNKNOWN_SOURCE', 'MIXED_CURRENCIES'
            ))
          ),
          CONSTRAINT uq_financial_report_logical_key UNIQUE NULLS NOT DISTINCT (
            security_id, methodology_id, statement_type, report_period, period_basis,
            statement_scope, currency, report_type
          )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_financial_report_security_period "
        "ON financial_report (security_id, report_period)"
    )


def _create_revision_tables() -> None:
    """创建双时态报表、指标和估值父表，物理子表由分区函数统一管理。"""
    op.execute(
        """
        CREATE TABLE financial_report_revision (
          report_period DATE NOT NULL,
          revision_id UUID NOT NULL,
          financial_report_id BIGINT NOT NULL REFERENCES financial_report(financial_report_id)
            ON DELETE RESTRICT,
          revision INTEGER NOT NULL CHECK (revision > 0),
          announcement_date DATE,
          provider_update_at TIMESTAMPTZ,
          audit_status VARCHAR(16) NOT NULL CHECK (
            audit_status IN ('AUDITED', 'UNAUDITED', 'UNKNOWN')
          ),
          effective_from DATE NOT NULL,
          effective_to DATE,
          known_from TIMESTAMPTZ NOT NULL,
          known_to TIMESTAMPTZ,
          knowledge_basis VARCHAR(24) NOT NULL CHECK (knowledge_basis IN (
            'OFFICIAL_ANNOUNCEMENT', 'PROVIDER_UPDATE', 'OBSERVED_AT'
          )),
          knowledge_confidence VARCHAR(16) NOT NULL CHECK (knowledge_confidence IN (
            'HIGH', 'MEDIUM', 'CONSERVATIVE'
          )),
          observed_at TIMESTAMPTZ NOT NULL,
          source_batch_id UUID NOT NULL REFERENCES source_batch(source_batch_id) ON DELETE RESTRICT,
          content_sha256 CHAR(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
          quality_status VARCHAR(16) NOT NULL CHECK (
            quality_status IN ('passed', 'warned', 'quarantined')
          ),
          created_at TIMESTAMPTZ NOT NULL,
          effective_range DATERANGE GENERATED ALWAYS AS (
            daterange(effective_from, effective_to, '[)')
          ) STORED,
          knowledge_range TSTZRANGE GENERATED ALWAYS AS (
            tstzrange(known_from, known_to, '[)')
          ) STORED,
          PRIMARY KEY (report_period, revision_id),
          CONSTRAINT uq_financial_report_revision_number UNIQUE (
            report_period, financial_report_id, revision
          ),
          CONSTRAINT ck_financial_report_revision_effective_range CHECK (
            effective_to IS NULL OR effective_to > effective_from
          ),
          CONSTRAINT ck_financial_report_revision_knowledge_range CHECK (
            known_to IS NULL OR known_to > known_from
          ),
          CONSTRAINT ck_financial_report_revision_known_after_observed CHECK (
            known_from >= observed_at
          )
        ) PARTITION BY RANGE (report_period)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_financial_report_revision_current
        ON financial_report_revision (report_period, financial_report_id)
        WHERE known_to IS NULL
        """
    )
    op.execute(
        """
        CREATE TABLE financial_statement_fact (
          report_period DATE NOT NULL,
          revision_id UUID NOT NULL,
          metric_id BIGINT NOT NULL REFERENCES financial_metric_definition(metric_id)
            ON DELETE RESTRICT,
          value NUMERIC(38, 10),
          null_reason VARCHAR(24),
          currency CHAR(3),
          currency_null_reason VARCHAR(24),
          original_unit VARCHAR(32) NOT NULL,
          canonical_unit VARCHAR(32) NOT NULL,
          scale_factor NUMERIC(30, 12) NOT NULL CHECK (scale_factor > 0),
          sign_convention VARCHAR(32) NOT NULL,
          PRIMARY KEY (report_period, revision_id, metric_id),
          CONSTRAINT fk_financial_statement_fact_revision FOREIGN KEY (report_period, revision_id)
            REFERENCES financial_report_revision(report_period, revision_id) ON DELETE RESTRICT,
          CONSTRAINT ck_financial_statement_fact_value CHECK (
            (value IS NOT NULL AND null_reason IS NULL)
            OR (value IS NULL AND null_reason IN (
              'NOT_REPORTED', 'NOT_APPLICABLE', 'UPSTREAM_NULL'
            ))
          ),
          CONSTRAINT ck_financial_statement_fact_currency CHECK (
            (currency IS NOT NULL AND currency_null_reason IS NULL)
            OR (currency IS NULL AND currency_null_reason IN (
              'NOT_APPLICABLE', 'UNKNOWN_SOURCE', 'MIXED_CURRENCIES'
            ))
          )
        ) PARTITION BY RANGE (report_period)
        """
    )
    op.execute(
        "CREATE INDEX ix_financial_statement_fact_metric_period "
        "ON financial_statement_fact (metric_id, report_period)"
    )
    _create_metric_revision_table("provider_financial_metric_revision", "provider")
    _create_metric_revision_table("derived_financial_metric_revision", "derived")
    op.execute(
        """
        CREATE TABLE valuation_observation_revision (
          observation_date DATE NOT NULL,
          valuation_revision_id UUID NOT NULL,
          security_id BIGINT NOT NULL REFERENCES equity_instrument(security_id) ON DELETE RESTRICT,
          metric_id BIGINT NOT NULL REFERENCES financial_metric_definition(metric_id)
            ON DELETE RESTRICT,
          methodology_id UUID NOT NULL REFERENCES financial_methodology(methodology_id)
            ON DELETE RESTRICT,
          revision INTEGER NOT NULL CHECK (revision > 0),
          value NUMERIC(38, 12) NOT NULL,
          unit VARCHAR(32) NOT NULL,
          currency CHAR(3),
          currency_null_reason VARCHAR(24),
          finality VARCHAR(32) NOT NULL CHECK (finality = 'PROVIDER_OBSERVATION'),
          effective_from DATE NOT NULL,
          effective_to DATE,
          known_from TIMESTAMPTZ NOT NULL,
          known_to TIMESTAMPTZ,
          knowledge_basis VARCHAR(24) NOT NULL CHECK (knowledge_basis IN (
            'OFFICIAL_ANNOUNCEMENT', 'PROVIDER_UPDATE', 'OBSERVED_AT'
          )),
          knowledge_confidence VARCHAR(16) NOT NULL CHECK (knowledge_confidence IN (
            'HIGH', 'MEDIUM', 'CONSERVATIVE'
          )),
          observed_at TIMESTAMPTZ NOT NULL,
          source_batch_id UUID NOT NULL REFERENCES source_batch(source_batch_id) ON DELETE RESTRICT,
          content_sha256 CHAR(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
          quality_status VARCHAR(16) NOT NULL CHECK (
            quality_status IN ('passed', 'warned', 'quarantined')
          ),
          created_at TIMESTAMPTZ NOT NULL,
          effective_range DATERANGE GENERATED ALWAYS AS (
            daterange(effective_from, effective_to, '[)')
          ) STORED,
          knowledge_range TSTZRANGE GENERATED ALWAYS AS (
            tstzrange(known_from, known_to, '[)')
          ) STORED,
          PRIMARY KEY (observation_date, valuation_revision_id),
          CONSTRAINT uq_valuation_observation_revision UNIQUE (
            observation_date, security_id, metric_id, methodology_id, revision
          ),
          CONSTRAINT ck_valuation_observation_currency CHECK (
            (currency IS NOT NULL AND currency_null_reason IS NULL)
            OR (currency IS NULL AND currency_null_reason IN (
              'NOT_APPLICABLE', 'UNKNOWN_SOURCE', 'MIXED_CURRENCIES'
            ))
          ),
          CONSTRAINT ck_valuation_observation_effective_range CHECK (
            effective_to IS NULL OR effective_to > effective_from
          ),
          CONSTRAINT ck_valuation_observation_knowledge_range CHECK (
            known_to IS NULL OR known_to > known_from
          ),
          CONSTRAINT ck_valuation_observation_known_after_observed CHECK (
            known_from >= observed_at
          )
        ) PARTITION BY RANGE (observation_date)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_valuation_observation_current
        ON valuation_observation_revision (observation_date, security_id, metric_id, methodology_id)
        WHERE known_to IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_valuation_observation_series
        ON valuation_observation_revision (security_id, metric_id, observation_date)
        INCLUDE (value, methodology_id)
        """
    )


def _create_metric_revision_table(table_name: str, kind: str) -> None:
    """按供应商或派生指标差异创建带双时态约束的年度分区父表。"""
    extra_columns = (
        """
          formula_version INTEGER NOT NULL CHECK (formula_version > 0),
          input_manifest_sha256 CHAR(64) NOT NULL CHECK (
            input_manifest_sha256 ~ '^[0-9a-f]{64}$'
          ),
          derivation_run_id UUID NOT NULL REFERENCES sync_run(run_id) ON DELETE RESTRICT,
          computed_at TIMESTAMPTZ NOT NULL,
        """
        if kind == "derived"
        else ""
    )
    formula_key = ", formula_version" if kind == "derived" else ""
    op.execute(
        f"""
        CREATE TABLE {table_name} (
          report_period DATE NOT NULL,
          metric_revision_id UUID NOT NULL,
          security_id BIGINT NOT NULL REFERENCES equity_instrument(security_id) ON DELETE RESTRICT,
          metric_id BIGINT NOT NULL REFERENCES financial_metric_definition(metric_id)
            ON DELETE RESTRICT,
          methodology_id UUID NOT NULL REFERENCES financial_methodology(methodology_id)
            ON DELETE RESTRICT,
          period_basis VARCHAR(24) NOT NULL CHECK (period_basis IN (
            'POINT_IN_TIME', 'YEAR_TO_DATE', 'SINGLE_QUARTER', 'TTM'
          )),
          statement_scope VARCHAR(16) NOT NULL CHECK (statement_scope IN (
            'CONSOLIDATED', 'PARENT', 'UNKNOWN'
          )),
          value NUMERIC(38, 12) NOT NULL,
          unit VARCHAR(32) NOT NULL,
          currency CHAR(3),
          currency_null_reason VARCHAR(24),
          {extra_columns}
          effective_from DATE NOT NULL,
          effective_to DATE,
          known_from TIMESTAMPTZ NOT NULL,
          known_to TIMESTAMPTZ,
          knowledge_basis VARCHAR(24) NOT NULL CHECK (knowledge_basis IN (
            'OFFICIAL_ANNOUNCEMENT', 'PROVIDER_UPDATE', 'OBSERVED_AT'
          )),
          knowledge_confidence VARCHAR(16) NOT NULL CHECK (knowledge_confidence IN (
            'HIGH', 'MEDIUM', 'CONSERVATIVE'
          )),
          observed_at TIMESTAMPTZ NOT NULL,
          source_batch_id UUID NOT NULL REFERENCES source_batch(source_batch_id) ON DELETE RESTRICT,
          revision INTEGER NOT NULL CHECK (revision > 0),
          content_sha256 CHAR(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{{64}}$'),
          quality_status VARCHAR(16) NOT NULL CHECK (
            quality_status IN ('passed', 'warned', 'quarantined')
          ),
          created_at TIMESTAMPTZ NOT NULL,
          effective_range DATERANGE GENERATED ALWAYS AS (
            daterange(effective_from, effective_to, '[)')
          ) STORED,
          knowledge_range TSTZRANGE GENERATED ALWAYS AS (
            tstzrange(known_from, known_to, '[)')
          ) STORED,
          PRIMARY KEY (report_period, metric_revision_id),
          CONSTRAINT uq_{kind}_financial_metric_revision UNIQUE (
            report_period, security_id, metric_id, methodology_id, period_basis, statement_scope
            {formula_key}, revision
          ),
          CONSTRAINT ck_{kind}_financial_metric_currency CHECK (
            (currency IS NOT NULL AND currency_null_reason IS NULL)
            OR (currency IS NULL AND currency_null_reason IN (
              'NOT_APPLICABLE', 'UNKNOWN_SOURCE', 'MIXED_CURRENCIES'
            ))
          ),
          CONSTRAINT ck_{kind}_financial_metric_effective_range CHECK (
            effective_to IS NULL OR effective_to > effective_from
          ),
          CONSTRAINT ck_{kind}_financial_metric_knowledge_range CHECK (
            known_to IS NULL OR known_to > known_from
          ),
          CONSTRAINT ck_{kind}_financial_metric_known_after_observed CHECK (
            known_from >= observed_at
          )
        ) PARTITION BY RANGE (report_period)
        """
    )
    op.execute(
        f"""
        CREATE UNIQUE INDEX uq_{kind}_financial_metric_current
        ON {table_name} (
          report_period, security_id, metric_id, methodology_id,
          period_basis, statement_scope{formula_key}
        ) WHERE known_to IS NULL
        """
    )
    op.execute(
        f"CREATE INDEX ix_{kind}_financial_metric_series "
        f"ON {table_name} (security_id, metric_id, report_period)"
    )


def _create_operational_tables() -> None:
    """创建隔离、质量、发布与摘要 checkpoint，默认没有任何来源策略或数据。"""
    op.execute(
        """
        CREATE TABLE financial_field_quarantine (
          quarantine_id UUID PRIMARY KEY,
          source_batch_id UUID NOT NULL REFERENCES source_batch(source_batch_id) ON DELETE RESTRICT,
          capability VARCHAR(64) NOT NULL,
          statement_type VARCHAR(32),
          upstream_field VARCHAR(200) NOT NULL,
          upstream_type VARCHAR(64) NOT NULL,
          schema_fingerprint CHAR(64) NOT NULL CHECK (schema_fingerprint ~ '^[0-9a-f]{64}$'),
          sample_sha256 CHAR(64) NOT NULL CHECK (sample_sha256 ~ '^[0-9a-f]{64}$'),
          first_seen_at TIMESTAMPTZ NOT NULL,
          last_seen_at TIMESTAMPTZ NOT NULL,
          status VARCHAR(16) NOT NULL CHECK (status IN ('open', 'resolved', 'ignored')),
          resolution VARCHAR(500),
          CONSTRAINT uq_financial_field_quarantine_field UNIQUE (
            capability, schema_fingerprint, upstream_field
          )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_financial_field_quarantine_open
        ON financial_field_quarantine (status, last_seen_at) WHERE status = 'open'
        """
    )
    op.execute(
        """
        CREATE TABLE financial_quality_result (
          quality_result_id UUID PRIMARY KEY,
          source_batch_id UUID NOT NULL REFERENCES source_batch(source_batch_id) ON DELETE RESTRICT,
          data_version UUID,
          rule_code VARCHAR(64) NOT NULL,
          rule_version SMALLINT NOT NULL CHECK (rule_version > 0),
          severity VARCHAR(16) NOT NULL CHECK (severity IN ('info', 'warning', 'blocking')),
          status VARCHAR(16) NOT NULL CHECK (
            status IN ('passed', 'warned', 'failed', 'quarantined')
          ),
          measured NUMERIC(38, 12),
          threshold NUMERIC(38, 12),
          dimension VARCHAR(200),
          created_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT uq_financial_quality_result_batch_rule UNIQUE (
            source_batch_id, rule_code, rule_version
          )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_financial_quality_result_failed
        ON financial_quality_result (created_at)
        WHERE status IN ('failed', 'quarantined')
        """
    )
    op.execute(
        """
        CREATE TABLE financial_publication (
          data_version UUID PRIMARY KEY REFERENCES dataset_publication(data_version)
            ON DELETE RESTRICT,
          capability VARCHAR(64) NOT NULL CHECK (capability IN (
            'financial.report', 'financial.provider-metric',
            'financial.derived-metric', 'financial.valuation'
          )),
          security_id BIGINT NOT NULL REFERENCES equity_instrument(security_id) ON DELETE RESTRICT,
          methodology_id UUID NOT NULL REFERENCES financial_methodology(methodology_id)
            ON DELETE RESTRICT,
          effective_as_of DATE NOT NULL,
          knowledge_cutoff TIMESTAMPTZ NOT NULL,
          row_count INTEGER NOT NULL CHECK (row_count >= 0),
          content_sha256 CHAR(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
          published_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT uq_financial_publication_scope_version UNIQUE (
            capability, security_id, methodology_id, data_version
          )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE financial_change_checkpoint (
          capability VARCHAR(64) NOT NULL,
          partition_key VARCHAR(240) NOT NULL,
          summary_sha256 CHAR(64) NOT NULL CHECK (summary_sha256 ~ '^[0-9a-f]{64}$'),
          provider_watermark VARCHAR(240),
          last_data_version UUID REFERENCES dataset_publication(data_version) ON DELETE RESTRICT,
          last_success_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY (capability, partition_key)
        )
        """
    )


def _create_initial_partitions() -> None:
    """按执行日期预建五年历史、当年和下一年度，写入未来年份仍须显式扩展。"""
    current_year = (
        op.get_bind().execute(text("SELECT EXTRACT(YEAR FROM CURRENT_DATE)::integer")).scalar_one()
    )
    for year in range(current_year - 5, current_year + 2):
        _create_year_partitions(year)


def _create_year_partitions(year: int) -> None:
    """创建一个自然年度的子表和双时态排斥约束，阻止同一逻辑键的时间重叠。"""
    next_year = year + 1
    range_sql = f"FOR VALUES FROM ('{year}-01-01') TO ('{next_year}-01-01')"
    tables = (
        ("financial_report_revision", "financial_report_id WITH ="),
        ("financial_statement_fact", None),
        (
            "provider_financial_metric_revision",
            "security_id WITH =, metric_id WITH =, methodology_id WITH =, "
            "period_basis WITH =, statement_scope WITH =",
        ),
        (
            "derived_financial_metric_revision",
            "security_id WITH =, metric_id WITH =, methodology_id WITH =, "
            "period_basis WITH =, statement_scope WITH =, formula_version WITH =",
        ),
        (
            "valuation_observation_revision",
            "security_id WITH =, metric_id WITH =, methodology_id WITH =",
        ),
    )
    for parent_table, logical_key in tables:
        child_table = f"{parent_table}_{year}"
        op.execute(
            f"CREATE TABLE IF NOT EXISTS {child_table} PARTITION OF {parent_table} {range_sql}"
        )
        if logical_key is None:
            continue
        constraint_name = f"ex_{parent_table}_{year}_bitemporal"
        op.execute(
            f"""
            ALTER TABLE {child_table}
            ADD CONSTRAINT {constraint_name}
            EXCLUDE USING gist (
              {logical_key},
              effective_range WITH &&,
              knowledge_range WITH &&
            )
            """
        )


def _sync_model_comments(models: Iterable[type[DeclarativeBase]]) -> None:
    """在 schema expand 时同步本次新增模型的中文数据字典说明。"""
    connection = op.get_bind()
    for model in models:
        table = model.__table__
        _set_table_comment(connection, table.name, table.comment)
        for column in table.columns:
            _set_column_comment(connection, table.name, column.name, column.comment)


def _set_table_comment(connection: Connection, table_name: str, comment: str | None) -> None:
    """为本次 migration 创建的逻辑表写入固定中文说明。"""
    quoted_table_name = connection.dialect.identifier_preparer.quote(table_name)
    connection.execute(
        text(f"COMMENT ON TABLE {quoted_table_name} IS {_comment_literal(connection, comment)}")
    )


def _set_column_comment(
    connection: Connection,
    table_name: str,
    column_name: str,
    comment: str | None,
) -> None:
    """为本次 migration 创建的列写入固定中文说明。"""
    identifier_preparer = connection.dialect.identifier_preparer
    quoted_table_name = identifier_preparer.quote(table_name)
    quoted_column_name = identifier_preparer.quote(column_name)
    connection.execute(
        text(
            f"COMMENT ON COLUMN {quoted_table_name}.{quoted_column_name} "
            f"IS {_comment_literal(connection, comment)}"
        )
    )


def _comment_literal(connection: Connection, comment: str | None) -> str:
    """将受控中文说明渲染为 PostgreSQL 字面量，避免注释内容影响 DDL。"""
    if comment is None:
        return "NULL"
    if "\x00" in comment:
        raise ValueError("数据库说明不能包含空字符")
    return "'" + comment.replace("'", "''") + "'"
