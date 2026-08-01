"""初始化中国证券交易场所固定参考身份。

两融、龙虎榜和大宗交易等事实只接受已治理的交易场所 UUID。本迁移把沪深北三家
证券交易所作为独立参考数据初始化，不能再依赖 ETF 目录同步碰巧先写入场所。
若库中已有相同 `code` 或 `MIC`，必须与完整参考属性一致才复用；冲突不会被静默覆盖。

`reference_seed_revision` 只标记本迁移实际插入的行。回滚会先检查所有指向
`trading_venue` 的外键事实：有依赖时明确拒绝，避免删除仍被 canonical 数据引用的身份；
复用的既有行永远不带本标记，因此不会被回滚删除。

Revision ID: 202607300020
Revises: 202607300019
Create Date: 2026-08-01
"""

from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection, RowMapping

# Alembic 使用的版本标识。
revision = "202607300020"
down_revision = "202607300019"
branch_labels = None
depends_on = None

_SEED_COLUMN = "reference_seed_revision"


@dataclass(frozen=True, slots=True)
class _TradingVenueReference:
    """定义一个可审计的交易场所固定参考身份及其稳定 UUID。"""

    venue_id: UUID
    code: str
    mic: str
    name: str
    timezone: str
    country: str
    active: bool


_VENUES = (
    _TradingVenueReference(
        venue_id=UUID("c0f6fbee-2993-53e5-8d2f-ffae70828a44"),
        code="SSE",
        mic="XSHG",
        name="上海证券交易所",
        timezone="Asia/Shanghai",
        country="CN",
        active=True,
    ),
    _TradingVenueReference(
        venue_id=UUID("f9c5630f-2efb-59dc-9602-bf9f650bd702"),
        code="SZSE",
        mic="XSHE",
        name="深圳证券交易所",
        timezone="Asia/Shanghai",
        country="CN",
        active=True,
    ),
    _TradingVenueReference(
        venue_id=UUID("a3ee0ce9-e2cc-5dda-a7a6-b0be5c26b7e5"),
        code="BSE",
        mic="BJSE",
        name="北京证券交易所",
        timezone="Asia/Shanghai",
        country="CN",
        active=True,
    ),
)
_VENUE_BY_CODE = {venue.code: venue for venue in _VENUES}


def upgrade() -> None:
    """添加迁移来源标记并严格初始化沪深北三家交易场所。"""
    connection = op.get_bind()
    if not _has_seed_column(connection):
        op.add_column(
            "trading_venue",
            sa.Column(
                _SEED_COLUMN,
                sa.String(length=32),
                nullable=True,
                comment="创建该固定参考场所的 Alembic 修订号；非迁移创建场所保持为空。",
            ),
        )
    _lock_venue_table(connection)
    for venue in _VENUES:
        _ensure_venue(connection, venue=venue)


def downgrade() -> None:
    """仅删除本迁移创建且没有下游事实引用的参考场所，再移除标记列。"""
    connection = op.get_bind()
    if not _has_seed_column(connection):
        return
    _lock_venue_table(connection)
    seeded_rows = _seeded_rows(connection)
    _validate_seeded_rows(seeded_rows)
    _reject_unexpected_seed_markers(connection)
    seeded_ids = tuple(UUID(str(row["venue_id"])) for row in seeded_rows)
    dependencies = _dependent_foreign_keys(connection, venue_ids=seeded_ids)
    if dependencies:
        raise RuntimeError(
            "cannot rollback China trading venue reference while dependent facts exist; "
            "rollback dependent data first: " + ", ".join(dependencies)
        )
    if seeded_ids:
        connection.execute(
            sa.text("DELETE FROM trading_venue WHERE reference_seed_revision = :seed_revision"),
            {"seed_revision": revision},
        )
    op.drop_column("trading_venue", _SEED_COLUMN)


def _has_seed_column(connection: Connection) -> bool:
    """读取当前表结构，使异常中断后的前滚重试不会重复新增标记列。"""
    columns = sa.inspect(connection).get_columns("trading_venue")
    return any(str(column["name"]) == _SEED_COLUMN for column in columns)


def _lock_venue_table(connection: Connection) -> None:
    """串行化参考身份校验和回滚，避免外键事实在检查与删除之间插入。"""
    connection.execute(sa.text("LOCK TABLE trading_venue IN EXCLUSIVE MODE"))


def _ensure_venue(connection: Connection, *, venue: _TradingVenueReference) -> None:
    """复用严格一致的既有场所，或插入带本迁移来源标记的固定身份。"""
    by_code = _venue_row(connection, field="code", value=venue.code)
    by_mic = _venue_row(connection, field="mic", value=venue.mic)
    if by_code is not None and by_mic is not None:
        if UUID(str(by_code["venue_id"])) != UUID(str(by_mic["venue_id"])):
            raise _conflict(
                venue,
                "code and MIC resolve to different existing trading venues",
            )
    existing = by_code if by_code is not None else by_mic
    if existing is not None:
        _validate_reference_row(existing, venue=venue)
        if (
            existing["reference_seed_revision"] == revision
            and UUID(str(existing["venue_id"])) != venue.venue_id
        ):
            raise _conflict(venue, "seed marker does not match its fixed reference UUID")
        return

    fixed_id_row = (
        connection.execute(
            sa.text(
                "SELECT venue_id, mic, code, name, timezone, country, active, "
                "reference_seed_revision "
                "FROM trading_venue WHERE venue_id = CAST(:venue_id AS uuid)"
            ),
            {"venue_id": str(venue.venue_id)},
        )
        .mappings()
        .one_or_none()
    )
    if fixed_id_row is not None:
        raise _conflict(venue, "fixed reference UUID already belongs to another trading venue")

    connection.execute(
        sa.text(
            """
            INSERT INTO trading_venue (
              venue_id,
              mic,
              code,
              name,
              timezone,
              country,
              active,
              reference_seed_revision
            ) VALUES (
              CAST(:venue_id AS uuid),
              :mic,
              :code,
              :name,
              :timezone,
              :country,
              :active,
              :seed_revision
            )
            """
        ),
        {
            "venue_id": str(venue.venue_id),
            "mic": venue.mic,
            "code": venue.code,
            "name": venue.name,
            "timezone": venue.timezone,
            "country": venue.country,
            "active": venue.active,
            "seed_revision": revision,
        },
    )


def _venue_row(
    connection: Connection,
    *,
    field: str,
    value: str,
) -> RowMapping | None:
    """按受控唯一键读取场所完整属性；调用方不能传入动态 SQL 标识符。"""
    if field not in {"code", "mic"}:
        raise ValueError("trading venue lookup field is invalid")
    return (
        connection.execute(
            sa.text(
                "SELECT venue_id, mic, code, name, timezone, country, active, "
                "reference_seed_revision "
                f"FROM trading_venue WHERE {field} = :value"
            ),
            {"value": value},
        )
        .mappings()
        .one_or_none()
    )


def _validate_reference_row(
    row: RowMapping,
    *,
    venue: _TradingVenueReference,
) -> None:
    """拒绝同 code 或 MIC 却有不同属性的场所，避免把错误治理数据继续用于事实。"""
    expected = {
        "code": venue.code,
        "mic": venue.mic,
        "name": venue.name,
        "timezone": venue.timezone,
        "country": venue.country,
        "active": venue.active,
    }
    conflicts = [
        field for field, expected_value in expected.items() if row[field] != expected_value
    ]
    if conflicts:
        raise _conflict(venue, "mismatched fields: " + ", ".join(conflicts))


def _conflict(venue: _TradingVenueReference, detail: str) -> RuntimeError:
    """构造稳定冲突文本，供迁移日志与受控人工修复共同定位。"""
    return RuntimeError(
        "trading venue reference conflict for "
        f"{venue.code}/{venue.mic}: {detail}; correct existing reference data before retry"
    )


def _seeded_rows(connection: Connection) -> tuple[RowMapping, ...]:
    """读取仅由本修订插入的场所，复用的既有行不会出现在回滚删除集合中。"""
    rows = (
        connection.execute(
            sa.text(
                """
            SELECT venue_id, mic, code, name, timezone, country, active, reference_seed_revision
            FROM trading_venue
            WHERE reference_seed_revision = :seed_revision
            ORDER BY code
            """
            ),
            {"seed_revision": revision},
        )
        .mappings()
        .all()
    )
    return tuple(rows)


def _validate_seeded_rows(rows: tuple[RowMapping, ...]) -> None:
    """确认来源标记没有被转移到非本迁移行，也未让固定参考属性发生漂移。"""
    if len(rows) > len(_VENUES):
        raise RuntimeError("unexpected China trading venue seed markers prevent rollback")
    seen_codes: set[str] = set()
    for row in rows:
        code = str(row["code"])
        venue = _VENUE_BY_CODE.get(code)
        if venue is None or code in seen_codes:
            raise RuntimeError("unexpected China trading venue seed marker prevents rollback")
        seen_codes.add(code)
        if UUID(str(row["venue_id"])) != venue.venue_id:
            raise RuntimeError(
                "China trading venue seed UUID changed; manual review is required before rollback"
            )
        _validate_reference_row(row, venue=venue)


def _reject_unexpected_seed_markers(connection: Connection) -> None:
    """阻止删除列时丢失其他修订留下的参考来源审计信息。"""
    unexpected_count = connection.scalar(
        sa.text(
            """
            SELECT count(*)
            FROM trading_venue
            WHERE reference_seed_revision IS NOT NULL
              AND reference_seed_revision <> :seed_revision
            """
        ),
        {"seed_revision": revision},
    )
    if int(unexpected_count or 0) != 0:
        raise RuntimeError(
            "other trading venue seed revisions exist; rollback would discard their provenance"
        )


def _dependent_foreign_keys(
    connection: Connection,
    *,
    venue_ids: tuple[UUID, ...],
) -> tuple[str, ...]:
    """枚举并计数全部引用场所的外键事实，确保回滚不会留下悬空身份。"""
    if not venue_ids:
        return ()
    foreign_keys = (
        connection.execute(
            sa.text(
                """
            SELECT
              child_namespace.nspname AS schema_name,
              child_table.relname AS table_name,
              constraint_.conname AS constraint_name,
              array_agg(child_attribute.attname ORDER BY child_key.ordinality) AS column_names
            FROM pg_constraint AS constraint_
            JOIN pg_class AS child_table
              ON child_table.oid = constraint_.conrelid
            JOIN pg_namespace AS child_namespace
              ON child_namespace.oid = child_table.relnamespace
            JOIN LATERAL unnest(constraint_.conkey) WITH ORDINALITY
              AS child_key(attnum, ordinality) ON TRUE
            JOIN pg_attribute AS child_attribute
              ON child_attribute.attrelid = child_table.oid
             AND child_attribute.attnum = child_key.attnum
            WHERE constraint_.contype = 'f'
              AND constraint_.confrelid = 'trading_venue'::regclass
            GROUP BY
              child_namespace.nspname,
              child_table.relname,
              constraint_.conname
            ORDER BY child_namespace.nspname, child_table.relname, constraint_.conname
            """
            )
        )
        .mappings()
        .all()
    )
    parameters = {f"venue_id_{index}": str(value) for index, value in enumerate(venue_ids)}
    placeholders = ", ".join(f"CAST(:venue_id_{index} AS uuid)" for index in range(len(venue_ids)))
    preparer = connection.dialect.identifier_preparer
    dependencies: list[str] = []
    for foreign_key in foreign_keys:
        column_names = tuple(str(value) for value in foreign_key["column_names"])
        if len(column_names) != 1:
            raise RuntimeError(
                "cannot safely rollback China trading venue reference "
                "with a composite venue foreign key"
            )
        schema_name = str(foreign_key["schema_name"])
        table_name = str(foreign_key["table_name"])
        column_name = column_names[0]
        qualified_table = f"{preparer.quote_schema(schema_name)}.{preparer.quote(table_name)}"
        statement = sa.text(
            f"SELECT count(*) FROM {qualified_table} "
            f"WHERE {preparer.quote(column_name)} IN ({placeholders})"
        )
        count = connection.scalar(statement, parameters)
        if int(count or 0) != 0:
            dependencies.append(
                f"{schema_name}.{table_name}.{foreign_key['constraint_name']}={int(count)}"
            )
    return tuple(dependencies)
