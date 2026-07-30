"""互联互通 coverage audit 从不可变清单到当前完整包的 PostgreSQL 集成测试。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from tests.integration.stock_connect.test_bundle_rollback_repository import (
    _seed_bundle_history,
)

from service_data_sync.application.ports.delivery_manifest import (
    DeliveryManifestTradeDate,
    build_immutable_delivery_manifest,
)
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.entrypoints.stock_connect_coverage_audit import main
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.delivery_manifest_repository import (
    SqlAlchemyDeliveryManifestRepository,
)
from service_data_sync.infrastructure.persistence.stock_connect_status_boundary_repository import (
    SqlAlchemyStockConnectStatusBoundaryRepository,
)
from service_data_sync.infrastructure.providers.official.stock_connect import (
    stock_connect_delivery_manifest_days_from_evidence,
)


@pytest.mark.integration
def test_cli_audits_real_manifest_boundary_and_current_bundle(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """真实 PostgreSQL 中六阶段全集齐备时，CLI 必须以机器 JSON 和退出码 0 通过。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("requires DATA_SYNC_RUN_INTEGRATION=1")
    database = DatabaseClient.from_settings(load_settings())
    try:
        history = _seed_bundle_history(database)
        observed_at = datetime.now(UTC)
        evidence = _provider_evidence(
            trade_date=history.trade_date,
            available_until=observed_at + timedelta(days=1),
        )
        days = tuple(
            DeliveryManifestTradeDate(
                trade_date=trade_date,
                target_count=target_count,
                evidence=day_evidence,
            )
            for trade_date, target_count, day_evidence in (
                stock_connect_delivery_manifest_days_from_evidence(evidence)
            )
        )
        manifest = build_immutable_delivery_manifest(
            manifest_id=uuid4(),
            dataset_code="market.stock_connect.overview.bundle",
            provider_id="official-stock-connect",
            request_hash="8" * 64,
            status="ELIGIBLE",
            available_until=observed_at + timedelta(days=1),
            minimum_remaining_seconds=0,
            created_at=observed_at,
            days=days,
        )
        SqlAlchemyDeliveryManifestRepository(database).persist(manifest)
        SqlAlchemyStockConnectStatusBoundaryRepository(database).claim(
            required_from=date(2014, 11, 17),
            manifest_sha256="4" * 64,
            observed_at=observed_at,
        )

        exit_code = main(
            [
                "--manifest-id",
                str(manifest.manifest_id),
                "--root-hash",
                manifest.root_hash,
            ]
        )
        output = json.loads(capsys.readouterr().out)

        assert exit_code == 0
        assert output["passed"] is True
        assert output["expectedCount"] == 1
        assert output["statusRequiredFrom"] == "2014-11-17"
        assert all(stage["publishedCount"] == 1 for stage in output["stages"].values())
    finally:
        database.close()


def _provider_evidence(*, trade_date: date, available_until: datetime) -> dict[str, object]:
    """构造只有官方交付元数据的单目标证据，不向 production 代码注入行情假值。"""
    trade_date_text = trade_date.isoformat()
    available_until_text = available_until.isoformat().replace("+00:00", "Z")
    body: dict[str, object] = {
        "schema": "quant-v2.stock-connect-preflight-delivery-manifest.v1",
        "providerId": "official-stock-connect",
        "request": {
            "datasetCode": "market.stock_connect.overview.bundle",
            "mode": "DATE_RANGE",
            "selector": {
                "kind": "STOCK_CONNECT",
                "operation": "MARKET",
                "channel": "SH",
                "direction": "NORTHBOUND",
            },
            "dateFrom": trade_date_text,
            "dateTo": trade_date_text,
            "observationDate": None,
        },
        "profileManifestSha256": "1" * 64,
        "calendarManifestSha256": "2" * 64,
        "sftpDeliveryManifestRootHash": "3" * 64,
        "statusManifestSha256": "4" * 64,
        "availableUntil": available_until_text,
        "minimumExecutionWindowSeconds": 0,
        "calendarDeliveries": [
            {
                "year": trade_date.year,
                "payloadSha256": "5" * 64,
            }
        ],
        "sftpDeliveries": [
            {
                "deliveryKind": "DAILY_STATISTICS",
                "channel": "SH",
                "tradeDate": trade_date_text,
                "issuedDate": None,
                "orderReference": "integration-order-reference",
                "availableUntil": available_until_text,
                "available": True,
                "byteSize": 1,
                "remoteModifiedAtEpochSeconds": 1,
            }
        ],
        "statusDeliveries": [
            {
                "channel": "SH",
                "direction": "NORTHBOUND",
                "tradeDate": trade_date_text,
                "available": True,
                "finality": "END_OF_DAY_FINAL",
            }
        ],
        "bundleTargets": [
            {
                "channel": "SH",
                "direction": "NORTHBOUND",
                "tradeDate": trade_date_text,
            }
        ],
    }
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return {**body, "manifestHash": hashlib.sha256(encoded).hexdigest()}
