"""不生成 canonical 事实时使用的同步可用性应用端口。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DatasetAvailability:
    """表示一个精确请求分区当前的空集或来源不可用结果。"""

    availability: str
    reason_code: str
    observed_at: datetime


class DatasetAvailabilityRepository(Protocol):
    """持久化诊断性可用性观测，绝不向业务事实表伪造空行。"""

    def record(
        self,
        *,
        dataset: str,
        partition_key: str,
        availability: str,
        reason_code: str,
        provider_id: str | None,
        observed_at: datetime,
    ) -> DatasetAvailability:
        """写入空集或来源不可用观测，并使同分区旧观测失效。"""
        ...

    def clear(self, *, dataset: str, partition_key: str, cleared_at: datetime) -> None:
        """在同一分区发布真实 canonical 事实后终结当前非事实观测。"""
        ...
