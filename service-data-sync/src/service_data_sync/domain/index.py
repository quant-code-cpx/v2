"""指数管理人、指数身份和来源观测快照的领域值对象。

中证与国证由管理人身份严格隔离；名称只是展示信息，不能替代稳定指数代码。
当前成分与权重只表示来源在观测时刻展示的内容，不在这里虚构历史生效区间或方法学结论。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class IndexAdministrator(StrEnum):
    """区分中证与国证两个不可静默混用的指数管理人。"""

    CSI = "CSI"
    CNI = "CNI"


class IndexCapability(StrEnum):
    """声明指数 Adapter 可以提供的 provider-neutral 原始能力。"""

    CATALOG_SNAPSHOT = "index.catalog.snapshot"
    CONSTITUENT_SNAPSHOT = "index.constituent.snapshot"
    WEIGHT_SNAPSHOT = "index.weight.snapshot"
    ADJUSTMENT_HISTORY = "index.adjustment.history"
    METHODOLOGY_DOCUMENT = "index.methodology.document"


@dataclass(frozen=True, slots=True)
class IndexIdentifier:
    """表示管理人范围内稳定的六码至八码大写字母数字指数代码，不以名称作为业务身份。"""

    administrator: IndexAdministrator
    code: str

    def __post_init__(self) -> None:
        """拒绝非六码至八码、大写以外或含非 ASCII 字符的来源指数代码。

        中证与国证真实目录同时包含 ``000300``、``H00999``、``AITCNYG`` 和 ``39926401``；
        大小写是来源身份的一部分，因此不能自动大写或接受小写值，以免请求和持久化键在不同
        边界发生静默合并。
        """
        if (
            not isinstance(self.code, str)
            or not 6 <= len(self.code) <= 8
            or not self.code.isascii()
            or not self.code.isalnum()
            or self.code != self.code.upper()
        ):
            raise ValueError(
                "index code must contain 6 to 8 uppercase ASCII alphanumeric characters"
            )

    @property
    def qualified_key(self) -> str:
        """生成日志、分区和来源请求使用的稳定复合身份。"""
        return f"{self.administrator.value}:{self.code}"


@dataclass(frozen=True, slots=True)
class IndexCatalogEntry:
    """表示目录快照中的最小指数身份信息，不推断生命周期或方法学。"""

    identifier: IndexIdentifier
    name: str

    def __post_init__(self) -> None:
        """确保未确认目录项至少包含可审计且非空的来源名称。"""
        if self.name != self.name.strip() or not self.name or len(self.name) > 200:
            raise ValueError("index name must be a trimmed string from 1 to 200 characters")


@dataclass(frozen=True, slots=True)
class IndexConstituentObservation:
    """表示当前来源快照内一条证券观察，不声明其真实生效区间。"""

    source_symbol: str
    source_name: str
    source_exchange: str | None

    def __post_init__(self) -> None:
        """限制来源身份形状，避免未解析记录绕过后续证券身份质量门。"""
        if len(self.source_symbol) != 6 or not self.source_symbol.isdigit():
            raise ValueError("constituent source symbol must be six digits")
        if self.source_name != self.source_name.strip() or not self.source_name:
            raise ValueError("constituent source name must not be blank")
        if self.source_exchange is not None and (
            self.source_exchange != self.source_exchange.strip() or not self.source_exchange
        ):
            raise ValueError("constituent source exchange must not be blank when provided")
