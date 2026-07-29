"""不生成 `canonical` 事实时使用的同步可用性应用端口。

它记录“来源无此数据”或“来源暂不可用”等精确请求结果，使调用方能区分合法空集、失败和尚未同步，而不是把它们都当成缺数据。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DatasetAvailability:
    """表示一个精确请求分区当前的空集或来源不可用结果。

    它不是一行全零业务数据。
    `availability` 表示结果类别，`reason_code` 说明原因，`observed_at` 记录该结论何时得到。
    """

    availability: str
    reason_code: str
    observed_at: datetime


class DatasetAvailabilityRepository(Protocol):
    """持久化诊断性可用性观察，绝不向业务事实表伪造空行。

    真实数据成功发布后，实现必须清除相同请求分区的旧观察，避免读取端把过期“空集”覆盖新事实。
    """

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
        """写入空集或来源不可用观察，并使同分区旧观察失效。"""
        ...

    def clear(self, *, dataset: str, partition_key: str, cleared_at: datetime) -> None:
        """在同一分区发布真实 `canonical` 事实后终结当前非事实观察。"""
        ...
