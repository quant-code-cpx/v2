"""增加市场概览不可变组件、完整包和原子当前指针。

Revision ID: 202607300001
Revises: 202607290015
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op

revision = "202607300001"
down_revision = "202607290015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建市场概览发布表；指针仅在完整包事务成功后切换。"""
    op.execute(
        """
        CREATE TABLE market_overview_component_release (
          component_release_id UUID PRIMARY KEY,
          dataset_code VARCHAR(120) NOT NULL,
          partition_key VARCHAR(240) NOT NULL,
          trade_date DATE NULL,
          data_version UUID NOT NULL UNIQUE,
          content_hash CHAR(64) NOT NULL,
          payload_json JSONB NOT NULL,
          source_json JSONB NOT NULL,
          methodology_json JSONB NOT NULL,
          quality_json JSONB NOT NULL,
          quality_status VARCHAR(16) NOT NULL,
          finality VARCHAR(16) NOT NULL,
          observed_at TIMESTAMPTZ NOT NULL,
          published_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT uq_market_overview_component_content
            UNIQUE(dataset_code, partition_key, content_hash),
          CONSTRAINT ck_market_overview_component_hash
            CHECK(content_hash ~ '^[0-9a-f]{64}$'),
          CONSTRAINT ck_market_overview_component_quality
            CHECK(quality_status = 'passed'),
          CONSTRAINT ck_market_overview_component_finality
            CHECK(finality = 'final')
        );
        CREATE INDEX ix_market_overview_component_dataset_date
          ON market_overview_component_release(dataset_code, trade_date, published_at);

        CREATE TABLE market_overview_bundle (
          bundle_id UUID PRIMARY KEY,
          trade_date DATE NOT NULL,
          data_version UUID NOT NULL UNIQUE,
          content_hash CHAR(64) NOT NULL,
          payload_json JSONB NOT NULL,
          manifest_json JSONB NOT NULL,
          quality_status VARCHAR(16) NOT NULL,
          finality VARCHAR(16) NOT NULL,
          published_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT uq_market_overview_bundle_content UNIQUE(trade_date, content_hash),
          CONSTRAINT ck_market_overview_bundle_hash
            CHECK(content_hash ~ '^[0-9a-f]{64}$'),
          CONSTRAINT ck_market_overview_bundle_quality
            CHECK(quality_status = 'passed'),
          CONSTRAINT ck_market_overview_bundle_finality
            CHECK(finality = 'final')
        );
        CREATE INDEX ix_market_overview_bundle_trade_date
          ON market_overview_bundle(trade_date, published_at);

        CREATE TABLE market_overview_bundle_component (
          bundle_id UUID NOT NULL REFERENCES market_overview_bundle(bundle_id)
            ON DELETE RESTRICT,
          dataset_code VARCHAR(120) NOT NULL,
          component_release_id UUID NOT NULL
            REFERENCES market_overview_component_release(component_release_id)
            ON DELETE RESTRICT,
          verified_at TIMESTAMPTZ NOT NULL,
          verification_json JSONB NOT NULL,
          PRIMARY KEY(bundle_id, dataset_code)
        );

        CREATE TABLE market_overview_current_pointer (
          market VARCHAR(32) PRIMARY KEY,
          bundle_id UUID NOT NULL REFERENCES market_overview_bundle(bundle_id)
            ON DELETE RESTRICT,
          updated_at TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE market_overview_active_bundle (
          market VARCHAR(32) NOT NULL,
          trade_date DATE NOT NULL,
          bundle_id UUID NOT NULL REFERENCES market_overview_bundle(bundle_id)
            ON DELETE RESTRICT,
          updated_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY(market, trade_date),
          CONSTRAINT uq_market_overview_active_bundle UNIQUE(bundle_id)
        );

        CREATE TABLE market_overview_pointer_transition (
          transition_id UUID PRIMARY KEY,
          market VARCHAR(32) NOT NULL,
          trade_date DATE NOT NULL,
          from_bundle_id UUID NULL REFERENCES market_overview_bundle(bundle_id)
            ON DELETE RESTRICT,
          to_bundle_id UUID NOT NULL REFERENCES market_overview_bundle(bundle_id)
            ON DELETE RESTRICT,
          action VARCHAR(16) NOT NULL,
          reason VARCHAR(500) NOT NULL,
          actor_ref VARCHAR(160) NOT NULL,
          changed_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT ck_market_overview_pointer_transition_action
            CHECK(action IN ('publish', 'rollback', 'forward'))
        );
        CREATE INDEX ix_market_overview_pointer_transition_market_date
          ON market_overview_pointer_transition(market, trade_date, changed_at);

        CREATE TABLE market_overview_derivation_input_pointer (
          dataset_code VARCHAR(120) NOT NULL,
          trade_date DATE NOT NULL,
          component_release_id UUID NOT NULL
            REFERENCES market_overview_component_release(component_release_id)
            ON DELETE RESTRICT,
          updated_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY(dataset_code, trade_date),
          CONSTRAINT ck_market_overview_derivation_input_dataset
            CHECK(dataset_code IN ('sector.quote.eod.dc', 'sw.market-data'))
        );

        COMMENT ON TABLE market_overview_component_release IS
          '市场概览写时组合前的不可变组件发布；载荷已通过 provider-neutral schema。';
        COMMENT ON TABLE market_overview_bundle IS
          '市场首页原子可见完整包；任一必需组件失败时不会创建。';
        COMMENT ON TABLE market_overview_bundle_component IS
          '完整包到不可变组件发布的固定 manifest。';
        COMMENT ON TABLE market_overview_current_pointer IS
          '市场首页 latest 完整包原子指针；组件失败时保持旧值。';
        COMMENT ON TABLE market_overview_active_bundle IS
          '按交易日解析 exact/history reader 的公开可见 bundle。';
        COMMENT ON TABLE market_overview_pointer_transition IS
          '市场 bundle 公开可见指针的不可变发布、回滚与前滚审计。';
        COMMENT ON TABLE market_overview_derivation_input_pointer IS
          '近期 bootstrap 日线输入指针；公开 reader 不直接读取。';
        """
    )


def downgrade() -> None:
    """仅在没有任何市场发布时允许回退，避免删除消费者已见版本。"""
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM market_overview_bundle)
             OR EXISTS (SELECT 1 FROM market_overview_component_release) THEN
            RAISE EXCEPTION 'market overview publication history prevents rollback';
          END IF;
        END $$;
        DROP TABLE market_overview_derivation_input_pointer;
        DROP TABLE market_overview_pointer_transition;
        DROP TABLE market_overview_active_bundle;
        DROP TABLE market_overview_current_pointer;
        DROP TABLE market_overview_bundle_component;
        DROP TABLE market_overview_bundle;
        DROP TABLE market_overview_component_release;
        """
    )
