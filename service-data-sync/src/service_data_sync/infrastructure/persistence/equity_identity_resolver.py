"""基于证券标识双时间历史的日期感知身份解析器。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from service_data_sync.application.ports.equity_master import EquityIdentityResolver
from service_data_sync.domain.equity import Exchange
from service_data_sync.domain.equity_master import (
    EquityIdentityResolution,
    EquityIdentityResolutionStatus,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient

from ..database.models.equity.identity.equity_identifier_version import (
    EquityIdentifierVersion,
)


class SqlAlchemyEquityIdentityResolver(EquityIdentityResolver):
    """在服务自有 PostgreSQL 上执行只读的双时间证券标识查询。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存 Session 工厂，不向应用层暴露连接或事务管理细节。"""
        self._database = database

    def resolve(
        self,
        *,
        exchange: Exchange,
        symbol: str,
        fact_date: date,
        known_at: datetime,
    ) -> EquityIdentityResolution:
        """按市场有效日和系统知识时刻解析，不回退至身份锚当前列。"""
        with self._database.session() as session:
            return resolve_identity_on_connection(
                session,
                exchange=exchange,
                symbol=symbol,
                fact_date=fact_date,
                known_at=known_at,
            )

    def resolve_current_open(self, *, exchange: Exchange, symbol: str) -> EquityIdentityResolution:
        """解析唯一当前开放确认标识，PENDING 占位不允许作为公开读取结果。"""
        statement = (
            select(EquityIdentifierVersion.security_id, EquityIdentifierVersion.identity_state)
            .where(
                EquityIdentifierVersion.exchange == exchange.value,
                EquityIdentifierVersion.symbol == symbol,
                EquityIdentifierVersion.effective_to.is_(None),
                EquityIdentifierVersion.known_to.is_(None),
                EquityIdentifierVersion.identity_state == "CONFIRMED",
            )
            .order_by(EquityIdentifierVersion.security_id)
        )
        with self._database.session() as session:
            rows = session.execute(statement).mappings().all()
        return _resolution(rows)


def resolve_identity_on_connection(
    connection: Session,
    *,
    exchange: Exchange,
    symbol: str,
    fact_date: date,
    known_at: datetime,
) -> EquityIdentityResolution:
    """在调用方事务中解析标识，使事实写入和身份选择共享同一知识视图。"""
    if known_at.tzinfo is None:
        raise ValueError("known_at must include a timezone")
    statement: Select[tuple[int, str]] = (
        select(EquityIdentifierVersion.security_id, EquityIdentifierVersion.identity_state)
        .where(
            EquityIdentifierVersion.exchange == exchange.value,
            EquityIdentifierVersion.symbol == symbol,
            EquityIdentifierVersion.effective_range.op("@>")(fact_date),
            EquityIdentifierVersion.knowledge_range.op("@>")(known_at.astimezone(UTC)),
        )
        .order_by(EquityIdentifierVersion.security_id)
    )
    rows = connection.execute(statement).mappings().all()
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
