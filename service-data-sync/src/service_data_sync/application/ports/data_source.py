"""数据源无关的获取契约。

应用层只能通过本模块请求某项 `capability` 并接收标准化批次。
供应商名称、`SDK`、`URL` 和重试细节由 `adapter` 隔离在基础设施层。
批次同时携带观察时间、版本和可选原始字节，使失败留证与来源追溯不依赖具体数据源。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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


class ProviderError(RuntimeError):
    """`adapter` 抛出的数据源无关失败。

    `code` 说明业务失败类别，`retryable` 明确任务是否可以重试。
    调用方不能根据供应商错误文本猜测处理方式。
    """

    def __init__(self, code: ProviderErrorCode, message: str, *, retryable: bool) -> None:
        """保存可移植失败分类、面向日志的消息和是否允许重试的策略。"""
        super().__init__(message)
        self.code = code
        self.retryable = retryable


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
