"""验证 PostgreSQL 不可变交付清单的原子写入、分页读取与防篡改约束。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from sqlalchemy import update
from sqlalchemy.exc import DBAPIError

from service_data_sync.application.ports.delivery_manifest import (
    DeliveryManifestTradeDate,
    DeliveryManifestUnavailable,
    build_immutable_delivery_manifest,
)
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.delivery_manifest import (
    DataOperationDeliveryManifest,
    DataOperationDeliveryManifestPage,
)
from service_data_sync.infrastructure.persistence.delivery_manifest_repository import (
    SqlAlchemyDeliveryManifestRepository,
)


@pytest.mark.integration
def test_postgres_manifest_is_idempotent_paged_and_database_immutable() -> None:
    """真实 PostgreSQL 必须重放同一清单、按页复核，并拒绝 header/page 更新。"""
    database = DatabaseClient.from_settings(load_settings())
    repository = SqlAlchemyDeliveryManifestRepository(database)
    manifest = _manifest()
    try:
        first = repository.persist(manifest)
        second = repository.persist(manifest)
        available = repository.require_available(
            manifest_id=manifest.manifest_id,
            expected_root_hash=manifest.root_hash,
            observed_at=datetime(2026, 7, 30, 12, 5, tzinfo=UTC),
            required_remaining_seconds=900,
        )
        descriptors = repository.list_page_descriptors(
            manifest_id=manifest.manifest_id,
            expected_root_hash=manifest.root_hash,
            observed_at=datetime(2026, 7, 30, 12, 5, tzinfo=UTC),
        )
        page = repository.load_page(
            manifest_id=manifest.manifest_id,
            expected_root_hash=manifest.root_hash,
            page_no=1,
            observed_at=datetime(2026, 7, 30, 12, 5, tzinfo=UTC),
        )
        with pytest.raises(DeliveryManifestUnavailable, match="expired"):
            repository.require_available(
                manifest_id=manifest.manifest_id,
                expected_root_hash=manifest.root_hash,
                observed_at=manifest.available_until + timedelta(seconds=1),
            )
        audit_pages = repository.load_pages_for_audit(
            manifest_id=manifest.manifest_id,
            expected_root_hash=manifest.root_hash,
        )

        assert first == second == available
        assert len(descriptors) == manifest.page_count == 2
        assert page == manifest.pages[1]
        assert audit_pages == manifest.pages

        with pytest.raises(DBAPIError):
            with database.transaction() as session:
                session.execute(
                    update(DataOperationDeliveryManifest)
                    .where(DataOperationDeliveryManifest.manifest_id == manifest.manifest_id)
                    .values(status="REJECTED")
                )
        with pytest.raises(DBAPIError):
            with database.transaction() as session:
                session.execute(
                    update(DataOperationDeliveryManifestPage)
                    .where(
                        DataOperationDeliveryManifestPage.manifest_id == manifest.manifest_id,
                        DataOperationDeliveryManifestPage.page_no == 0,
                    )
                    .values(page_hash="f" * 64)
                )

        assert (
            repository.load_page(
                manifest_id=manifest.manifest_id,
                expected_root_hash=manifest.root_hash,
                page_no=0,
                observed_at=datetime(2026, 7, 30, 12, 5, tzinfo=UTC),
            )
            == manifest.pages[0]
        )
    finally:
        database.close()


def _manifest():
    """构造跨两页的小型确定性清单，重复测试运行复用同一内容身份。"""
    created_at = datetime(2026, 7, 30, 12, tzinfo=UTC)
    initial = build_immutable_delivery_manifest(
        manifest_id=UUID("00000000-0000-4000-8000-000000000001"),
        dataset_code="market.stock_connect.overview.bundle",
        provider_id="official-stock-connect",
        request_hash="c" * 64,
        status="ELIGIBLE",
        available_until=created_at + timedelta(days=3650),
        minimum_remaining_seconds=600,
        created_at=created_at,
        days=tuple(
            DeliveryManifestTradeDate(
                trade_date=(trade_date := date(2026, 1, 1) + timedelta(days=index)),
                target_count=4,
                evidence={
                    "bundleTargets": [
                        {
                            "channel": channel,
                            "direction": direction,
                            "tradeDate": trade_date.isoformat(),
                        }
                        for channel, direction in (
                            ("SH", "NORTHBOUND"),
                            ("SH", "SOUTHBOUND"),
                            ("SZ", "NORTHBOUND"),
                            ("SZ", "SOUTHBOUND"),
                        )
                    ]
                },
            )
            for index in range(21)
        ),
    )
    return replace(
        initial,
        manifest_id=uuid5(NAMESPACE_URL, f"quant-v2:delivery-manifest:{initial.root_hash}"),
    )
