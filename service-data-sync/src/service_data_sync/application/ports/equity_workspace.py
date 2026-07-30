"""股票中心缺失事实同步与冻结发现投影的应用端口。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from service_data_sync.domain.equity import EquityIdentifier
from service_data_sync.domain.equity_workspace import (
    EquityShareCapital,
    EquityTradingStatus,
    SwEquityMembership,
)


@dataclass(frozen=True, slots=True)
class EquityWorkspaceSourceObservation:
    """表示股票中心来源调用的 raw 与标准 JSON 摘要及受控引用。"""

    provider_id: str
    capability: str
    raw_payload_sha256: str
    raw_uri: str
    raw_content_type: str
    raw_byte_size: int
    normalized_payload_sha256: str
    normalized_uri: str
    normalized_content_type: str
    normalized_byte_size: int
    observed_at: datetime
    upstream_source: str
    adapter_version: str
    schema_fingerprint: str


@dataclass(frozen=True, slots=True)
class PublishedEquityWorkspaceDataset:
    """描述一次股票中心事实发布及其幂等变更统计。"""

    data_version: UUID
    inserted_count: int
    unchanged_count: int


class EquityWorkspaceRepository(Protocol):
    """负责新增事实的 revision、canonical release 与 publication 原子提交。"""

    def publish_trading_statuses(
        self,
        *,
        observation_date: date,
        statuses: Sequence[EquityTradingStatus],
        source: EquityWorkspaceSourceObservation,
    ) -> PublishedEquityWorkspaceDataset:
        """发布一个观察日明确披露的普通停牌清单。"""
        ...

    def publish_share_capital(
        self,
        *,
        identifier: EquityIdentifier,
        instrument_id: UUID,
        identity_as_of: date,
        structures: Sequence[EquityShareCapital],
        source: EquityWorkspaceSourceObservation,
    ) -> PublishedEquityWorkspaceDataset:
        """按受理时冻结的永久身份发布一只证券来源报告的完整历史股本结构。"""
        ...

    def publish_sw_memberships(
        self,
        *,
        node_code: str,
        observation_date: date,
        memberships: Sequence[SwEquityMembership],
        source: EquityWorkspaceSourceObservation,
    ) -> PublishedEquityWorkspaceDataset:
        """发布一个申万三级节点的当前完整证券归属快照。"""
        ...
