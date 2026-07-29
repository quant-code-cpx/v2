"""跨域 `canonical` 发布的端口与传输无关值对象。

它定义消费者可见版本所需的来源血缘、质量结论和内容摘要。
它不规定这些信息存进哪个数据库或由哪个 `API` 暴露。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CanonicalLineageRecord:
    """表示 release 中一条强类型事实与其直接证据的最小可审计关联。"""

    record_key_hash: str
    content_hash: str
    source_batch_id: UUID
    transform_hash: str
    role: str = "primary"
    raw_payload_id: UUID | None = None

    def __post_init__(self) -> None:
        """拒绝无法定位、无法验证或未受控的血缘角色。"""
        for value, label in (
            (self.record_key_hash, "record key"),
            (self.content_hash, "content"),
            (self.transform_hash, "transform"),
        ):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"canonical {label} hash must be lowercase SHA-256")
        if self.role not in {"primary", "corroborating", "input"}:
            raise ValueError("canonical lineage role is invalid")


@dataclass(frozen=True, slots=True)
class CanonicalQualityRule:
    """表示版本化质量策略内一条规则的可发布性结论。"""

    rule_code: str
    severity: str
    passed: bool

    def __post_init__(self) -> None:
        """限制规则标识与严重级别，避免调用方拼写差异绕过发布门。"""
        if not self.rule_code.strip():
            raise ValueError("canonical quality rule code must not be blank")
        if self.severity not in {"info", "warn", "blocking"}:
            raise ValueError("canonical quality severity is invalid")


@dataclass(frozen=True, slots=True)
class CanonicalQualityDecision:
    """表示一个已完成 normalization run 的发布级质量结论。"""

    status: str
    policy_code: str
    policy_version: int
    rules: tuple[CanonicalQualityRule, ...]

    def __post_init__(self) -> None:
        """确保评估状态和规则集没有矛盾或重复编码。"""
        if self.status not in {"passed", "warned", "partial"}:
            raise ValueError("canonical release quality status is invalid")
        if not self.policy_code.strip() or self.policy_version <= 0:
            raise ValueError("canonical quality policy identity is invalid")
        if not self.rules:
            raise ValueError("canonical quality decision requires at least one rule")
        if len({rule.rule_code for rule in self.rules}) != len(self.rules):
            raise ValueError("canonical quality rule codes must be unique")
        if any(rule.severity == "blocking" and not rule.passed for rule in self.rules):
            raise ValueError("blocking quality rule prevents canonical publication")


@dataclass(frozen=True, slots=True)
class CanonicalReleaseCandidate:
    """描述一个已写入强类型事实、等待原子对消费者可见的固定内容集合。"""

    dataset_id: UUID
    dataset_code: str
    partition_key: str
    methodology_version_id: UUID
    normalization_run_id: UUID
    records: tuple[CanonicalLineageRecord, ...]
    quality: CanonicalQualityDecision
    fact_min: date | None
    fact_max: date | None
    checkpoint_kind: str
    checkpoint_position: dict[str, object]
    expected_fencing_token: int
    created_at: datetime

    def __post_init__(self) -> None:
        """校验发布分区、事实日期和 CAS 前提，禁止模糊或倒退式提交。"""
        if not self.dataset_code.strip() or not self.partition_key.strip():
            raise ValueError("canonical dataset code and partition key must not be blank")
        if not self.checkpoint_kind.strip() or not self.checkpoint_position:
            raise ValueError("canonical checkpoint kind and position are required")
        if self.expected_fencing_token < 0:
            raise ValueError("canonical expected fencing token must be non-negative")
        if self.created_at.tzinfo is None:
            raise ValueError("canonical release created_at must include a timezone")
        if self.fact_max is not None and (self.fact_min is None or self.fact_max < self.fact_min):
            raise ValueError("canonical fact date range is invalid")
        if len({record.record_key_hash for record in self.records}) != len(self.records):
            raise ValueError("canonical release record keys must be unique")


@dataclass(frozen=True, slots=True)
class PublishedCanonicalRelease:
    """返回 immutable release 与消费者 data version 的稳定发布结果。"""

    release_id: UUID
    data_version: UUID
    reused_release: bool
    reused_publication: bool
    published_at: datetime


class CanonicalReleaseRepository(Protocol):
    """负责把已验证候选原子固化为 release、publication 与 checkpoint。"""

    def publish(self, candidate: CanonicalReleaseCandidate) -> PublishedCanonicalRelease:
        """提交候选；必须在同一事务内处理 release、血缘、版本指针及 fencing。"""
        ...
