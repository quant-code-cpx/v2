"""可重复测量 5,500 条冻结身份全历史回填拓扑的时间、内存与存储量级。

本文件只生成确定性的压力输入并调用纯规划器，不访问 Provider、数据库或业务 publication。
它不是日常单元测试，避免把大内存基准混入默认 `pytest`；应在测试镜像内作为脚本显式运行。
"""

from __future__ import annotations

import gc
import hashlib
import json
import math
import resource
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

from service_data_sync.infrastructure.data_operations.equity_backfill import (
    BackfillTopology,
    FrozenIdentity,
    FrozenReferenceBundle,
    FrozenSource,
    PlannedChild,
    build_topology,
    compute_roster_hash,
    source_contract_hash,
)

IDENTITY_COUNT = 5_500
HISTORY_FROM = date(1990, 12, 19)
SNAPSHOT_OBSERVED_ON = date(2026, 7, 29)
MARKET_AS_OF = date(2026, 7, 29)
KNOWN_AT = datetime(2026, 7, 29, 12, tzinfo=UTC)
PLAN_ID = uuid5(NAMESPACE_URL, "quant-v2:benchmark:equity-backfill:5500:full-history:v1")

_DATE_DATASETS = frozenset(
    {
        "equity.bar.1d.raw",
        "equity.bar.1w.raw",
        "equity.bar.1mo.raw",
        "equity.adjustment_factor",
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
_INTERNAL_DATASETS = frozenset({"equity.discovery.eod"})
_MIB = 1024 * 1024
_PRE_CHECKPOINT_BASELINE = {
    "commandCount": 280_928,
    "partitionWorkCount": 881_268,
    "plannerPeakRssMiB": 2_073.59,
    "planStorageMiB": 1_218.11,
    "topologyWallSeconds": 22.600825,
    "topologySha256": "0f6fa64032ce9331ce0f11d8d116e4888c5dde3fea1f9162e6593c9ea795d919",
}


def _canonical_json(value: object) -> bytes:
    """按规划器相同排序规则生成 UTF-8 规范 JSON。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _sha256_json(value: object) -> str:
    """计算规范 JSON 的小写 SHA-256。"""
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _reference_bundle() -> FrozenReferenceBundle:
    """构造全量基准所需的精确当前态引用 bundle，不访问任何真实来源。"""
    component_keys = (
        ("equity.master.cn-a", "CN_A_STABLE"),
        ("equity.lifecycle.explicit", "SSE"),
        ("equity.lifecycle.explicit", "SZSE"),
        ("equity.lifecycle.explicit", "BSE"),
        ("sector.catalog.raw", "eastmoney.industry"),
        ("sector.catalog.raw", "eastmoney.concept"),
        ("sector.membership.release", "eastmoney.industry"),
        ("sector.membership.release", "eastmoney.concept"),
        (
            "sector.sw.taxonomy",
            f"sw.industry:{SNAPSHOT_OBSERVED_ON.isoformat()}",
        ),
        ("sector.sw2021.membership.snapshot", "SW2021:801010"),
        ("equity.trading_status.1d", f"date:{MARKET_AS_OF.isoformat()}"),
    )
    manifest = tuple(
        {
            "datasetCode": dataset_code,
            "partitionKey": partition_key,
            "publicationId": str(
                uuid5(
                    NAMESPACE_URL,
                    f"quant-v2:benchmark:reference:publication:{dataset_code}:{partition_key}",
                )
            ),
            "dataVersion": str(
                uuid5(
                    NAMESPACE_URL,
                    f"quant-v2:benchmark:reference:data-version:{dataset_code}:{partition_key}",
                )
            ),
            "releaseId": str(
                uuid5(
                    NAMESPACE_URL,
                    f"quant-v2:benchmark:reference:release:{dataset_code}:{partition_key}",
                )
            ),
            "effectiveAsOf": (
                MARKET_AS_OF if dataset_code == "equity.trading_status.1d" else SNAPSHOT_OBSERVED_ON
            ).isoformat(),
            "observedOn": (
                MARKET_AS_OF if dataset_code == "equity.trading_status.1d" else SNAPSHOT_OBSERVED_ON
            ).isoformat(),
            "sourceBatchIds": [
                str(
                    uuid5(
                        NAMESPACE_URL,
                        f"quant-v2:benchmark:reference:source:{dataset_code}:{partition_key}",
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
        publication_id=uuid5(NAMESPACE_URL, "quant-v2:benchmark:reference:bundle:publication"),
        data_version=uuid5(NAMESPACE_URL, "quant-v2:benchmark:reference:bundle:data-version"),
        release_id=uuid5(NAMESPACE_URL, "quant-v2:benchmark:reference:bundle:release"),
        snapshot_observed_on=SNAPSHOT_OBSERVED_ON,
        market_as_of=MARKET_AS_OF,
        manifest=manifest,
        manifest_hash=_sha256_json(list(manifest)),
    )


def _emit(event: str, payload: dict[str, object]) -> None:
    """立即输出一条单行 JSON，便于 OOM 时保留最后完成阶段。"""
    print(
        json.dumps(
            {"event": event, **payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )


def _rss_bytes() -> int:
    """返回 Linux 测试容器内进程生命周期峰值 RSS 字节数。"""
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024


def _identity_coordinates(ordinal: int) -> tuple[str, str]:
    """把稳定序号映射为三所内不重复的六位压力测试代码。"""
    if ordinal <= 2_300:
        return "SSE", f"{600_000 + ordinal:06d}"
    if ordinal <= 5_200:
        return "SZSE", f"{ordinal - 2_300:06d}"
    return "BSE", f"{830_000 + ordinal - 5_200:06d}"


def _identities() -> tuple[FrozenIdentity, ...]:
    """构造 5,500 条从 A 股最早历史边界持续有效的确定性身份。"""
    identities: list[FrozenIdentity] = []
    for ordinal in range(1, IDENTITY_COUNT + 1):
        exchange, symbol = _identity_coordinates(ordinal)
        label = f"{exchange}:{symbol}:{ordinal}"
        identities.append(
            FrozenIdentity(
                ordinal=ordinal,
                identifier_version_id=uuid5(
                    NAMESPACE_URL, f"quant-v2:benchmark:identifier:{label}"
                ),
                security_id=ordinal,
                instrument_id=uuid5(NAMESPACE_URL, f"quant-v2:benchmark:instrument:{label}"),
                exchange=exchange,
                symbol=symbol,
                effective_from=HISTORY_FROM,
                effective_to=None,
                known_from=datetime(1990, 12, 19, tzinfo=UTC),
                known_to=None,
                effective_date_precision="OFFICIAL_DATE",
            )
        )
    return tuple(identities)


def _input_contract(dataset_code: str) -> tuple[dict[str, Any], ...]:
    """为内部 executor 构造最小、确定性的精确输入合同。"""
    if dataset_code == "equity.discovery.eod":
        return (
            {
                "datasetCode": "equity.workspace.reference-bundle",
                "binding": "PLAN_HEADER_EXACT_PUBLICATION",
            },
        )
    return ()


def _source(dataset_code: str) -> FrozenSource:
    """构造结构完整、自校验但绝不用于运行时取数的基准来源合同。"""
    internal = dataset_code in _INTERNAL_DATASETS
    provider_id = "platform" if internal else "benchmark-provider"
    upstream = "quant-v2.internal" if internal else "benchmark-upstream"
    executor = f"service_data_sync.benchmark.{dataset_code.replace('.', '_')}" if internal else None
    snapshot = (
        {
            "sourceKind": "INTERNAL_EXECUTOR" if internal else "EXTERNAL_PROVIDER",
            "providerId": provider_id,
            "capability": dataset_code,
            "upstreamSource": upstream,
        },
    )
    input_contract = _input_contract(dataset_code)
    earliest = HISTORY_FROM if dataset_code in _HISTORICAL_DATASETS else None
    draft = FrozenSource(
        dataset_code=dataset_code,
        publication_dataset_code=dataset_code,
        source_snapshot=snapshot,
        source_snapshot_hash=_sha256_json(list(snapshot)),
        earliest_date=earliest,
        earliest_date_method="DETERMINISTIC_SCALE_BOUNDARY",
        evidence_ref=f"benchmark://equity-backfill/{dataset_code}",
        evidence_sha256=_sha256_json(
            {
                "datasetCode": dataset_code,
                "earliestDate": None if earliest is None else earliest.isoformat(),
            }
        ),
        evidence_observed_at=KNOWN_AT,
        expected_provider_id=provider_id,
        expected_capability=dataset_code,
        expected_upstream_source=upstream,
        expected_adapter_version="benchmark-adapter-v1",
        expected_schema_fingerprint=_sha256_json({"schema": dataset_code, "version": 1}),
        supported_exchanges=("BSE", "SSE", "SZSE"),
        methodology_code=f"benchmark.{dataset_code}",
        methodology_version=1,
        mapping_version="benchmark-mapping-v1",
        source_contract_hash="0" * 64,
        source_kind="INTERNAL_EXECUTOR" if internal else "EXTERNAL_PROVIDER",
        internal_executor_code=executor,
        input_contract=input_contract,
        input_contract_hash=_sha256_json(list(input_contract)),
    )
    return replace(draft, source_contract_hash=source_contract_hash(draft))


def _sources() -> dict[str, FrozenSource]:
    """生成规划器要求的完整数据集来源清单。"""
    return {dataset_code: _source(dataset_code) for dataset_code in _PLANNED_DATASETS}


def _align8(value: int) -> int:
    """把 PostgreSQL 估算字节数向八字节边界取整。"""
    return (value + 7) // 8 * 8


def _varlena_size(value: str | bytes) -> int:
    """估算未 TOAST、未压缩 `varlena` 的四字节头和正文。"""
    payload = value.encode() if isinstance(value, str) else value
    return 4 + len(payload)


def _btree_entry_bytes(key_bytes: int) -> int:
    """按 90% 页利用率估算含 item pointer、heap TID 和键的 B-tree 条目。"""
    return int(_align8(24 + key_bytes) / 0.9)


def _child_spec_heap_bytes(child: PlannedChild) -> int:
    """估算一条 child spec 未压缩 heap tuple，JSONB 以规范 JSON 大小近似。"""
    nullable_bitmap_and_header = 32
    fixed_columns = 16 + 16 + 4 + 68 + 16 + 2 + 8
    variable_columns = (
        _varlena_size(child.phase)
        + _varlena_size(child.requirement)
        + _varlena_size(child.request_prefix)
        + _varlena_size(_canonical_json(child.targets))
        + _varlena_size(_canonical_json(child.intents))
        + _varlena_size(_canonical_json(child.dependency_keys))
        + _varlena_size(_canonical_json(child.completion_dependency_keys))
        + _varlena_size(_canonical_json(child.source_hashes))
    )
    nullable_fixed = (
        (4 if child.identity_ordinal is not None else 0)
        + (4 if child.window_from is not None else 0)
        + (4 if child.window_to is not None else 0)
    )
    return _align8(nullable_bitmap_and_header + fixed_columns + variable_columns + nullable_fixed)


def _child_spec_index_bytes(child: PlannedChild) -> int:
    """估算 child spec 五个 B-tree 索引的一条记录总字节数。"""
    return sum(
        (
            _btree_entry_bytes(16),
            _btree_entry_bytes(16 + 4),
            _btree_entry_bytes(16 + 68),
            _btree_entry_bytes(16),
            _btree_entry_bytes(16 + _varlena_size(child.phase) + 4),
        )
    )


def _child_state_initial_bytes(child_count: int) -> tuple[int, int]:
    """估算全部初始 `HELD` child state 的 heap 与三个索引。"""
    heap_per_row = _align8(32 + 16 + _varlena_size("HELD") + 4 + 8)
    indexes_per_row = (
        _btree_entry_bytes(16)
        + _btree_entry_bytes(1)
        + _btree_entry_bytes(_varlena_size("HELD") + 8)
    )
    return heap_per_row * child_count, indexes_per_row * child_count


def _child_result_minimum_bytes(child_count: int) -> tuple[int, int]:
    """估算所有 child 终态后结果清单的结构下限，不猜测真实审计正文。"""
    empty_json = _varlena_size(b"[]")
    empty_object = _varlena_size(b"{}")
    heap_per_row = _align8(
        32
        + 16
        + 16
        + _varlena_size("SUCCEEDED")
        + empty_json
        + 68
        + empty_json
        + 68
        + empty_object
        + 68
        + 8
    )
    indexes_per_row = _btree_entry_bytes(16) + _btree_entry_bytes(16) + _btree_entry_bytes(16)
    return heap_per_row * child_count, indexes_per_row * child_count


def _identity_table_bytes(identities: tuple[FrozenIdentity, ...]) -> tuple[int, int]:
    """估算冻结身份表 heap 与三个 B-tree 索引。"""
    heap = 0
    indexes = 0
    for identity in identities:
        heap += _align8(
            32
            + 16
            + 4
            + 16
            + 8
            + 16
            + _varlena_size(identity.exchange)
            + _varlena_size(identity.symbol)
            + 4
            + 8
            + _varlena_size(identity.effective_date_precision)
        )
        indexes += (
            _btree_entry_bytes(16 + 4)
            + _btree_entry_bytes(16 + 16)
            + _btree_entry_bytes(16 + 8 + 16)
        )
    return heap, indexes


def _topology_digest(topology: BackfillTopology) -> str:
    """流式计算 child 顺序和排除项摘要，避免构造另一份大 JSON。"""
    digest = hashlib.sha256()
    for child in topology.children:
        digest.update(child.child_key.encode())
        digest.update(b"\n")
    digest.update(_canonical_json(topology.exclusions))
    return digest.hexdigest()


def _storage_estimate(
    topology: BackfillTopology,
    identities: tuple[FrozenIdentity, ...],
) -> dict[str, object]:
    """估算计划创建事务与终态结果表的未压缩 PostgreSQL 量级。"""
    spec_heap = 0
    spec_indexes = 0
    for child in topology.children:
        spec_heap += _child_spec_heap_bytes(child)
        spec_indexes += _child_spec_index_bytes(child)
    state_heap, state_indexes = _child_state_initial_bytes(len(topology.children))
    identity_heap, identity_indexes = _identity_table_bytes(identities)
    create_heap = spec_heap + state_heap + identity_heap
    create_indexes = spec_indexes + state_indexes + identity_indexes
    create_total = create_heap + create_indexes
    result_heap, result_indexes = _child_result_minimum_bytes(len(topology.children))
    return {
        "basis": "未压缩 heap、90% B-tree 页利用率；不含 TOAST/WAL/控制面 run/publication",
        "planCreate": {
            "heapBytes": create_heap,
            "indexBytes": create_indexes,
            "totalBytes": create_total,
            "totalMiB": round(create_total / _MIB, 2),
            "walRangeMiB": [
                round(create_total * 1.0 / _MIB, 2),
                round(create_total * 2.0 / _MIB, 2),
            ],
        },
        "terminalResultMinimum": {
            "heapBytes": result_heap,
            "indexBytes": result_indexes,
            "totalBytes": result_heap + result_indexes,
            "totalMiB": round((result_heap + result_indexes) / _MIB, 2),
        },
    }


def _sequential_eta(partition_count: int) -> dict[str, object]:
    """按明确的单 checkpoint 耗时敏感度给出顺序 ETA，不伪装成实测预测。"""
    seconds_per_target = (0.25, 1.0, 5.0, 30.0)
    return {
        "formula": "checkpointPartitionCount * measuredSecondsPerPartition",
        "warning": "仅敏感度，不是 Provider 实测 SLA；真实 ETA 必须替换为各数据集实测分位数。",
        "scenarios": [
            {
                "secondsPerPartition": seconds,
                "hours": round(partition_count * seconds / 3600, 2),
                "days": round(partition_count * seconds / 86400, 2),
            }
            for seconds in seconds_per_target
        ],
    }


def _checkpoint_days(dataset_code: str) -> int | None:
    """返回 executor 对历史能力承诺的单 checkpoint 最大自然日数。"""
    if dataset_code in _DATE_DATASETS:
        return 366
    if dataset_code == "equity.corporate_action":
        return 1_098
    if dataset_code in _EVENT_DATASETS:
        return 31
    return None


def _partition_count(target: dict[str, Any], intent: dict[str, Any]) -> int:
    """根据冻结全区间计算一个目标实际需要执行的内部 checkpoint 数。"""
    maximum_days = _checkpoint_days(str(target["datasetCode"]))
    if maximum_days is None:
        return 1
    first_value = intent.get("backfillDateFrom") or target.get("dateFrom")
    last_value = intent.get("backfillDateTo") or target.get("dateTo")
    if not isinstance(first_value, str) or not isinstance(last_value, str):
        raise ValueError("historical benchmark target must freeze a complete backfill range")
    span_days = (date.fromisoformat(last_value) - date.fromisoformat(first_value)).days + 1
    if span_days < 1:
        raise ValueError("historical benchmark target range must not be empty")
    return math.ceil(span_days / maximum_days)


def _architecture_comparison(
    *,
    child_count: int,
    target_count: int,
    partition_work_count: int,
    current_storage_bytes: int,
    peak_rss_bytes: int,
    topology_seconds: float,
) -> dict[str, object]:
    """量化已落地内部 checkpoint 与尚需补齐的分页 seal 硬预算。"""
    page_children = 1_000
    current_page_count = math.ceil(child_count / page_children)
    baseline_commands = cast(int, _PRE_CHECKPOINT_BASELINE["commandCount"])
    baseline_partitions = cast(int, _PRE_CHECKPOINT_BASELINE["partitionWorkCount"])
    return {
        "hardAcceptanceBudget": {
            "topologyWallSecondsMax": 60,
            "plannerPeakRssMiBMax": 512,
            "sealedCommandCountMax": 50_000,
            "pageChildCountMax": page_children,
            "pageCanonicalPayloadMiBMax": 8,
            "singleWriteTransactionMiBMax": 64,
            "sealRequirements": [
                "完整 ordinal 连续",
                "page hash 与 root hash 一致",
                "child 总数与目标总数一致",
                "所有 dependency key 可解析且 DAG 无环",
                "SEALED 前零 command 可提交",
            ],
        },
        "measuredPreCheckpointBaseline": {
            **_PRE_CHECKPOINT_BASELINE,
            "evidence": "同一 5,500 身份、同一历史边界、同一 Docker Desktop 的变更前实测。",
        },
        "currentCheckpointTopology": {
            "commandCount": child_count,
            "targetCount": target_count,
            "partitionWorkCount": partition_work_count,
            "commandReductionPercent": round((1 - child_count / baseline_commands) * 100, 2),
            "partitionWorkReductionPercent": round(
                (1 - partition_work_count / baseline_partitions) * 100, 2
            ),
            "plannerPeakRssMiB": round(peak_rss_bytes / _MIB, 2),
            "planStorageMiB": round(current_storage_bytes / _MIB, 2),
            "estimatedMaximumWriteTransactionMiB": round(
                current_storage_bytes / current_page_count / _MIB,
                2,
            ),
            "topologyWallSeconds": round(topology_seconds, 6),
            "budgetResult": {
                "wall": "PASS" if topology_seconds <= 60 else "FAIL",
                "rss": "PASS" if peak_rss_bytes <= 512 * _MIB else "FAIL",
                "commands": "PASS" if child_count <= 50_000 else "FAIL",
                "singleTransaction": (
                    "PASS" if current_storage_bytes / current_page_count <= 64 * _MIB else "FAIL"
                ),
            },
            "checkpointRule": (
                "行情/因子每证券每数据集一个 command，366 日分区在 executor 内持久化；"
                "公司行动使用 1098 日、全局事件使用 31 日 checkpoint。"
            ),
            "requiredInvariant": (
                "checkpoint 必须保存分区、来源批次、事实数、publication/dataVersion 和终态；"
                "重试只执行未成功分区，最终 child result seal 覆盖全部分区。"
            ),
        },
        "optionA_DeterministicPagesAndSeal": {
            "commandCount": child_count,
            "pageCount": current_page_count,
            "maxLiveChildFraction": round(page_children / child_count, 6),
            "estimatedWriteMiBPerPage": round(current_storage_bytes / current_page_count / _MIB, 2),
            "storageChange": "总量基本不变；把物理原子大事务改为 SEALED 前不可见的逻辑原子计划。",
            "requiredInvariant": (
                "每页单独提交 BUILDING 数据；seal 事务只写根摘要和状态，"
                "任何缺页、重复 ordinal、hash 或依赖错误都不得 SEALED。"
            ),
        },
        "recommendedCombinedAPlusB": {
            "commandCount": child_count,
            "pageCount": current_page_count,
            "estimatedWriteMiBPerPage": round(current_storage_bytes / current_page_count / _MIB, 2),
            "partitionWorkCount": partition_work_count,
            "decision": (
                "B 已把历史窗口下沉 executor checkpoint；继续采用 A 分页写入并 seal，"
                "Provider 工作量不因元数据降维而消失。"
            ),
        },
    }


def _topology_statistics(topology: BackfillTopology) -> dict[str, object]:
    """汇总 child、目标、阶段、数据集和确定性摘要。"""
    phase_counts: Counter[str] = Counter()
    dataset_target_counts: Counter[str] = Counter()
    target_count = 0
    partition_work_count = 0
    for child in topology.children:
        phase_counts[child.phase] += 1
        target_count += len(child.targets)
        for target, intent in zip(child.targets, child.intents, strict=True):
            dataset_target_counts[str(target["datasetCode"])] += 1
            partition_work_count += _partition_count(target, intent)
    return {
        "childCount": len(topology.children),
        "targetCount": target_count,
        "checkpointPartitionCount": partition_work_count,
        "exclusionCount": len(topology.exclusions),
        "phaseChildCounts": dict(sorted(phase_counts.items())),
        "datasetTargetCounts": dict(sorted(dataset_target_counts.items())),
        "topologySha256": _topology_digest(topology),
        "sequentialExecutionEtaSensitivity": _sequential_eta(partition_work_count),
    }


def main() -> None:
    """运行一次隔离基准并输出可机器比较的阶段证据与最终结果。"""
    benchmark_started = time.perf_counter()
    baseline_rss = _rss_bytes()
    _emit(
        "benchmark_started",
        {
            "identityCount": IDENTITY_COUNT,
            "historyFrom": HISTORY_FROM.isoformat(),
            "snapshotObservedOn": SNAPSHOT_OBSERVED_ON.isoformat(),
            "marketAsOf": MARKET_AS_OF.isoformat(),
            "baselinePeakRssBytes": baseline_rss,
        },
    )

    identities_started = time.perf_counter()
    identities = _identities()
    identities_seconds = time.perf_counter() - identities_started
    roster_hash = compute_roster_hash(identities)
    sources = _sources()
    _emit(
        "inputs_frozen",
        {
            "identityCount": len(identities),
            "identityBuildSeconds": round(identities_seconds, 6),
            "rosterHash": roster_hash,
            "peakRssBytes": _rss_bytes(),
        },
    )

    topology_started = time.perf_counter()
    topology = build_topology(
        plan_id=PLAN_ID,
        snapshot_observed_on=SNAPSHOT_OBSERVED_ON,
        market_as_of=MARKET_AS_OF,
        known_at=KNOWN_AT,
        roster_hash=roster_hash,
        identities=identities,
        sources=sources,
        reference_bundle=_reference_bundle(),
    )
    topology_seconds = time.perf_counter() - topology_started
    _emit(
        "topology_built",
        {
            "childCount": len(topology.children),
            "topologyBuildAndAuditSeconds": round(topology_seconds, 6),
            "peakRssBytes": _rss_bytes(),
        },
    )

    estimate_started = time.perf_counter()
    statistics = _topology_statistics(topology)
    target_count = cast(int, statistics["targetCount"])
    partition_work_count = cast(int, statistics["checkpointPartitionCount"])
    storage = _storage_estimate(topology, identities)
    estimate_seconds = time.perf_counter() - estimate_started
    peak_rss = _rss_bytes()
    minimum_memory = int(peak_rss * 1.3)
    create_total = int(
        dict(storage["planCreate"])["totalBytes"]  # type: ignore[arg-type]
    )
    result = {
        "schema": "quant-v2.equity-backfill-topology-benchmark.v1",
        "identityCount": len(identities),
        "historyFrom": HISTORY_FROM.isoformat(),
        "snapshotObservedOn": SNAPSHOT_OBSERVED_ON.isoformat(),
        "marketAsOf": MARKET_AS_OF.isoformat(),
        "timingSeconds": {
            "identityBuild": round(identities_seconds, 6),
            "topologyBuildAndAudit": round(topology_seconds, 6),
            "statisticsAndStorageEstimate": round(estimate_seconds, 6),
            "total": round(time.perf_counter() - benchmark_started, 6),
        },
        "memory": {
            "baselinePeakRssBytes": baseline_rss,
            "processPeakRssBytes": peak_rss,
            "processPeakRssMiB": round(peak_rss / _MIB, 2),
            "recommendedContainerMinimumBytes": minimum_memory,
            "recommendedContainerMinimumMiB": round(minimum_memory / _MIB, 2),
        },
        "topology": statistics,
        "postgresEstimate": storage,
        "architectureComparison": _architecture_comparison(
            child_count=len(topology.children),
            target_count=target_count,
            partition_work_count=partition_work_count,
            current_storage_bytes=create_total,
            peak_rss_bytes=peak_rss,
            topology_seconds=topology_seconds,
        ),
        "transactionRisk": {
            "level": (
                "HIGH"
                if len(topology.children) > 100_000
                or peak_rss > 2 * 1024**3
                or create_total > 512 * _MIB
                else "MEDIUM"
            ),
            "atomicRows": len(topology.children) * 2 + len(identities) + 2,
            "reason": (
                "父计划创建需在一个事务冻结 identity、child spec 和 HELD state；"
                "大量 JSONB、索引与 WAL 会放大锁持有时间和故障恢复成本。"
            ),
            "failFast": (
                "创建前先按 child 公式估算并校验容器内存、数据库可用空间和 WAL 预算；"
                "低于实测峰值 1.3 倍时拒绝开始。"
            ),
            "writeBatch": (
                "同一数据库事务内按不超过 1,000 行执行批量 INSERT/COPY；"
                "不得跨事务提前暴露部分 child，也不得逐 ORM 对象 flush。"
            ),
        },
    }
    gc.collect()
    _emit("benchmark_completed", result)


if __name__ == "__main__":
    main()
