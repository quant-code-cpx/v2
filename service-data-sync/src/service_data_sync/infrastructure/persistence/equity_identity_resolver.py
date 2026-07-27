"""基于证券标识双时间历史的日期感知身份解析器。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Connection, Engine, text

from service_data_sync.application.ports.equity_master import EquityIdentityResolver
from service_data_sync.domain.equity import Exchange
from service_data_sync.domain.equity_master import (
    EquityIdentityResolution,
    EquityIdentityResolutionStatus,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient


class SqlAlchemyEquityIdentityResolver(EquityIdentityResolver):
    """在服务自有 PostgreSQL 上执行只读的双时间证券标识查询。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存 SQLAlchemy 引擎，不向应用层暴露连接管理细节。"""
        self._engine: Engine = database.engine

    def resolve(
        self,
        *,
        exchange: Exchange,
        symbol: str,
        fact_date: date,
        known_at: datetime,
    ) -> EquityIdentityResolution:
        """按市场有效日和系统知识时刻解析，不回退至身份锚当前列。"""
        with self._engine.connect() as connection:
            return resolve_identity_on_connection(
                connection,
                exchange=exchange,
                symbol=symbol,
                fact_date=fact_date,
                known_at=known_at,
            )

    def resolve_current_open(self, *, exchange: Exchange, symbol: str) -> EquityIdentityResolution:
        """解析唯一当前开放确认标识，PENDING 占位不允许作为公开读取结果。"""
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        """
                        SELECT security_id, identity_state
                        FROM equity_identifier_version
                        WHERE exchange = :exchange
                          AND symbol = :symbol
                          AND effective_to IS NULL
                          AND known_to IS NULL
                          AND identity_state = 'CONFIRMED'
                        ORDER BY security_id
                        """
                    ),
                    {"exchange": exchange.value, "symbol": symbol},
                )
                .mappings()
                .all()
            )
        return _resolution(rows)


def resolve_identity_on_connection(
    connection: Connection,
    *,
    exchange: Exchange,
    symbol: str,
    fact_date: date,
    known_at: datetime,
) -> EquityIdentityResolution:
    """在调用方事务中解析标识，使事实写入和身份选择共享同一知识视图。"""
    if known_at.tzinfo is None:
        raise ValueError("known_at must include a timezone")
    rows = (
        connection.execute(
            text(
                """
                SELECT security_id, identity_state
                FROM equity_identifier_version
                WHERE exchange = :exchange
                  AND symbol = :symbol
                  AND effective_range @> :fact_date
                  AND knowledge_range @> :known_at
                ORDER BY security_id
                """
            ),
            {
                "exchange": exchange.value,
                "symbol": symbol,
                "fact_date": fact_date,
                "known_at": known_at.astimezone(UTC),
            },
        )
        .mappings()
        .all()
    )
    return _resolution(rows)


def _resolution(rows: Sequence[Mapping[Any, Any]]) -> EquityIdentityResolution:
    """把零、一、多条标识命中投影为不可被调用方任意挑选的枚举结果。"""
    if not rows:
        return EquityIdentityResolution(status=EquityIdentityResolutionStatus.NOT_FOUND)
    if len(rows) > 1:
        return EquityIdentityResolution(status=EquityIdentityResolutionStatus.CONFLICT)
    row = rows[0]
    return EquityIdentityResolution(
        status=EquityIdentityResolutionStatus.RESOLVED,
        security_id=int(row["security_id"]),
        identity_state=str(row["identity_state"]),
    )
