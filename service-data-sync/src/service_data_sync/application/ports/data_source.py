"""数据源无关的获取契约。

应用层只能通过本模块请求某项 `capability` 并接收标准化批次。
供应商名称、`SDK`、`URL` 和重试细节由 `adapter` 隔离在基础设施层。
批次同时携带观察时间、版本和可选原始字节，使失败留证与来源追溯不依赖具体数据源。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ProviderErrorCode(StrEnum):
    """`adapter` 可跨数据源传递的失败分类。

    应用层据此判断错误是配置/请求/`schema` 问题，还是暂时的来源不可用或限流。
    具体异常类型不应穿过端口边界。
    """

    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION = "authentication"
    INVALID_REQUEST = "invalid_request"
    SCHEMA = "schema"
    CURRENTLY_UNSUPPORTED = "currently_unsupported"


class ProviderError(RuntimeError):
    """`adapter` 抛出的数据源无关失败。

    `code` 说明业务失败类别，`retryable` 明确任务是否可以重试。
    调用方不能根据供应商错误文本猜测处理方式。
    """

    def __init__(self, code: ProviderErrorCode, message: str, *, retryable: bool) -> None:
        """保存可移植失败分类、重试策略和可选的脱敏失败证据。"""
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.failure_evidence: bytes | None = None
        self.failure_evidence_content_type: str | None = None

    def attach_failure_evidence(
        self,
        payload: bytes,
        *,
        content_type: str = "application/json",
    ) -> None:
        """附加 adapter 生成的脱敏审计摘要；禁止放入凭据或供应商响应原文。"""
        self.failure_evidence = payload
        self.failure_evidence_content_type = content_type


@dataclass(frozen=True, slots=True)
class SourceRequest:
    """最小不可变请求结构，具体数据语义由 `capability` 契约定义。

    `parameters` 是已标准化的键值对，而非供应商 `SDK` 参数对象。
    这让应用服务能够表达请求意图而不依赖某个来源实现。
    """

    capability: str
    parameters: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        """拒绝无法定位数据能力的空 `capability` 请求。"""
        if not self.capability.strip():
            raise ValueError("capability must not be blank")


@dataclass(frozen=True, slots=True)
class ProviderBatch:
    """承载标准化载荷和仅失败留证信息的数据源无关批次。

    `payload` 是 `adapter` 已归一化、供应用层解码的字节。
    `raw_payload` 是可选的原始响应，只能在失败排障时持久化。
    `observed_at` 必须带时区，表示服务真正观察到来源的时间；它不同于行情日期、报告期或来源公告时间。
    """

    provider_id: str
    capability: str
    payload: bytes
    observed_at: datetime
    content_type: str = "application/octet-stream"
    raw_payload: bytes | None = None
    raw_content_type: str | None = None
    upstream_source: str | None = None
    adapter_version: str = "unversioned"
    schema_fingerprint: str | None = None

    def __post_init__(self) -> None:
        """校验批次仍保有来源、能力、版本和带时区观察时间等可追溯信息。"""
        if not self.provider_id.strip():
            raise ValueError("provider_id must not be blank")
        if not self.capability.strip():
            raise ValueError("capability must not be blank")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        if self.raw_payload is None and self.raw_content_type is not None:
            raise ValueError("raw_content_type requires raw_payload")
        if not self.adapter_version.strip():
            raise ValueError("adapter_version must not be blank")
        if self.upstream_source is not None and not self.upstream_source.strip():
            raise ValueError("upstream_source must not be blank when provided")
        if self.schema_fingerprint is not None and len(self.schema_fingerprint) != 64:
            raise ValueError("schema_fingerprint must be a SHA-256 hex digest")

    @classmethod
    def empty(cls, provider_id: str, capability: str) -> ProviderBatch:
        """为已支持的 `capability` 创建带时区观察时间的合法空结果。"""
        return cls(
            provider_id=provider_id,
            capability=capability,
            payload=b"",
            observed_at=datetime.now(UTC),
        )


@dataclass(frozen=True, slots=True)
class ProviderPreflightRequest:
    """描述人工提交前一次有界、只读且不产生业务写入的来源探测。"""

    dataset_code: str
    mode: str
    selector: Mapping[str, object]
    date_from: str | None
    date_to: str | None
    observation_date: str | None
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class ProviderPreflightComponent:
    """返回一个来源组件的可提交结论与不泄密稳定原因码。"""

    component: str
    accepted: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ProviderPreflightReport:
    """汇总来源探针结论，并携带仅供内部受理冻结的可审计交付证据。"""

    components: tuple[ProviderPreflightComponent, ...]
    execution_evidence: Mapping[str, object] | None = None
    readiness_evidence: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ProviderStatusCoverageBoundary:
    """描述来源状态清单声明、但仍需持久化门禁确认的 coverage 边界。"""

    required_from: date
    manifest_sha256: str


@runtime_checkable
class SourceStatusCoverageBoundaryPort(Protocol):
    """由具有历史缺源豁免的来源暴露不发网络的状态覆盖边界声明。"""

    def status_coverage_boundary(self) -> ProviderStatusCoverageBoundary:
        """返回已完成清单 schema 与摘要校验的候选边界。"""
        ...


@runtime_checkable
class SourcePreflightProbePort(Protocol):
    """由需要在线 entitlement 校验的数据源实现只读 preflight probe。"""

    def preflight_probe(self, request: ProviderPreflightRequest) -> ProviderPreflightReport:
        """在总 deadline 内验证认证、来源文件与本地 landing，不写业务状态。"""
        ...


@runtime_checkable
class SourcePreflightVerificationPort(Protocol):
    """由来源在执行前复核受理时冻结的交付证据，阻断清单漂移。"""

    def verify_preflight_evidence(
        self,
        evidence: Mapping[str, object],
        *,
        timeout_seconds: int,
        target_keys: tuple[str, ...] | None = None,
    ) -> tuple[ProviderPreflightComponent, ...]:
        """复核整份或指定内部批次的来源对象，并要求目标版本与冻结证据一致。"""
        ...


@runtime_checkable
class DataSourcePort(Protocol):
    """应用层访问外部数据源时唯一允许使用的 `Protocol` 抽象。

    具体 `adapter` 在基础设施层实现该协议。
    应用服务只检查其 `provider_id`、声明能力并请求标准批次，不能触及 `SDK` 或网络细节。
    """

    @property
    def provider_id(self) -> str:
        """返回只读来源身份；包装器不得改变已获准 `adapter` 的归属。"""
        ...

    def capabilities(self) -> frozenset[str]:
        """不发起网络请求，返回该来源明确声明支持的 `capability` 集合。"""
        ...

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """按中立请求获取一个标准批次；失败时抛出带重试语义的 `ProviderError`。"""
        ...
