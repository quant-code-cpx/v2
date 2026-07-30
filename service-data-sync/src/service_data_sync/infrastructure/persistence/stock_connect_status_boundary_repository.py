"""持久化互联互通状态 coverage 的不可后移日期边界。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.delivery_manifest import (
    StockConnectStatusCoverageBoundaryLock,
)

_PRODUCTION_SCOPE = "market.stock_connect.channel_status.eod"
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class StockConnectStatusBoundaryViolation(ValueError):
    """表示候选状态边界试图移到未来或越过既有持久化锁。"""

    def __init__(self, reason: str) -> None:
        """保存可安全进入 preflight 结果的稳定拒绝原因码。"""
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class StockConnectStatusBoundarySnapshot:
    """返回一次锁定后真正生效的状态 coverage 边界。"""

    scope_key: str
    required_from: date
    first_locked_at: datetime
    tightened_at: datetime


class StockConnectStatusBoundaryRepository(Protocol):
    """抽象控制面所需的状态边界锁操作，便于无数据库单元测试替换。"""

    def claim(
        self,
        *,
        required_from: date,
        manifest_sha256: str,
        observed_at: datetime,
    ) -> StockConnectStatusBoundarySnapshot:
        """锁定或收紧状态 coverage 边界。"""
        ...


class SqlAlchemyStockConnectStatusBoundaryRepository:
    """以 PostgreSQL 唯一行和触发器原子锁定状态覆盖边界。"""

    def __init__(
        self,
        database: DatabaseClient,
        *,
        scope_key: str = _PRODUCTION_SCOPE,
    ) -> None:
        """保存数据库客户端与稳定作用域，测试可使用独立作用域避免互相污染。"""
        if not scope_key.strip() or len(scope_key) > 160:
            raise ValueError("status coverage boundary scope is invalid")
        self._database = database
        self._scope_key = scope_key

    def claim(
        self,
        *,
        required_from: date,
        manifest_sha256: str,
        observed_at: datetime,
    ) -> StockConnectStatusBoundarySnapshot:
        """首次插入边界，或仅在候选更早时原子收紧既有边界。

        相同边界是幂等读取；更晚边界会回滚并拒绝。数据库触发器独立复核未来日期、更新方向和
        删除操作，因此修改环境变量、绕过仓储执行 SQL 或并发启动都不能扩大历史缺源豁免区间。
        """
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("status coverage boundary observation must include timezone")
        if required_from > observed_at.astimezone(_SHANGHAI).date():
            raise StockConnectStatusBoundaryViolation("STATUS_BOUNDARY_IN_FUTURE")
        if (
            len(manifest_sha256) != 64
            or manifest_sha256 != manifest_sha256.lower()
            or any(char not in "0123456789abcdef" for char in manifest_sha256)
        ):
            raise ValueError("status coverage manifest digest is invalid")

        with self._database.transaction() as session:
            session.execute(
                insert(StockConnectStatusCoverageBoundaryLock)
                .values(
                    scope_key=self._scope_key,
                    required_from=required_from,
                    first_manifest_sha256=manifest_sha256,
                    current_manifest_sha256=manifest_sha256,
                    first_locked_at=observed_at,
                    tightened_at=observed_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[StockConnectStatusCoverageBoundaryLock.scope_key]
                )
            )
            row = session.scalar(
                select(StockConnectStatusCoverageBoundaryLock)
                .where(StockConnectStatusCoverageBoundaryLock.scope_key == self._scope_key)
                .with_for_update()
            )
            if row is None:
                raise RuntimeError("status coverage boundary lock is unavailable after insert")
            if required_from > row.required_from:
                raise StockConnectStatusBoundaryViolation("STATUS_BOUNDARY_MOVED_LATER")
            if required_from < row.required_from:
                session.execute(
                    update(StockConnectStatusCoverageBoundaryLock)
                    .where(StockConnectStatusCoverageBoundaryLock.scope_key == self._scope_key)
                    .values(
                        required_from=required_from,
                        current_manifest_sha256=manifest_sha256,
                        tightened_at=observed_at,
                    )
                )
                row.required_from = required_from
                row.current_manifest_sha256 = manifest_sha256
                row.tightened_at = observed_at
            return StockConnectStatusBoundarySnapshot(
                scope_key=row.scope_key,
                required_from=row.required_from,
                first_locked_at=row.first_locked_at,
                tightened_at=row.tightened_at,
            )
