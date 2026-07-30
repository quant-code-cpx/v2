"""验证状态 coverage 边界在真实 PostgreSQL 中首次锁定且只能前移。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.delivery_manifest import (
    StockConnectStatusCoverageBoundaryLock,
)
from service_data_sync.infrastructure.persistence.stock_connect_status_boundary_repository import (
    SqlAlchemyStockConnectStatusBoundaryRepository,
    StockConnectStatusBoundaryViolation,
)


@pytest.mark.integration
def test_status_boundary_first_lock_is_idempotent_and_only_moves_earlier() -> None:
    """首次值持久化后，相同值幂等、后移拒绝、前移成功且 SQL 绕过仍被触发器拒绝。"""
    database = DatabaseClient.from_settings(load_settings())
    scope_key = f"test.stock-connect.status-boundary.{uuid4()}"
    repository = SqlAlchemyStockConnectStatusBoundaryRepository(
        database,
        scope_key=scope_key,
    )
    observed_at = datetime(2026, 7, 30, 10, tzinfo=UTC)
    try:
        first = repository.claim(
            required_from=date(2025, 1, 1),
            manifest_sha256="a" * 64,
            observed_at=observed_at,
        )
        repeated = repository.claim(
            required_from=date(2025, 1, 1),
            manifest_sha256="b" * 64,
            observed_at=observed_at + timedelta(minutes=1),
        )

        assert first == repeated
        assert first.required_from == date(2025, 1, 1)
        with pytest.raises(
            StockConnectStatusBoundaryViolation,
            match="STATUS_BOUNDARY_MOVED_LATER",
        ):
            repository.claim(
                required_from=date(2025, 2, 1),
                manifest_sha256="c" * 64,
                observed_at=observed_at + timedelta(minutes=2),
            )

        tightened = repository.claim(
            required_from=date(2024, 12, 1),
            manifest_sha256="d" * 64,
            observed_at=observed_at + timedelta(minutes=3),
        )
        assert tightened.required_from == date(2024, 12, 1)
        assert tightened.first_locked_at == observed_at
        assert tightened.tightened_at == observed_at + timedelta(minutes=3)

        with pytest.raises(DBAPIError):
            with database.transaction() as session:
                session.execute(
                    update(StockConnectStatusCoverageBoundaryLock)
                    .where(StockConnectStatusCoverageBoundaryLock.scope_key == scope_key)
                    .values(required_from=date(2025, 3, 1))
                )
    finally:
        database.close()


@pytest.mark.integration
def test_future_status_boundary_is_rejected_without_persisting_first_lock() -> None:
    """未来 requiredFrom 必须在首次 insert 前失败，不能留下可被后续误认的锁。"""
    database = DatabaseClient.from_settings(load_settings())
    scope_key = f"test.stock-connect.status-boundary.future.{uuid4()}"
    repository = SqlAlchemyStockConnectStatusBoundaryRepository(
        database,
        scope_key=scope_key,
    )
    observed_at = datetime(2026, 7, 30, 10, tzinfo=UTC)
    try:
        with pytest.raises(
            StockConnectStatusBoundaryViolation,
            match="STATUS_BOUNDARY_IN_FUTURE",
        ):
            repository.claim(
                required_from=date(2026, 7, 31),
                manifest_sha256="e" * 64,
                observed_at=observed_at,
            )
        with database.session() as session:
            assert (
                session.scalar(
                    select(StockConnectStatusCoverageBoundaryLock).where(
                        StockConnectStatusCoverageBoundaryLock.scope_key == scope_key
                    )
                )
                is None
            )
    finally:
        database.close()
