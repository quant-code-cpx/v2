"""对不可变交付清单与已发布完整包执行全量互联互通 coverage 对账。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select

from service_data_sync.application.ports.delivery_manifest import DeliveryManifestPage
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.delivery_manifest import (
    StockConnectStatusCoverageBoundaryLock,
)
from service_data_sync.infrastructure.database.models.market import StockConnectBundlePublication
from service_data_sync.infrastructure.persistence.delivery_manifest_repository import (
    SqlAlchemyDeliveryManifestRepository,
)
from service_data_sync.infrastructure.providers.official.stock_connect import (
    stock_connect_bundle_targets_from_evidence,
    stock_connect_preflight_evidence_from_delivery_page,
)

type StockConnectCoverageKey = tuple[date, str, str]
_STAGE_NAMES = ("entitlement", "object", "status", "market", "active", "bundle")
_GAP_SUMMARY_LIMIT = 100
_STATUS_BOUNDARY_SCOPE = "market.stock_connect.channel_status.eod"


@dataclass(frozen=True, slots=True)
class StockConnectCoverageStage:
    """汇总一个交付或 publication 阶段的完整集合差异。"""

    expected_count: int
    published_count: int
    missing_count: int
    duplicate_count: int
    out_of_range_count: int
    gaps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StockConnectCoverageAudit:
    """返回从官方日历目标到当前 bundle 的机器可判定全量审计。"""

    manifest_id: UUID
    root_hash: str
    minimum_trade_date: date
    maximum_trade_date: date
    expected_count: int
    status_required_from: date
    status_historical_warning_count: int
    stages: Mapping[str, StockConnectCoverageStage]
    passed: bool


def audit_stock_connect_coverage_sets(
    *,
    manifest_id: UUID,
    root_hash: str,
    expected: Sequence[StockConnectCoverageKey],
    observations: Mapping[str, Sequence[StockConnectCoverageKey]],
    status_required_from: date,
) -> StockConnectCoverageAudit:
    """逐阶段计算 missing、duplicate 与 out-of-range，不用抽样替代全集合比较。"""
    if (
        len(root_hash) != 64
        or root_hash != root_hash.lower()
        or any(character not in "0123456789abcdef" for character in root_hash)
    ):
        raise ValueError("stock-connect coverage root hash is invalid")
    expected_counter = Counter(expected)
    if not expected_counter or any(count != 1 for count in expected_counter.values()):
        raise ValueError("stock-connect expected coverage targets must be non-empty and unique")
    expected_set = set(expected_counter)
    ordered_expected = sorted(expected_set)
    required_status = {key for key in expected_set if key[0] >= status_required_from}
    historical_status = expected_set.difference(required_status)
    unknown_stages = set(observations).difference(_STAGE_NAMES)
    if unknown_stages:
        raise ValueError("stock-connect coverage contains an unknown stage")

    stages: dict[str, StockConnectCoverageStage] = {}
    for stage_name in _STAGE_NAMES:
        observed = tuple(observations.get(stage_name, ()))
        required = required_status if stage_name == "status" else expected_set
        allowed = expected_set
        stages[stage_name] = _coverage_stage(
            required=required,
            allowed=allowed,
            observed=observed,
        )
    observed_status = set(observations.get("status", ()))
    historical_warning_count = len(historical_status.difference(observed_status))
    passed = all(
        stage.missing_count == 0 and stage.duplicate_count == 0 and stage.out_of_range_count == 0
        for stage in stages.values()
    )
    return StockConnectCoverageAudit(
        manifest_id=manifest_id,
        root_hash=root_hash,
        minimum_trade_date=ordered_expected[0][0],
        maximum_trade_date=ordered_expected[-1][0],
        expected_count=len(expected_set),
        status_required_from=status_required_from,
        status_historical_warning_count=historical_warning_count,
        stages=stages,
        passed=passed,
    )


class SqlAlchemyStockConnectCoverageAuditRepository:
    """从摘要复核后的 manifest pages 与当前 bundle 行生成全量 coverage 审计。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存数据库连接，并复用不可变清单仓储的摘要链校验。"""
        self._database = database
        self._delivery_manifests = SqlAlchemyDeliveryManifestRepository(database)

    def audit(
        self,
        *,
        manifest_id: UUID,
        root_hash: str,
    ) -> StockConnectCoverageAudit:
        """以持久化状态边界审计完整 preflight manifest，不因 entitlement 到期跳过历史完成度。"""
        pages = self._delivery_manifests.load_pages_for_audit(
            manifest_id=manifest_id,
            expected_root_hash=root_hash,
        )
        expected, entitlement, objects, statuses = _manifest_observations(pages)
        market, active, bundles, status_required_from = self._publication_observations(expected)
        return audit_stock_connect_coverage_sets(
            manifest_id=manifest_id,
            root_hash=root_hash,
            expected=expected,
            observations={
                "entitlement": entitlement,
                "object": objects,
                "status": statuses,
                "market": market,
                "active": active,
                "bundle": bundles,
            },
            status_required_from=status_required_from,
        )

    def _publication_observations(
        self,
        expected: Sequence[StockConnectCoverageKey],
    ) -> tuple[
        tuple[StockConnectCoverageKey, ...],
        tuple[StockConnectCoverageKey, ...],
        tuple[StockConnectCoverageKey, ...],
        date,
    ]:
        """在同一只读会话读取锁定状态边界和当前 bundle，并独立核对 market/active 组件。"""
        expected_set = set(expected)
        minimum = min(key[0] for key in expected_set)
        maximum = max(key[0] for key in expected_set)
        selected_pairs = {(key[1], key[2]) for key in expected_set}
        with self._database.session() as session:
            boundary = session.scalar(
                select(StockConnectStatusCoverageBoundaryLock).where(
                    StockConnectStatusCoverageBoundaryLock.scope_key == _STATUS_BOUNDARY_SCOPE
                )
            )
            if boundary is None:
                raise RuntimeError("stock-connect status coverage boundary is unavailable")
            rows = session.scalars(
                select(StockConnectBundlePublication).where(
                    StockConnectBundlePublication.trade_date >= minimum,
                    StockConnectBundlePublication.trade_date <= maximum,
                    StockConnectBundlePublication.superseded_at.is_(None),
                    or_(
                        *(
                            and_(
                                StockConnectBundlePublication.channel == channel,
                                StockConnectBundlePublication.direction == direction,
                            )
                            for channel, direction in sorted(selected_pairs)
                        )
                    ),
                )
            ).all()
        bundle_keys = tuple((row.trade_date, row.channel, row.direction) for row in rows)
        market_keys = tuple(
            (row.trade_date, row.channel, row.direction)
            for row in rows
            if row.market_release_id is not None
        )
        active_keys = tuple(
            (row.trade_date, row.channel, row.direction)
            for row in rows
            if row.active_release_id is not None or row.active_security_count == 0
        )
        return market_keys, active_keys, bundle_keys, boundary.required_from


def stock_connect_coverage_audit_view(audit: StockConnectCoverageAudit) -> dict[str, object]:
    """把审计结果转换为稳定 JSON，供 CLI 和自动验收直接判定。"""
    return {
        "schema": "quant-v2.stock-connect-coverage-audit.v1",
        "manifestId": str(audit.manifest_id),
        "rootHash": audit.root_hash,
        "minimumTradeDate": audit.minimum_trade_date.isoformat(),
        "maximumTradeDate": audit.maximum_trade_date.isoformat(),
        "expectedCount": audit.expected_count,
        "statusRequiredFrom": audit.status_required_from.isoformat(),
        "statusHistoricalWarningCount": audit.status_historical_warning_count,
        "stages": {
            name: {
                "expectedCount": stage.expected_count,
                "publishedCount": stage.published_count,
                "missingCount": stage.missing_count,
                "duplicateCount": stage.duplicate_count,
                "outOfRangeCount": stage.out_of_range_count,
                "gaps": list(stage.gaps),
            }
            for name, stage in audit.stages.items()
        },
        "passed": audit.passed,
    }


def _manifest_observations(
    pages: Sequence[DeliveryManifestPage],
) -> tuple[
    tuple[StockConnectCoverageKey, ...],
    tuple[StockConnectCoverageKey, ...],
    tuple[StockConnectCoverageKey, ...],
    tuple[StockConnectCoverageKey, ...],
]:
    """从每页已验证 evidence 恢复 expected、entitlement、对象和最终状态集合。"""
    expected: list[StockConnectCoverageKey] = []
    entitlement: list[StockConnectCoverageKey] = []
    objects: list[StockConnectCoverageKey] = []
    statuses: list[StockConnectCoverageKey] = []
    for page in pages:
        evidence = stock_connect_preflight_evidence_from_delivery_page(page.evidence)
        page_targets = list(stock_connect_bundle_targets_from_evidence(evidence))
        expected.extend(
            (trade_date, channel, direction) for channel, direction, trade_date in page_targets
        )
        sftp_rows = _mapping_rows(evidence.get("sftpDeliveries"))
        for row in sftp_rows:
            if row.get("deliveryKind") != "DAILY_STATISTICS":
                continue
            channel = row.get("channel")
            trade_date_value = row.get("tradeDate")
            if not isinstance(channel, str) or not isinstance(trade_date_value, str):
                continue
            trade_date = date.fromisoformat(trade_date_value)
            matching = [key for key in page_targets if key[0] == channel and key[2] == trade_date]
            keys = [
                (current, selected_channel, direction)
                for selected_channel, direction, current in matching
            ]
            if isinstance(row.get("orderReference"), str) and isinstance(
                row.get("availableUntil"), str
            ):
                entitlement.extend(keys)
            if (
                row.get("available") is True
                and isinstance(row.get("byteSize"), int)
                and int(row["byteSize"]) > 0
                and isinstance(row.get("remoteModifiedAtEpochSeconds"), int)
            ):
                objects.extend(keys)
        for row in _mapping_rows(evidence.get("statusDeliveries")):
            if row.get("available") is not True or row.get("finality") != "END_OF_DAY_FINAL":
                continue
            channel = row.get("channel")
            direction = row.get("direction")
            trade_date_value = row.get("tradeDate")
            if (
                isinstance(channel, str)
                and isinstance(direction, str)
                and isinstance(trade_date_value, str)
            ):
                statuses.append((date.fromisoformat(trade_date_value), channel, direction))
    return tuple(expected), tuple(entitlement), tuple(objects), tuple(statuses)


def _coverage_stage(
    *,
    required: set[StockConnectCoverageKey],
    allowed: set[StockConnectCoverageKey],
    observed: Sequence[StockConnectCoverageKey],
) -> StockConnectCoverageStage:
    """计算单阶段集合差异并限制 gap 摘要大小，完整计数不受截断影响。"""
    counter = Counter(observed)
    observed_set = set(counter)
    missing = sorted(required.difference(observed_set))
    duplicates = sum(max(0, count - 1) for count in counter.values())
    out_of_range = sum(count for key, count in counter.items() if key not in allowed)
    return StockConnectCoverageStage(
        expected_count=len(required),
        published_count=len(required.intersection(observed_set)),
        missing_count=len(missing),
        duplicate_count=duplicates,
        out_of_range_count=out_of_range,
        gaps=tuple(_coverage_key(key) for key in missing[:_GAP_SUMMARY_LIMIT]),
    )


def _mapping_rows(value: object) -> tuple[Mapping[str, Any], ...]:
    """把已由 provider evidence 校验的列表安全收窄为映射序列。"""
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError("stock-connect coverage evidence rows are invalid")
    return tuple(item for item in value if isinstance(item, Mapping))


def _coverage_key(key: StockConnectCoverageKey) -> str:
    """把业务主键转换为机器可排序的 gap 文本。"""
    trade_date, channel, direction = key
    return f"{trade_date.isoformat()}:{channel}:{direction}"
