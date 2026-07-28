"""证券目录主数据的标准领域值。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from service_data_sync.domain.equity import EquityIdentifier


class EquityCatalogCompletenessError(ValueError):
    """表示完整目录相对稳定基线异常缩减，必须阻断本次发布。"""


class EquityLifecycleStatus(StrEnum):
    """表示交易所明确发布的上市生命周期状态，不包含普通交易停牌。"""

    LISTED = "LISTED"
    SUSPENDED = "SUSPENDED"
    DELISTED = "DELISTED"


class EquityLifecycleEvidenceKind(StrEnum):
    """表示可改变上市生命周期的显式证据语义。"""

    EXPLICIT_LISTING = "EXPLICIT_LISTING"
    EXPLICIT_SUSPENSION = "EXPLICIT_SUSPENSION"
    EXPLICIT_RESUMPTION = "EXPLICIT_RESUMPTION"
    EXPLICIT_DELISTING = "EXPLICIT_DELISTING"
    OFFICIAL_CORRECTION = "OFFICIAL_CORRECTION"


@dataclass(frozen=True, slots=True)
class EquityCatalogEntry:
    """表示一所交易所在某目标日完整目录中的一只在册证券。"""

    identifier: EquityIdentifier
    name: str
    listed_on: date | None

    def __post_init__(self) -> None:
        """拒绝无法作为已确认目录事实发布的空名称。"""
        if not self.name.strip():
            raise ValueError("catalog entry name must not be blank")


@dataclass(frozen=True, slots=True)
class EquityLifecycleEntry:
    """表示一条已通过字段语义验证的显式上市生命周期事实。"""

    identifier: EquityIdentifier
    status: EquityLifecycleStatus
    effective_on: date
    evidence_kind: EquityLifecycleEvidenceKind
    listed_on: date | None = None
    delisted_on: date | None = None
    correction_approval_reference: str | None = None

    def __post_init__(self) -> None:
        """阻止目录缺席或普通停牌伪装成可发布的生命周期转换。"""
        expected_evidence = {
            EquityLifecycleStatus.LISTED: {
                EquityLifecycleEvidenceKind.EXPLICIT_LISTING,
                EquityLifecycleEvidenceKind.EXPLICIT_RESUMPTION,
                EquityLifecycleEvidenceKind.OFFICIAL_CORRECTION,
            },
            EquityLifecycleStatus.SUSPENDED: {
                EquityLifecycleEvidenceKind.EXPLICIT_SUSPENSION,
                EquityLifecycleEvidenceKind.OFFICIAL_CORRECTION,
            },
            EquityLifecycleStatus.DELISTED: {
                EquityLifecycleEvidenceKind.EXPLICIT_DELISTING,
                EquityLifecycleEvidenceKind.OFFICIAL_CORRECTION,
            },
        }
        if self.evidence_kind not in expected_evidence[self.status]:
            raise ValueError("lifecycle evidence kind does not authorize status")
        if self.status is EquityLifecycleStatus.DELISTED and self.delisted_on is None:
            raise ValueError("delisted lifecycle entry requires delisted_on")
        if self.delisted_on is not None and self.delisted_on < self.effective_on:
            raise ValueError("delisted_on must not predate effective_on")
        if self.listed_on is not None and self.delisted_on is not None:
            if self.delisted_on < self.listed_on:
                raise ValueError("delisted_on must not predate listed_on")
        if self.evidence_kind is EquityLifecycleEvidenceKind.OFFICIAL_CORRECTION:
            if (
                not self.correction_approval_reference
                or not self.correction_approval_reference.strip()
            ):
                raise ValueError("official correction requires source evidence reference")
        elif self.correction_approval_reference is not None:
            raise ValueError("only official correction may include source evidence reference")


class EquityIdentityResolutionStatus(StrEnum):
    """描述标识历史查询的确定性结果，调用方不得用名称或排序补偿。"""

    RESOLVED = "resolved"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class EquityIdentityResolution:
    """表示某事实日期和知识时间下一个代码的可写入身份解析结果。"""

    status: EquityIdentityResolutionStatus
    security_id: int | None = None
    identity_state: str | None = None

    def __post_init__(self) -> None:
        """确保只有唯一命中才会携带可供写入者使用的数据库身份。"""
        if self.status is EquityIdentityResolutionStatus.RESOLVED:
            if self.security_id is None or self.identity_state is None:
                raise ValueError("resolved identity requires security_id and identity_state")
        elif self.security_id is not None or self.identity_state is not None:
            raise ValueError("unresolved identity must not expose a security_id")
