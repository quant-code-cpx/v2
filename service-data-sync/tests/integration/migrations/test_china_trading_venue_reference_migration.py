"""中国证券交易场所固定参考迁移的真实 PostgreSQL 回归。"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, RowMapping, make_url
from sqlalchemy.schema import CreateSchema, DropSchema

pytestmark = pytest.mark.integration

_PREVIOUS_REVISION = "202607300019"
_CURRENT_REVISION = "202607300020"
_SEED_REVISION = _CURRENT_REVISION
_VENUES = {
    "BSE": {
        "venue_id": "a3ee0ce9-e2cc-5dda-a7a6-b0be5c26b7e5",
        "mic": "BJSE",
        "name": "北京证券交易所",
    },
    "SSE": {
        "venue_id": "c0f6fbee-2993-53e5-8d2f-ffae70828a44",
        "mic": "XSHG",
        "name": "上海证券交易所",
    },
    "SZSE": {
        "venue_id": "f9c5630f-2efb-59dc-9602-bf9f650bd702",
        "mic": "XSHE",
        "name": "深圳证券交易所",
    },
}


def test_china_venue_upgrade_reuses_exact_row_and_downgrade_removes_only_seeded_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实迁移复用既有 SSE，回滚只删除本修订新增的 SZSE/BSE，并可再次升级。"""
    database_url = _integration_database_url()
    if database_url is None:
        pytest.skip("requires DATA_SYNC_RUN_INTEGRATION=1 and DATA_SYNC_DATABASE_URL")
    schema = f"china_venue_reference_{uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    migration_url = _schema_url(database_url, schema=schema)
    migration_engine = create_engine(migration_url, pool_pre_ping=True)
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        config = _migration_config(monkeypatch, migration_url=migration_url)
        command.upgrade(config, _PREVIOUS_REVISION)
        existing_sse_id = uuid4()
        _insert_venue(
            migration_engine,
            venue_id=existing_sse_id,
            code="SSE",
            mic="XSHG",
            name="上海证券交易所",
        )

        command.upgrade(config, _CURRENT_REVISION)
        upgraded_rows = _venue_rows(migration_engine)
        assert set(upgraded_rows) == set(_VENUES)
        assert upgraded_rows["SSE"]["venue_id"] == str(existing_sse_id)
        assert upgraded_rows["SSE"]["reference_seed_revision"] is None
        for code in {"BSE", "SZSE"}:
            assert upgraded_rows[code]["venue_id"] == _VENUES[code]["venue_id"]
            assert upgraded_rows[code]["reference_seed_revision"] == _SEED_REVISION
            assert upgraded_rows[code]["timezone"] == "Asia/Shanghai"
            assert upgraded_rows[code]["country"] == "CN"
            assert upgraded_rows[code]["active"] is True

        command.downgrade(config, _PREVIOUS_REVISION)
        assert _venue_rows(migration_engine, include_seed_revision=False) == {
            "SSE": {
                "venue_id": str(existing_sse_id),
                "mic": "XSHG",
                "name": "上海证券交易所",
                "timezone": "Asia/Shanghai",
                "country": "CN",
                "active": True,
            }
        }
        with migration_engine.connect() as connection:
            columns = {
                str(row["column_name"])
                for row in connection.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = 'trading_venue'
                        """
                    )
                ).mappings()
            }
        assert "reference_seed_revision" not in columns

        command.upgrade(config, _CURRENT_REVISION)
        reupgraded_rows = _venue_rows(migration_engine)
        assert set(reupgraded_rows) == set(_VENUES)
        assert reupgraded_rows["SSE"]["venue_id"] == str(existing_sse_id)
        assert reupgraded_rows["SSE"]["reference_seed_revision"] is None
    finally:
        migration_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        admin_engine.dispose()


def test_china_venue_upgrade_rejects_conflicting_existing_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已有同 code 但错误属性时必须失败，不能静默覆盖治理场所。"""
    database_url = _integration_database_url()
    if database_url is None:
        pytest.skip("requires DATA_SYNC_RUN_INTEGRATION=1 and DATA_SYNC_DATABASE_URL")
    schema = f"china_venue_conflict_{uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    migration_url = _schema_url(database_url, schema=schema)
    migration_engine = create_engine(migration_url, pool_pre_ping=True)
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        config = _migration_config(monkeypatch, migration_url=migration_url)
        command.upgrade(config, _PREVIOUS_REVISION)
        _insert_venue(
            migration_engine,
            venue_id=uuid4(),
            code="SSE",
            mic="XSHG",
            name="错误的交易场所名称",
        )

        with pytest.raises(RuntimeError, match="trading venue reference conflict"):
            command.upgrade(config, _CURRENT_REVISION)
    finally:
        migration_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        admin_engine.dispose()


def test_china_venue_downgrade_rejects_referenced_seed_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """固定场所已有外键事实时回滚必须说明依赖并保持数据不变。"""
    database_url = _integration_database_url()
    if database_url is None:
        pytest.skip("requires DATA_SYNC_RUN_INTEGRATION=1 and DATA_SYNC_DATABASE_URL")
    schema = f"china_venue_rollback_{uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    migration_url = _schema_url(database_url, schema=schema)
    migration_engine = create_engine(migration_url, pool_pre_ping=True)
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        config = _migration_config(monkeypatch, migration_url=migration_url)
        command.upgrade(config, _CURRENT_REVISION)
        with migration_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE venue_reference_rollback_probe (
                      venue_id uuid NOT NULL
                        REFERENCES trading_venue(venue_id) ON DELETE RESTRICT
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO venue_reference_rollback_probe (venue_id)
                    VALUES (CAST(:venue_id AS uuid))
                    """
                ),
                {"venue_id": _VENUES["BSE"]["venue_id"]},
            )

        with pytest.raises(RuntimeError, match="rollback China trading venue reference"):
            command.downgrade(config, _PREVIOUS_REVISION)
        assert set(_venue_rows(migration_engine)) == set(_VENUES)
    finally:
        migration_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        admin_engine.dispose()


def _integration_database_url() -> str | None:
    """只在显式集成环境读取连接，默认单元测试不访问 PostgreSQL。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        return None
    return os.environ.get("DATA_SYNC_DATABASE_URL")


def _migration_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    migration_url: str,
) -> Config:
    """让 Alembic 与 settings 使用同一隔离 schema，避免触及共享数据库状态。"""
    monkeypatch.setenv("DATA_SYNC_DATABASE_URL", migration_url)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migration_url.replace("%", "%%"))
    return config


def _insert_venue(
    engine: Engine,
    *,
    venue_id: UUID,
    code: str,
    mic: str,
    name: str,
) -> None:
    """在目标迁移前建立一条明确既有场所，用于验证严格复用而不是静默更新。"""
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO trading_venue (
                  venue_id,
                  code,
                  mic,
                  name,
                  timezone,
                  country,
                  active
                ) VALUES (
                  :venue_id,
                  :code,
                  :mic,
                  :name,
                  'Asia/Shanghai',
                  'CN',
                  TRUE
                )
                """
            ),
            {
                "venue_id": venue_id,
                "code": code,
                "mic": mic,
                "name": name,
            },
        )


def _venue_rows(
    engine: Engine,
    *,
    include_seed_revision: bool = True,
) -> dict[str, dict[str, object]]:
    """读取完整参考属性，断言升级、回退和再升级均没有产生重复身份。"""
    seed_select = (
        "reference_seed_revision"
        if include_seed_revision
        else "NULL::text AS reference_seed_revision"
    )
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    f"""
                SELECT venue_id, code, mic, name, timezone, country, active,
                       {seed_select}
                FROM trading_venue
                ORDER BY code
                """
                )
            )
            .mappings()
            .all()
        )
    return {
        str(row["code"]): _venue_row(row, include_seed_revision=include_seed_revision)
        for row in rows
    }


def _venue_row(
    row: RowMapping,
    *,
    include_seed_revision: bool,
) -> dict[str, object]:
    """将数据库行归一为稳定断言字典，避免驱动 UUID 表示影响迁移回归。"""
    result: dict[str, object] = {
        "venue_id": str(row["venue_id"]),
        "mic": row["mic"],
        "name": row["name"],
        "timezone": row["timezone"],
        "country": row["country"],
        "active": row["active"],
    }
    if include_seed_revision:
        result["reference_seed_revision"] = row["reference_seed_revision"]
    return result


def _schema_url(database_url: str, *, schema: str) -> str:
    """把 Alembic 与 SQLAlchemy 都限制在独占 schema，避免污染共享集成数据库。"""
    url = make_url(database_url)
    query = dict(url.query)
    query["options"] = f"-csearch_path={schema}"
    return url.set(query=query).render_as_string(hide_password=False)
