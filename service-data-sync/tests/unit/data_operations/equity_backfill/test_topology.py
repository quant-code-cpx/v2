"""股票中心全量回填纯规划器的不可变拓扑契约测试。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import NAMESPACE_URL, uuid5

import pytest

from service_data_sync.infrastructure.data_operations.equity_backfill import (
    BackfillTopology,
    FrozenIdentity,
    FrozenReferenceBundle,
    FrozenSource,
    PlannedChild,
    build_topology,
    compute_roster_hash,
    compute_topology_seal,
    iter_topology_pages,
    source_contract_hash,
    validate_topology,
)
from service_data_sync.infrastructure.data_operations.legacy_submission import (
    system_command_group_identity,
)

_DATE_DATASETS = frozenset(
    {
        "equity.bar.1d.raw",
        "equity.bar.1w.raw",
        "equity.bar.1mo.raw",
        "equity.adjustment_factor",
    }
)
_CURRENT_REFRESH_DATASETS = frozenset(
    {
        "equity.profile",
        "equity.share_capital.reported",
        "financial.report",
        "financial.provider-metric",
        "financial.valuation",
        "financial.derived-metric",
    }
)
_EVENT_DATASETS = frozenset(
    {
        "equity.corporate_event.earnings.reported",
        "equity.dragon_tiger.disclosure.reported",
        "equity.block_trade.execution.reported",
    }
)
_PLANNED_DATASETS = frozenset(
    {
        *_DATE_DATASETS,
        "equity.corporate_action",
        *_EVENT_DATASETS,
        "equity.discovery.eod",
    }
)
_HISTORICAL_DATASETS = frozenset({*_DATE_DATASETS, "equity.corporate_action", *_EVENT_DATASETS})
_PLAN_ID = uuid5(NAMESPACE_URL, "quant-v2:test:equity-backfill-plan")
_KNOWN_AT = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)


def _sha256_json(value: object) -> str:
    """按生产规划器相同规范 JSON 算法生成测试 SHA-256。"""
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _reference_bundle(
    *,
    snapshot_observed_on: date,
    market_as_of: date,
) -> FrozenReferenceBundle:
    """构造覆盖全部当前态引用组件的精确、已封印测试 bundle。"""
    component_keys = (
        ("equity.master.cn-a", "CN_A_STABLE"),
        ("equity.lifecycle.explicit", "SSE"),
        ("equity.lifecycle.explicit", "SZSE"),
        ("equity.lifecycle.explicit", "BSE"),
        ("sector.catalog.raw", "eastmoney.industry"),
        ("sector.catalog.raw", "eastmoney.concept"),
        ("sector.membership.release", "eastmoney.industry"),
        ("sector.membership.release", "eastmoney.concept"),
        ("sector.sw.taxonomy", f"sw.industry:{snapshot_observed_on.isoformat()}"),
        ("sector.sw2021.membership.snapshot", "SW2021:801010"),
        ("sector.sw2021.membership.snapshot", "SW2021:801020"),
        ("equity.trading_status.1d", f"date:{market_as_of.isoformat()}"),
    )
    manifest = tuple(
        {
            "datasetCode": dataset_code,
            "partitionKey": partition_key,
            "publicationId": str(
                uuid5(
                    NAMESPACE_URL,
                    f"quant-v2:test:reference:publication:{dataset_code}:{partition_key}",
                )
            ),
            "dataVersion": str(
                uuid5(
                    NAMESPACE_URL,
                    f"quant-v2:test:reference:data-version:{dataset_code}:{partition_key}",
                )
            ),
            "releaseId": str(
                uuid5(
                    NAMESPACE_URL,
                    f"quant-v2:test:reference:release:{dataset_code}:{partition_key}",
                )
            ),
            "effectiveAsOf": (
                market_as_of if dataset_code == "equity.trading_status.1d" else snapshot_observed_on
            ).isoformat(),
            "observedOn": (
                market_as_of if dataset_code == "equity.trading_status.1d" else snapshot_observed_on
            ).isoformat(),
            "sourceBatchIds": [
                str(
                    uuid5(
                        NAMESPACE_URL,
                        f"quant-v2:test:reference:source:{dataset_code}:{partition_key}",
                    )
                )
            ],
            "sourceContractHash": _sha256_json(
                {"datasetCode": dataset_code, "partitionKey": partition_key}
            ),
        }
        for dataset_code, partition_key in component_keys
    )
    return FrozenReferenceBundle(
        publication_id=uuid5(NAMESPACE_URL, "quant-v2:test:reference:bundle:publication"),
        data_version=uuid5(NAMESPACE_URL, "quant-v2:test:reference:bundle:data-version"),
        release_id=uuid5(NAMESPACE_URL, "quant-v2:test:reference:bundle:release"),
        snapshot_observed_on=snapshot_observed_on,
        market_as_of=market_as_of,
        manifest=manifest,
        manifest_hash=_sha256_json(list(manifest)),
    )


def _source(dataset_code: str, earliest_date: date | None) -> FrozenSource:
    """构造摘要自洽、字段齐全且不触达真实供应商的冻结来源合同。"""
    snapshot = (
        {
            "providerId": "fixture-provider",
            "capability": dataset_code,
            "upstreamSource": "fixture-upstream",
        },
    )
    draft = FrozenSource(
        dataset_code=dataset_code,
        publication_dataset_code=dataset_code,
        source_snapshot=snapshot,
        source_snapshot_hash=_sha256_json(list(snapshot)),
        earliest_date=earliest_date,
        earliest_date_method="FIXTURE_VERIFIED_BOUNDARY",
        evidence_ref=f"fixture://equity-backfill/{dataset_code}",
        evidence_sha256=_sha256_json(
            {
                "datasetCode": dataset_code,
                "earliest": None if earliest_date is None else earliest_date.isoformat(),
            }
        ),
        evidence_observed_at=_KNOWN_AT,
        expected_provider_id="fixture-provider",
        expected_capability=dataset_code,
        expected_upstream_source="fixture-upstream",
        expected_adapter_version="fixture-adapter-v1",
        expected_schema_fingerprint=_sha256_json({"schema": dataset_code}),
        supported_exchanges=("BSE", "SSE", "SZSE"),
        methodology_code=f"fixture.{dataset_code}",
        methodology_version=1,
        mapping_version="fixture-mapping-v1",
        source_contract_hash="0" * 64,
    )
    return replace(draft, source_contract_hash=source_contract_hash(draft))


def _sources(
    *,
    default_earliest: date,
    earliest_overrides: dict[str, date] | None = None,
) -> dict[str, FrozenSource]:
    """构造规划器要求的完整来源清单，并允许逐数据集覆盖历史起点。"""
    overrides = earliest_overrides or {}
    return {
        dataset_code: _source(
            dataset_code,
            (
                overrides.get(dataset_code, default_earliest)
                if dataset_code in _HISTORICAL_DATASETS
                else None
            ),
        )
        for dataset_code in _PLANNED_DATASETS
    }


def _identity(
    *,
    label: str,
    ordinal: int,
    security_id: int,
    effective_from: date,
    effective_to: date | None,
    exchange: str = "SSE",
    symbol: str = "600519",
) -> FrozenIdentity:
    """构造稳定 UUID 的冻结证券身份，支持同代码跨版本复用场景。"""
    return FrozenIdentity(
        ordinal=ordinal,
        identifier_version_id=uuid5(NAMESPACE_URL, f"quant-v2:test:identifier:{label}"),
        security_id=security_id,
        instrument_id=uuid5(NAMESPACE_URL, f"quant-v2:test:instrument:{label}"),
        exchange=exchange,
        symbol=symbol,
        effective_from=effective_from,
        effective_to=effective_to,
        known_from=datetime(2020, 1, 1, tzinfo=UTC),
        known_to=None,
        effective_date_precision="DAY",
    )


def _build(
    *,
    as_of: date,
    identities: tuple[FrozenIdentity, ...],
    sources: dict[str, FrozenSource],
) -> BackfillTopology:
    """使用固定计划身份生成可重复比较的纯内存拓扑。"""
    return build_topology(
        plan_id=_PLAN_ID,
        snapshot_observed_on=as_of,
        market_as_of=as_of,
        known_at=_KNOWN_AT,
        roster_hash=compute_roster_hash(identities),
        identities=identities,
        sources=sources,
        reference_bundle=_reference_bundle(
            snapshot_observed_on=as_of,
            market_as_of=as_of,
        ),
    )


def _dataset_codes(child: PlannedChild) -> tuple[str, ...]:
    """按目标顺序提取一个 command 的数据集代码。"""
    return tuple(str(target["datasetCode"]) for target in child.targets)


def _date_span(child: PlannedChild) -> int:
    """返回 child 内部冻结的包含首尾日期范围长度。"""
    assert child.window_from is not None
    assert child.window_to is not None
    first = child.window_from
    last = child.window_to
    return (last - first).days + 1


def _replace_child(
    topology: BackfillTopology,
    original: PlannedChild,
    replacement: PlannedChild,
) -> BackfillTopology:
    """只替换一个 child，便于验证拓扑审计的失败分支。"""
    return replace(
        topology,
        children=tuple(
            replacement if child.child_key == original.child_key else child
            for child in topology.children
        ),
    )


def test_targets_intents_sources_and_submission_identity_align_exactly() -> None:
    """每个目标必须与意图、来源摘要及稳定 submission 身份逐项对齐。"""
    as_of = date(2024, 3, 15)
    sources = _sources(default_earliest=date(2024, 1, 1))
    identity = _identity(
        label="active",
        ordinal=1,
        security_id=1,
        effective_from=date(2023, 1, 1),
        effective_to=None,
    )

    topology = _build(as_of=as_of, identities=(identity,), sources=sources)

    for child in topology.children:
        dataset_codes = _dataset_codes(child)
        assert len(dataset_codes) == len(set(dataset_codes))
        assert len(child.targets) == len(child.intents) == len(child.source_hashes)
        assert set(dataset_codes) == set(child.source_hashes)
        for index, (target, intent) in enumerate(zip(child.targets, child.intents, strict=True)):
            dataset_code = str(target["datasetCode"])
            assert intent["targetIndex"] == index
            assert intent["childKey"] == child.child_key
            assert intent["sourceSnapshotHash"] == sources[dataset_code].source_snapshot_hash
            assert intent["sourceContractHash"] == sources[dataset_code].source_contract_hash
            assert intent["sourceSupportedExchanges"] == list(
                sources[dataset_code].supported_exchanges
            )
            expected_semantics = (
                "DERIVED_FROM_EXACT_INPUTS"
                if dataset_code == "equity.discovery.eod"
                else "FROZEN_PLAN_BOUNDARY"
            )
            assert intent["observationSemantics"] == expected_semantics
            assert child.source_hashes[dataset_code] == sources[dataset_code].source_snapshot_hash
        _fingerprint, expected_submission_id = system_command_group_identity(
            targets=list(child.targets),
            intents=list(child.intents),
            request_prefix=child.request_prefix,
        )
        assert child.submission_id == expected_submission_id


def test_current_only_targets_are_explicitly_separate_from_historical_plan() -> None:
    """当前快照不能混入长历史计划，必须记录为独立刷新命令族。"""
    as_of = date(2024, 3, 15)
    sources = _sources(default_earliest=date(2024, 1, 1))
    identity = _identity(
        label="current-group",
        ordinal=1,
        security_id=1,
        effective_from=date(2024, 1, 1),
        effective_to=None,
    )

    topology = _build(as_of=as_of, identities=(identity,), sources=sources)
    planned_codes = {
        dataset_code for child in topology.children for dataset_code in _dataset_codes(child)
    }
    exclusions = {
        str(item["datasetCode"]): item
        for item in topology.exclusions
        if item["datasetCode"] in _CURRENT_REFRESH_DATASETS
    }

    assert planned_codes.isdisjoint(_CURRENT_REFRESH_DATASETS)
    assert set(exclusions) == _CURRENT_REFRESH_DATASETS
    assert all(
        item["reasonCode"] == "CURRENT_SOURCE_SEPARATE_REFRESH" for item in exclusions.values()
    )


def test_reference_bundle_rejects_mixed_dates_and_duplicate_partitions() -> None:
    """引用 bundle 必须逐组件同日且分区精确，计数相同的重复项也不能通过。"""
    snapshot = date(2024, 3, 16)
    market = date(2024, 3, 15)
    bundle = _reference_bundle(
        snapshot_observed_on=snapshot,
        market_as_of=market,
    )
    mixed_manifest = tuple(dict(component) for component in bundle.manifest)
    mixed_manifest[0]["observedOn"] = market.isoformat()
    with pytest.raises(ValueError, match="date is mixed"):
        replace(
            bundle,
            manifest=mixed_manifest,
            manifest_hash=_sha256_json(list(mixed_manifest)),
        ).validate()

    duplicate_manifest = tuple(dict(component) for component in bundle.manifest)
    duplicate_manifest[5]["partitionKey"] = "eastmoney.industry"
    with pytest.raises(ValueError, match="duplicate component"):
        replace(
            bundle,
            manifest=duplicate_manifest,
            manifest_hash=_sha256_json(list(duplicate_manifest)),
        ).validate()


def test_reference_bundle_rejects_legacy_null_component_release() -> None:
    """旧 publication 即使有 dataVersion，缺少 canonical release 也不能封存为恢复输入。"""
    snapshot = date(2024, 3, 16)
    market = date(2024, 3, 15)
    bundle = _reference_bundle(
        snapshot_observed_on=snapshot,
        market_as_of=market,
    )
    legacy_manifest = tuple(dict(component) for component in bundle.manifest)
    legacy_manifest[0]["releaseId"] = None

    with pytest.raises(ValueError, match="component identity is invalid"):
        replace(
            bundle,
            manifest=legacy_manifest,
            manifest_hash=_sha256_json(list(legacy_manifest)),
        ).validate()


def test_dataset_specific_source_boundaries_clip_market_and_event_targets() -> None:
    """行情和事件目标分别服从各自来源起点，早期事件窗口只包含合法子集。"""
    as_of = date(2024, 3, 15)
    earliest = {
        "equity.bar.1d.raw": date(2024, 1, 1),
        "equity.bar.1w.raw": date(2024, 2, 1),
        "equity.bar.1mo.raw": date(2024, 1, 15),
        "equity.adjustment_factor": date(2023, 12, 15),
        "equity.corporate_event.earnings.reported": date(2024, 1, 1),
        "equity.dragon_tiger.disclosure.reported": date(2024, 1, 20),
        "equity.block_trade.execution.reported": date(2024, 2, 10),
    }
    sources = _sources(
        default_earliest=date(2023, 12, 1),
        earliest_overrides=earliest,
    )
    identity = _identity(
        label="clipped",
        ordinal=1,
        security_id=1,
        effective_from=date(2023, 12, 1),
        effective_to=None,
    )

    topology = _build(as_of=as_of, identities=(identity,), sources=sources)
    market_children = {
        str(child.targets[0]["datasetCode"]): child
        for child in topology.children
        if child.identity_ordinal == identity.ordinal
        and len(child.targets) == 1
        and child.targets[0]["datasetCode"] in _DATE_DATASETS
    }
    for dataset_code in _DATE_DATASETS:
        child = market_children[dataset_code]
        assert child.targets[0]["mode"] == "FULL"
        assert child.window_from == earliest[dataset_code]
        assert child.window_to == as_of
        assert child.intents[0]["backfillDateFrom"] == earliest[dataset_code].isoformat()
        assert child.intents[0]["backfillDateTo"] == as_of.isoformat()

    event_children = {
        str(child.targets[0]["datasetCode"]): child
        for child in topology.children
        if child.phase == "GLOBAL_EVENT"
    }
    assert set(event_children) == _EVENT_DATASETS
    for dataset_code, child in event_children.items():
        assert child.targets[0]["mode"] == "FULL"
        assert child.window_from == earliest[dataset_code]
        assert child.window_to == as_of


def test_closed_identity_keeps_only_legal_historical_children() -> None:
    """关闭身份仍保留合法历史 child，当前来源不属于历史计划。"""
    as_of = date(2024, 3, 15)
    sources = _sources(default_earliest=date(2020, 1, 1))
    identity = _identity(
        label="closed",
        ordinal=1,
        security_id=1,
        effective_from=date(2020, 1, 1),
        effective_to=date(2022, 1, 1),
    )

    topology = _build(as_of=as_of, identities=(identity,), sources=sources)
    identity_targets = {
        dataset_code
        for child in topology.children
        if child.identity_ordinal == identity.ordinal
        for dataset_code in _dataset_codes(child)
    }
    assert identity_targets == {
        "equity.adjustment_factor",
        "equity.bar.1d.raw",
        "equity.bar.1mo.raw",
        "equity.bar.1w.raw",
        "equity.corporate_action",
    }


def test_code_reuse_keeps_both_versions_and_clips_each_effective_interval() -> None:
    """同交易所代码复用时两个身份版本都生成，历史窗口不得跨越版本边界。"""
    as_of = date(2024, 3, 15)
    sources = _sources(default_earliest=date(2023, 1, 1))
    old_identity = _identity(
        label="reuse-old",
        ordinal=1,
        security_id=11,
        effective_from=date(2023, 1, 1),
        effective_to=date(2023, 7, 1),
    )
    new_identity = _identity(
        label="reuse-new",
        ordinal=2,
        security_id=22,
        effective_from=date(2023, 7, 1),
        effective_to=None,
    )

    topology = _build(
        as_of=as_of,
        identities=(old_identity, new_identity),
        sources=sources,
    )
    historical_by_identity: dict[int, list[PlannedChild]] = {
        old_identity.ordinal: [],
        new_identity.ordinal: [],
    }
    for child in topology.children:
        if child.identity_ordinal in historical_by_identity and any(
            dataset_code in _DATE_DATASETS for dataset_code in _dataset_codes(child)
        ):
            historical_by_identity[child.identity_ordinal].append(child)

    assert all(historical_by_identity.values())
    assert max(
        child.window_to or date.min for child in historical_by_identity[old_identity.ordinal]
    ) == date(2023, 6, 30)
    assert min(
        child.window_from or date.max for child in historical_by_identity[new_identity.ordinal]
    ) == date(2023, 7, 1)
    frozen_versions = {
        str(intent["identity"]["identifierVersionId"])
        for child in (
            *historical_by_identity[old_identity.ordinal],
            *historical_by_identity[new_identity.ordinal],
        )
        for intent in child.intents
    }
    assert frozen_versions == {
        str(old_identity.identifier_version_id),
        str(new_identity.identifier_version_id),
    }


def test_money_flow_is_one_fixed_exclusion_and_never_a_child() -> None:
    """未冻结供应商方法学的资金流只能出现一次固定排除，不能伪造执行成功。"""
    as_of = date(2024, 3, 15)
    sources = _sources(default_earliest=date(2024, 1, 1))
    topology = _build(as_of=as_of, identities=(), sources=sources)

    assert all("money_flow.daily" not in _dataset_codes(child) for child in topology.children)
    exclusions = [item for item in topology.exclusions if item["datasetCode"] == "money_flow.daily"]
    assert exclusions == [
        {
            "datasetCode": "money_flow.daily",
            "reasonCode": "UNSUPPORTED_PROVIDER_METHODOLOGY",
            "identifierVersionId": None,
            "exchange": None,
            "symbol": None,
            "detail": "当前资金流来源和供应商方法学未获准用于股票中心，不生成 child 或成功状态。",
        }
    ]


def test_dependency_audit_requires_existing_acyclic_dag() -> None:
    """依赖必须存在于同计划且保持 DAG；缺失边和同阶段环都应立即失败。"""
    as_of = date(2024, 3, 15)
    sources = _sources(default_earliest=date(2024, 1, 1))
    topology = _build(as_of=as_of, identities=(), sources=sources)

    validate_topology(topology, sources)
    known_keys = {child.child_key for child in topology.children}
    assert all(
        dependency_key in known_keys
        for child in topology.children
        for dependency_key in child.dependency_keys
    )

    discovery = next(child for child in topology.children if child.phase == "DISCOVERY_BUILD")
    missing_dependency = replace(discovery, dependency_keys=("missing-child-key",))
    with pytest.raises(ValueError, match="dependency does not exist"):
        validate_topology(
            _replace_child(topology, discovery, missing_dependency),
            sources,
        )

    event_children = [child for child in topology.children if child.phase == "GLOBAL_EVENT"][:2]
    first, second = event_children
    cyclic_first = replace(first, dependency_keys=(second.child_key,))
    cyclic_second = replace(second, dependency_keys=(first.child_key,))
    cyclic = _replace_child(
        _replace_child(topology, first, cyclic_first),
        second,
        cyclic_second,
    )
    with pytest.raises(ValueError, match="contains a cycle"):
        validate_topology(cyclic, sources)


def test_topology_audit_rejects_duplicate_dataset_in_one_command() -> None:
    """即使长度仍对齐，同一 command 的重复数据集也必须被拓扑审计拒绝。"""
    as_of = date(2024, 3, 15)
    sources = _sources(default_earliest=date(2024, 1, 1))
    identity = _identity(
        label="duplicate-target",
        ordinal=1,
        security_id=1,
        effective_from=date(2020, 1, 1),
        effective_to=None,
    )
    topology = _build(as_of=as_of, identities=(identity,), sources=sources)
    multi_target_child = next(
        child for child in topology.children if child.identity_ordinal == identity.ordinal
    )
    duplicated_target = dict(multi_target_child.targets[0])
    duplicated_intent = {
        **multi_target_child.intents[0],
        "targetIndex": 1,
    }
    invalid_child = replace(
        multi_target_child,
        targets=(
            multi_target_child.targets[0],
            duplicated_target,
        ),
        intents=(multi_target_child.intents[0], duplicated_intent),
        source_hashes={
            **multi_target_child.source_hashes,
            "invalid.duplicate.sentinel": "0" * 64,
        },
    )

    with pytest.raises(ValueError, match="dataset codes must be unique"):
        validate_topology(
            _replace_child(topology, multi_target_child, invalid_child),
            sources,
        )


def test_historical_ranges_use_one_low_cardinality_child_per_dataset() -> None:
    """长历史只冻结一个 child 范围，由 executor 内部按 366/1098/31 日 checkpoint。"""
    as_of = date(2025, 12, 31)
    sources = _sources(default_earliest=date(2020, 1, 1))
    identity = _identity(
        label="long-history",
        ordinal=1,
        security_id=1,
        effective_from=date(2020, 1, 1),
        effective_to=None,
    )

    topology = _build(as_of=as_of, identities=(identity,), sources=sources)
    historical_children = [
        child
        for child in topology.children
        if any(
            dataset_code in {*_DATE_DATASETS, "equity.corporate_action", *_EVENT_DATASETS}
            for dataset_code in _dataset_codes(child)
        )
    ]
    by_dataset: dict[str, list[PlannedChild]] = {}
    for child in historical_children:
        assert len(child.targets) == 1
        by_dataset.setdefault(str(child.targets[0]["datasetCode"]), []).append(child)
        assert _date_span(child) > 31
    assert len(by_dataset["equity.bar.1d.raw"]) == 1
    assert len(by_dataset["equity.corporate_action"]) == 1
    assert all(len(by_dataset[dataset_code]) == 1 for dataset_code in _EVENT_DATASETS)


def test_dual_plan_dates_route_current_and_market_families_independently() -> None:
    """周末当前快照保留周六，交易状态、发现与交易事件停在周五。"""
    snapshot_observed_on = date(2024, 3, 16)
    market_as_of = date(2024, 3, 15)
    sources = _sources(default_earliest=date(2024, 1, 1))
    topology = build_topology(
        plan_id=_PLAN_ID,
        snapshot_observed_on=snapshot_observed_on,
        market_as_of=market_as_of,
        known_at=_KNOWN_AT,
        roster_hash=compute_roster_hash(()),
        identities=(),
        sources=sources,
        reference_bundle=_reference_bundle(
            snapshot_observed_on=snapshot_observed_on,
            market_as_of=market_as_of,
        ),
    )
    targets = {
        str(target["datasetCode"]): target
        for child in topology.children
        for target in child.targets
    }
    assert targets["equity.discovery.eod"]["observationDate"] == "2024-03-15"
    assert "sector.sw.taxonomy" not in targets
    assert "equity.trading_status.1d" not in targets
    assert topology.reference_bundle.snapshot_observed_on == snapshot_observed_on
    assert topology.reference_bundle.market_as_of == market_as_of
    assert all(
        intent["referenceBundleDataVersion"] == str(topology.reference_bundle.data_version)
        and intent["referenceManifestHash"] == topology.reference_bundle.manifest_hash
        for child in topology.children
        for intent in child.intents
    )
    events = {
        str(child.targets[0]["datasetCode"]): child
        for child in topology.children
        if child.phase == "GLOBAL_EVENT"
    }
    assert events["equity.corporate_event.earnings.reported"].window_to == snapshot_observed_on
    assert events["equity.dragon_tiger.disclosure.reported"].window_to == market_as_of
    assert events["equity.block_trade.execution.reported"].window_to == market_as_of


def test_topology_pages_and_seal_respect_transaction_budgets() -> None:
    """分页最多一千 child、八 MiB，并由连续 page roster 形成最终 seal。"""
    as_of = date(2024, 3, 15)
    sources = _sources(default_earliest=date(2024, 1, 1))
    identities = tuple(
        _identity(
            label=f"page-{ordinal}",
            ordinal=ordinal,
            security_id=ordinal,
            effective_from=date(2024, 1, 1),
            effective_to=None,
            symbol=f"{ordinal:06d}",
        )
        for ordinal in range(1, 251)
    )
    topology = _build(as_of=as_of, identities=identities, sources=sources)
    pages = tuple(iter_topology_pages(topology))
    seal = compute_topology_seal(topology)

    assert pages
    assert all(len(page.children) <= 1000 for page in pages)
    assert all(page.payload_bytes <= 8 * 1024 * 1024 for page in pages)
    assert [page.page_number for page in pages] == list(range(1, len(pages) + 1))
    assert sum(len(page.children) for page in pages) == len(topology.children)
    assert seal.page_count == len(pages)
    assert seal.child_count == len(topology.children)
    assert len(seal.topology_hash) == len(seal.page_roster_hash) == 64


def test_unproven_exchange_is_explicitly_excluded_without_market_child() -> None:
    """未通过 BSE 实时探测的数据集必须排除，不得静默改用其他交易所来源。"""
    as_of = date(2024, 3, 15)
    sources = _sources(default_earliest=date(2024, 1, 1))
    daily_source = replace(
        sources["equity.bar.1d.raw"],
        supported_exchanges=("SSE", "SZSE"),
    )
    sources["equity.bar.1d.raw"] = replace(
        daily_source,
        source_contract_hash=source_contract_hash(daily_source),
    )
    identity = _identity(
        label="bse-unproven",
        ordinal=1,
        security_id=1,
        effective_from=date(2024, 1, 1),
        effective_to=None,
        exchange="BSE",
        symbol="835185",
    )

    topology = _build(as_of=as_of, identities=(identity,), sources=sources)

    assert all(
        "equity.bar.1d.raw" not in _dataset_codes(child)
        for child in topology.children
        if child.identity_ordinal == identity.ordinal
    )
    assert any(
        item["datasetCode"] == "equity.bar.1d.raw"
        and item["reasonCode"] == "SOURCE_EXCHANGE_UNAVAILABLE"
        and item["exchange"] == "BSE"
        for item in topology.exclusions
    )
