"""显式导入全部逻辑表模型，供维护者和 Alembic 使用唯一 metadata 入口。"""

from __future__ import annotations

from .base import Base

# 个股身份、生命周期和日线行情。
from .equity.identity.equity_identifier_version import EquityIdentifierVersion
from .equity.identity.equity_identity_quarantine import EquityIdentityQuarantine
from .equity.identity.equity_instrument import EquityInstrument
from .equity.identity.equity_listing_status_version import EquityListingStatusVersion
from .equity.identity.equity_master_snapshot import EquityMasterSnapshot
from .equity.identity.equity_master_snapshot_member import EquityMasterSnapshotMember
from .equity.identity.equity_name_version import EquityNameVersion
from .equity.identity.equity_presence_anomaly import EquityPresenceAnomaly
from .equity.market_data.equity_daily_bar import EquityDailyBar

# 来源证据与运行账本。
from .execution.sync_partition import SyncPartition
from .execution.sync_run import SyncRun
from .provenance.source_batch import SourceBatch

# 发布版本与质量问题。
from .publication.data_quality_issue import DataQualityIssue
from .publication.dataset_publication import DatasetPublication
from .publication.dataset_publication_component import DatasetPublicationComponent

# 板块目录与日、周、月行情。
from .sector.catalog.sector_entity import SectorEntity
from .sector.catalog.sector_scheme import SectorScheme

# 板块 EOD 快照、明细和运行账本。
from .sector.eod.sector_eod_quality_result import SectorEodQualityResult
from .sector.eod.sector_eod_quote import SectorEodQuote
from .sector.eod.sector_eod_snapshot import SectorEodSnapshot
from .sector.eod.sector_eod_sync_partition import SectorEodSyncPartition
from .sector.market_data.sector_daily_bar import SectorDailyBar
from .sector.market_data.sector_monthly_bar import SectorMonthlyBar
from .sector.market_data.sector_weekly_bar import SectorWeeklyBar

# 板块成分观测、质量、区间与发布。
from .sector.membership.sector_membership_interval import SectorMembershipInterval
from .sector.membership.sector_membership_item import SectorMembershipItem
from .sector.membership.sector_membership_pending import SectorMembershipPending
from .sector.membership.sector_membership_quality_result import SectorMembershipQualityResult
from .sector.membership.sector_membership_quarantine import SectorMembershipQuarantine
from .sector.membership.sector_membership_release import SectorMembershipRelease
from .sector.membership.sector_membership_release_sector import SectorMembershipReleaseSector
from .sector.membership.sector_membership_snapshot import SectorMembershipSnapshot

ALL_MODELS: tuple[type[Base], ...] = (
    SourceBatch,
    SyncRun,
    SyncPartition,
    DatasetPublication,
    DatasetPublicationComponent,
    DataQualityIssue,
    EquityInstrument,
    EquityIdentifierVersion,
    EquityNameVersion,
    EquityListingStatusVersion,
    EquityMasterSnapshot,
    EquityMasterSnapshotMember,
    EquityPresenceAnomaly,
    EquityIdentityQuarantine,
    EquityDailyBar,
    SectorScheme,
    SectorEntity,
    SectorDailyBar,
    SectorWeeklyBar,
    SectorMonthlyBar,
    SectorMembershipSnapshot,
    SectorMembershipItem,
    SectorMembershipPending,
    SectorMembershipQuarantine,
    SectorMembershipQualityResult,
    SectorMembershipInterval,
    SectorMembershipRelease,
    SectorMembershipReleaseSector,
    SectorEodSyncPartition,
    SectorEodSnapshot,
    SectorEodQuote,
    SectorEodQualityResult,
)

__all__ = ["ALL_MODELS", "Base"]
