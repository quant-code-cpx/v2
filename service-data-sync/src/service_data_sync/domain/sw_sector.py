"""申万三级行业层级、估值与方法论血缘领域对象。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import IntEnum


class SwIndustryLevel(IntEnum):
    """限制申万行业层级为一级、二级或三级。"""

    LEVEL_1 = 1
    LEVEL_2 = 2
    LEVEL_3 = 3


@dataclass(frozen=True, slots=True)
class SwMethodology:
    """描述乐咕展示的申万分类与估值方法学身份。"""

    code: str
    version: int
    status: str
    upstream_source: str
    semantic_spec_sha256: str

    def __post_init__(self) -> None:
        """拒绝缺失或不可追溯的方法学元数据。"""
        if not self.code.strip() or len(self.code) > 80 or self.version <= 0:
            raise ValueError("SW methodology identity is invalid")
        if self.status != "source_reported":
            raise ValueError("SW methodology status must be source_reported")
        if not self.upstream_source.strip() or len(self.upstream_source) > 120:
            raise ValueError("SW upstream source must not be blank")
        if len(self.semantic_spec_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.semantic_spec_sha256
        ):
            raise ValueError("SW methodology semantic digest must be SHA-256")


@dataclass(frozen=True, slots=True)
class SwIndustryNode:
    """表示一次完整申万快照中的稳定代码、层级和直接父级。"""

    code: str
    name: str
    level: SwIndustryLevel
    parent_code: str | None
    component_count: int

    def __post_init__(self) -> None:
        """校验代码、名称、层级父级关系与成分数量。"""
        if len(self.code) != 9 or self.code[6:] != ".SI" or not self.code[:6].isdigit():
            raise ValueError("SW industry code must be six digits followed by .SI")
        if not self.name.strip() or self.name != self.name.strip() or len(self.name) > 200:
            raise ValueError("SW industry name is invalid")
        if self.component_count < 0:
            raise ValueError("SW industry component count must be non-negative")
        if self.level is SwIndustryLevel.LEVEL_1 and self.parent_code is not None:
            raise ValueError("SW level-one industry must not have a parent")
        if self.level is not SwIndustryLevel.LEVEL_1 and self.parent_code is None:
            raise ValueError("SW level-two and level-three industries require a parent")
        if self.parent_code == self.code:
            raise ValueError("SW industry cannot be its own parent")


@dataclass(frozen=True, slots=True)
class SwIndustryValuation:
    """保存乐咕页面对一个申万行业给出的日期估值观察。"""

    code: str
    snapshot_date: date
    static_pe: Decimal | None
    ttm_pe: Decimal | None
    pb: Decimal | None
    dividend_yield_ratio: Decimal | None

    def __post_init__(self) -> None:
        """确保估值仅含有限精确值，百分比已转为一比一比例。"""
        for value in (
            self.static_pe,
            self.ttm_pe,
            self.pb,
            self.dividend_yield_ratio,
        ):
            if value is not None and not value.is_finite():
                raise ValueError("SW valuation values must be finite")


@dataclass(frozen=True, slots=True)
class SwClosureEdge:
    """表示一次 taxonomy 发布中的祖先到后代闭包边。"""

    ancestor_code: str
    descendant_code: str
    depth: int

    def __post_init__(self) -> None:
        """拒绝负深度以及深度零但代码不相同的伪闭包边。"""
        if self.depth < 0:
            raise ValueError("SW closure depth must be non-negative")
        if self.depth == 0 and self.ancestor_code != self.descendant_code:
            raise ValueError("SW closure depth zero must be a self edge")


@dataclass(frozen=True, slots=True)
class SwIndustrySnapshot:
    """聚合一个观测日完整的三级 taxonomy、估值和方法论。"""

    snapshot_date: date
    nodes: tuple[SwIndustryNode, ...]
    valuations: tuple[SwIndustryValuation, ...]
    methodology: SwMethodology

    def __post_init__(self) -> None:
        """验证三级非空、代码唯一、父级闭合及估值覆盖完整。"""
        if not self.nodes:
            raise ValueError("SW snapshot must contain industries")
        nodes_by_code = {node.code: node for node in self.nodes}
        if len(nodes_by_code) != len(self.nodes):
            raise ValueError("SW snapshot contains duplicate industry codes")
        levels = {node.level for node in self.nodes}
        if levels != set(SwIndustryLevel):
            raise ValueError("SW snapshot must contain all three levels")
        for node in self.nodes:
            if node.parent_code is None:
                continue
            parent = nodes_by_code.get(node.parent_code)
            if parent is None or parent.level.value != node.level.value - 1:
                raise ValueError("SW snapshot contains an orphan or invalid parent")
        valuation_codes = {valuation.code for valuation in self.valuations}
        if len(valuation_codes) != len(self.valuations) or valuation_codes != set(nodes_by_code):
            raise ValueError("SW valuation coverage must exactly match taxonomy")
        if any(valuation.snapshot_date != self.snapshot_date for valuation in self.valuations):
            raise ValueError("SW valuations must use the snapshot date")

    def closure(self) -> tuple[SwClosureEdge, ...]:
        """计算包含自反边的三级父级闭包，并在检测到环时失败。"""
        nodes_by_code = {node.code: node for node in self.nodes}
        edges: list[SwClosureEdge] = []
        for node in self.nodes:
            edges.append(SwClosureEdge(node.code, node.code, 0))
            parent_code = node.parent_code
            depth = 1
            visited = {node.code}
            while parent_code is not None:
                if parent_code in visited:
                    raise ValueError("SW taxonomy contains a parent cycle")
                visited.add(parent_code)
                edges.append(SwClosureEdge(parent_code, node.code, depth))
                parent_code = nodes_by_code[parent_code].parent_code
                depth += 1
        return tuple(
            sorted(edges, key=lambda edge: (edge.descendant_code, edge.depth, edge.ancestor_code))
        )

    def taxonomy_sha256(self) -> str:
        """计算方法学、taxonomy、父级和成分数的稳定完整快照摘要。"""
        payload = {
            "methodology": _methodology_identity(self.methodology),
            "nodes": [
                {
                    "code": node.code,
                    "name": node.name,
                    "level": node.level.value,
                    "parentCode": node.parent_code,
                    "componentCount": node.component_count,
                }
                for node in sorted(self.nodes, key=lambda value: value.code)
            ],
        }
        return _stable_sha256(payload)

    def valuation_sha256(self) -> str:
        """计算方法学与同日全行业估值观察的稳定完整快照摘要。"""
        payload = {
            "methodology": _methodology_identity(self.methodology),
            "valuations": [
                {
                    "code": valuation.code,
                    "date": valuation.snapshot_date.isoformat(),
                    "staticPe": _decimal_text(valuation.static_pe),
                    "ttmPe": _decimal_text(valuation.ttm_pe),
                    "pb": _decimal_text(valuation.pb),
                    "dividendYieldRatio": _decimal_text(valuation.dividend_yield_ratio),
                }
                for valuation in sorted(self.valuations, key=lambda value: value.code)
            ],
        }
        return _stable_sha256(payload)


def _decimal_text(value: Decimal | None) -> str | None:
    """把精确小数稳定投影为摘要文本并保留空值。"""
    return None if value is None else str(value)


def _methodology_identity(value: SwMethodology) -> dict[str, object]:
    """把影响消费者语义的方法学身份纳入发布内容摘要。"""
    return {
        "code": value.code,
        "version": value.version,
        "status": value.status,
        "upstreamSource": value.upstream_source,
        "semanticSpecSha256": value.semantic_spec_sha256,
    }


def _stable_sha256(value: object) -> str:
    """以固定 JSON 编码生成可跨重跑比较的 SHA-256。"""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
