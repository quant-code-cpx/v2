"""申万领域对象边界、完整覆盖与闭包防御性校验测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from service_data_sync.domain.sw_sector import (
    SwClosureEdge,
    SwIndustryLevel,
    SwIndustryNode,
    SwIndustrySnapshot,
    SwIndustryValuation,
    SwMethodology,
)

_DATE = date(2026, 7, 28)


def test_methodology_rejects_untraceable_identity_and_semantics() -> None:
    """方法学必须具备非空身份、固定状态、来源和合法 SHA-256。"""
    valid = _methodology()

    with pytest.raises(ValueError, match="identity"):
        replace(valid, code=" ")
    with pytest.raises(ValueError, match="status"):
        replace(valid, status="validated")
    with pytest.raises(ValueError, match="source"):
        replace(valid, upstream_source="")
    with pytest.raises(ValueError, match="SHA-256"):
        replace(valid, semantic_spec_sha256="G" * 64)


def test_industry_node_rejects_invalid_identity_parent_and_count() -> None:
    """行业节点应拒绝错误代码、空白名称、负成分数和非法父级关系。"""
    valid = _nodes()[0]

    with pytest.raises(ValueError, match="code"):
        replace(valid, code="801010")
    with pytest.raises(ValueError, match="name"):
        replace(valid, name=" 农林牧渔")
    with pytest.raises(ValueError, match="non-negative"):
        replace(valid, component_count=-1)
    with pytest.raises(ValueError, match="must not have"):
        replace(valid, parent_code="801016.SI")
    with pytest.raises(ValueError, match="require a parent"):
        replace(_nodes()[1], parent_code=None)
    with pytest.raises(ValueError, match="own parent"):
        replace(_nodes()[1], parent_code="801016.SI")


def test_valuation_and_closure_edge_reject_non_finite_or_impossible_values() -> None:
    """估值只接受有限小数，闭包边不得出现负深度或伪自反边。"""
    valuation = _valuations()[0]

    with pytest.raises(ValueError, match="finite"):
        replace(valuation, static_pe=Decimal("NaN"))
    with pytest.raises(ValueError, match="non-negative"):
        SwClosureEdge("801010.SI", "801016.SI", -1)
    with pytest.raises(ValueError, match="self edge"):
        SwClosureEdge("801010.SI", "801016.SI", 0)


def test_snapshot_rejects_incomplete_duplicate_or_orphan_taxonomy() -> None:
    """完整快照必须三级非空、代码唯一，且每个子级指向直接上一级。"""
    nodes = _nodes()
    valuations = _valuations()

    with pytest.raises(ValueError, match="contain industries"):
        SwIndustrySnapshot(_DATE, (), (), _methodology())
    with pytest.raises(ValueError, match="duplicate"):
        SwIndustrySnapshot(_DATE, (*nodes, nodes[0]), valuations, _methodology())
    with pytest.raises(ValueError, match="all three levels"):
        SwIndustrySnapshot(_DATE, (nodes[0],), (valuations[0],), _methodology())
    with pytest.raises(ValueError, match="orphan"):
        SwIndustrySnapshot(
            _DATE,
            (nodes[0], replace(nodes[1], parent_code="999999.SI"), nodes[2]),
            valuations,
            _methodology(),
        )


def test_snapshot_rejects_incomplete_or_wrong_date_valuation_coverage() -> None:
    """估值代码应与 taxonomy 一一对应，且所有观察日期等于快照日期。"""
    nodes = _nodes()
    valuations = _valuations()

    with pytest.raises(ValueError, match="exactly match"):
        SwIndustrySnapshot(_DATE, nodes, valuations[:-1], _methodology())
    with pytest.raises(ValueError, match="snapshot date"):
        SwIndustrySnapshot(
            _DATE,
            nodes,
            (*valuations[:-1], replace(valuations[-1], snapshot_date=date(2026, 7, 27))),
            _methodology(),
        )


def test_closure_detects_cycle_even_if_corrupted_object_bypasses_constructor() -> None:
    """闭包遍历应对反序列化或内存破坏造成的父级环保持防御。"""
    snapshot = _snapshot()
    root = snapshot.nodes[0]
    object.__setattr__(root, "parent_code", snapshot.nodes[-1].code)

    with pytest.raises(ValueError, match="cycle"):
        snapshot.closure()


def _methodology() -> SwMethodology:
    """构造可追溯的测试方法学。"""
    return SwMethodology(
        code="test-sw",
        version=1,
        status="source_reported",
        upstream_source="test.sw",
        semantic_spec_sha256="a" * 64,
    )


def _nodes() -> tuple[SwIndustryNode, ...]:
    """构造三级直接父子链。"""
    return (
        SwIndustryNode(
            code="801010.SI",
            name="农林牧渔",
            level=SwIndustryLevel.LEVEL_1,
            parent_code=None,
            component_count=8,
        ),
        SwIndustryNode(
            code="801016.SI",
            name="种植业",
            level=SwIndustryLevel.LEVEL_2,
            parent_code="801010.SI",
            component_count=8,
        ),
        SwIndustryNode(
            code="850111.SI",
            name="种子",
            level=SwIndustryLevel.LEVEL_3,
            parent_code="801016.SI",
            component_count=8,
        ),
    )


def _valuations() -> tuple[SwIndustryValuation, ...]:
    """为三级代码构造同日有限估值。"""
    return tuple(
        SwIndustryValuation(
            code=node.code,
            snapshot_date=_DATE,
            static_pe=Decimal("10"),
            ttm_pe=Decimal("11"),
            pb=Decimal("2"),
            dividend_yield_ratio=Decimal("0.01"),
        )
        for node in _nodes()
    )


def _snapshot() -> SwIndustrySnapshot:
    """构造通过聚合校验的完整快照。"""
    return SwIndustrySnapshot(
        snapshot_date=_DATE,
        nodes=_nodes(),
        valuations=_valuations(),
        methodology=_methodology(),
    )
