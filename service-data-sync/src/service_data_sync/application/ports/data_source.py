"""数据源无关契约；本模块不得依赖基础设施实现。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ProviderErrorCode(StrEnum):
    """适配器可跨数据源传递的失败分类。"""

    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION = "authentication"
    INVALID_REQUEST = "invalid_request"
    SCHEMA = "schema"


class ProviderError(RuntimeError):
    """适配器抛出的数据源无关失败。"""

    def __init__(self, code: ProviderErrorCode, message: str, *, retryable: bool) -> None:
        """保存可移植的失败分类及是否允许重试的策略。"""
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class SourceRequest:
    """最小不可变请求结构，具体数据语义由能力契约定义。"""

    capability: str
    parameters: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        """拒绝无法标识可用数据源能力的请求。"""
        if not self.capability.strip():
            raise ValueError("capability must not be blank")


@dataclass(frozen=True, slots=True)
class ProviderBatch:
    """同时承载标准化载荷与不可变原始证据的数据源无关批次。"""

    provider_id: str
    capability: str
    payload: bytes
    observed_at: datetime
    content_type: str = "application/octet-stream"
    raw_payload: bytes | None = None
    raw_content_type: str | None = None

    def __post_init__(self) -> None:
        """校验不透明适配器输出保留来源、能力与时区溯源信息。"""
        if not self.provider_id.strip():
            raise ValueError("provider_id must not be blank")
        if not self.capability.strip():
            raise ValueError("capability must not be blank")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        if self.raw_payload is None and self.raw_content_type is not None:
            raise ValueError("raw_content_type requires raw_payload")

    @classmethod
    def empty(cls, provider_id: str, capability: str) -> ProviderBatch:
        """为已支持能力创建带时区的空数据源结果。"""
        return cls(
            provider_id=provider_id,
            capability=capability,
            payload=b"",
            observed_at=datetime.now(UTC),
        )


@runtime_checkable
class DataSourcePort(Protocol):
    """应用层访问外部数据源时唯一允许使用的抽象。"""

    provider_id: str

    def capabilities(self) -> frozenset[str]:
        """不发起网络请求，返回数据源声明的能力集合。"""
        ...

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """获取一个数据源批次，失败时抛出 `ProviderError`。"""
        ...
