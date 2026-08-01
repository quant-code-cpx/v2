"""AKShare 港通市场统计 research 同步的应用端口与中立值对象。

这些端口只承载来源观察、失败证据与 research 事实，不能替代或调用官方港通完整包。字段保留
`None` 表示来源未提供，不以另一通道、持仓、排行或计算结果补齐。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from service_data_sync.application.ports.data_source import ProviderBatch
from service_data_sync.domain.stock_connect import StockConnectChannel


@dataclass(frozen=True, slots=True)
class StockConnectMarketStatResearchRecord:
    """表示一条来源报告的港通市场统计 research 观察。

    与正式 `StockConnectMarketDaily` 不同，币种、整体可用性和逐字段状态允许为空，以保存
    AKShare 标准载荷真实缺失而不虚构官方披露制度。金额仍使用精确十进制，买卖、成交和额度
    余额不能为负；净买额允许为负。
    """

    trade_date: date
    buy_amount: Decimal | None
    sell_amount: Decimal | None
    turnover_amount: Decimal | None
    net_buy_amount: Decimal | None
    quota_balance: Decimal | None
    currency: str | None
    availability_status: str | None
    field_availability: tuple[tuple[str, str], ...] | None

    def __post_init__(self) -> None:
        """校验可选字段仍保持来源语义，拒绝非有限金额或伪造币种格式。"""
        non_negative = (
            self.buy_amount,
            self.sell_amount,
            self.turnover_amount,
            self.quota_balance,
        )
        if any(
            value is not None and (not value.is_finite() or value < 0) for value in non_negative
        ):
            raise ValueError("stock-connect research amounts must be finite and non-negative")
        if self.net_buy_amount is not None and not self.net_buy_amount.is_finite():
            raise ValueError("stock-connect research net buy must be finite")
        if self.currency is not None and (
            len(self.currency) != 3
            or self.currency != self.currency.upper()
            or not self.currency.isascii()
        ):
            raise ValueError("stock-connect research currency is invalid")
        if self.availability_status is not None and not self.availability_status.strip():
            raise ValueError("stock-connect research availability status is blank")
        if self.field_availability is not None:
            values = dict(self.field_availability)
            if len(values) != len(self.field_availability) or any(
                not field.strip() or not status.strip() for field, status in self.field_availability
            ):
                raise ValueError("stock-connect research field availability is invalid")


@dataclass(frozen=True, slots=True)
class StockConnectMarketStatResearchSourceObservation:
    """表示 digest-only 来源清单、adapter 版本与上游身份。

    成功路径的两个 URI 必须是 `unretained://sha256/` 摘要标记；失败字节的私有清单由独立
    failure-evidence 端口管理，不能被此成功观察引用或被消费者读取。
    """

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

    def __post_init__(self) -> None:
        """验证来源摘要、大小、时区和必填血缘，避免半成品 evidence 进入数据库。"""
        hashes = (
            self.raw_payload_sha256,
            self.normalized_payload_sha256,
            self.schema_fingerprint,
        )
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in hashes
        ):
            raise ValueError("stock-connect research source hash is invalid")
        if self.raw_byte_size < 0 or self.normalized_byte_size < 0:
            raise ValueError("stock-connect research source size is invalid")
        if self.observed_at.tzinfo is None:
            raise ValueError("stock-connect research source observed_at requires timezone")
        if any(
            not value.strip()
            for value in (
                self.provider_id,
                self.capability,
                self.raw_uri,
                self.raw_content_type,
                self.normalized_uri,
                self.normalized_content_type,
                self.upstream_source,
                self.adapter_version,
            )
        ):
            raise ValueError("stock-connect research source lineage is incomplete")


@dataclass(frozen=True, slots=True)
class StoredStockConnectMarketStatResearchBatch:
    """返回已原子写入的 research 批次，不携带 publication 或 dataVersion。"""

    research_batch_id: UUID
    source_batch_id: UUID
    inserted_count: int
    quality_status: str


class StockConnectMarketStatResearchRepository(Protocol):
    """持有 AKShare 市场统计 research 观察，禁止创建正式港通发布。"""

    def record_market_statistics(
        self,
        *,
        channel: StockConnectChannel,
        records: Sequence[StockConnectMarketStatResearchRecord],
        source: StockConnectMarketStatResearchSourceObservation,
    ) -> StoredStockConnectMarketStatResearchBatch:
        """原子保存来源批次、摘要清单、规范化、质量和研究观察。"""
        ...


class StockConnectMarketStatFailureEvidenceStore(Protocol):
    """声明只在同步失败时固化私有证据所需的最小端口。

    基础设施实现会先在内存暂存 raw 与标准载荷；成功只 `discard`，失败才写入私有 manifest。
    应用层不能通过该端口读取历史字节或绕过留存授权。
    """

    def stage_batch(self, batch: ProviderBatch) -> None:
        """暂存一个已返回批次的摘要和受授权字节，以便后续失败留证。"""
        ...

    def stage_failure_summary(
        self,
        payload: bytes,
        content_type: str,
        *,
        capability: str | None = None,
    ) -> None:
        """暂存不含供应商原文或密钥的最小失败摘要。"""
        ...

    def persist_failure(self, error: Exception) -> str | None:
        """仅在失败时写入私有 evidence manifest，并返回内部定位 URI。"""
        ...

    def discard(self) -> None:
        """在成功或失败证据固化后清空本次内存暂存。"""
        ...


__all__ = [
    "StockConnectMarketStatFailureEvidenceStore",
    "StockConnectMarketStatResearchRecord",
    "StockConnectMarketStatResearchRepository",
    "StockConnectMarketStatResearchSourceObservation",
    "StoredStockConnectMarketStatResearchBatch",
]
