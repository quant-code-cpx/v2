"""以不可变历史 bundle 为目标原子回滚互联互通消费指针。

回滚不改写统计、活跃榜、状态、日历或来源事实；事务只重新激活已验证的历史 bundle，
同步重建或重新激活同日全部 overview 子集指针，并追加不可变审计。所有写入必须处于
`DataOperationsControlPlane` 传播的有效 fencing 上下文中。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import combinations
from typing import NoReturn
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from service_data_sync.domain.stock_connect import StockConnectChannel
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.fenced_execution import current_fenced_execution
from service_data_sync.infrastructure.database.models.canonical import DatasetRelease
from service_data_sync.infrastructure.database.models.market import (
    StockConnectBundlePublication,
    StockConnectBundleRollbackAudit,
    StockConnectCalendarObservation,
    StockConnectChannelStatusRevision,
    StockConnectOverviewPublication,
)
from service_data_sync.infrastructure.database.models.operations import (
    DataOperationCommand,
    DataOperationRun,
)

_APPROVED_QUALITY = frozenset({"APPROVED", "APPROVED_WITH_WARNINGS"})


class StockConnectBundleRollbackRejected(ValueError):
    """携带稳定错误码表示回滚目标或 publication 图不满足 fail-closed 条件。"""

    def __init__(self, code: str, message: str) -> None:
        """保存可由控制面安全映射的低基数代码和内部诊断消息。"""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RolledBackStockConnectBundle:
    """描述一次完整包指针回滚或同 run 幂等重放的稳定结果。"""

    rollback_id: UUID
    from_bundle_release_id: UUID
    to_bundle_release_id: UUID
    target_data_version: str
    overview_release_ids: tuple[tuple[str, UUID], ...]
    reused: bool


@dataclass(frozen=True, slots=True)
class _OverviewTransition:
    """保存校验完成后才能应用的一条 overview 指针转换。"""

    channel_set: str
    current: StockConnectOverviewPublication
    historical: StockConnectOverviewPublication | None
    target_release_id: UUID
    target_data_version: str
    component_bundle_ids: dict[str, str]
    quality_status: str
    quality_issues: list[dict[str, str]]
    source_refs: list[dict[str, object]]
    content_hash: str


class SqlAlchemyStockConnectRollbackRepository:
    """在一个受 fencing 保护的事务中回滚 bundle 与全部 overview 当前指针。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存服务拥有的事务工厂，不允许调用方传入或跨线程复用 session。"""
        self._database = database

    def rollback_bundle(
        self,
        *,
        channel: StockConnectChannel,
        trade_date: date,
        target_bundle_release_id: UUID,
        actor_ref: str,
        reason: str,
    ) -> RolledBackStockConnectBundle:
        """回滚到同日同通道历史完整包；目标、总览、审计和 fencing 任一不一致即全事务失败。"""
        execution = current_fenced_execution()
        if execution is None or execution.database is not self._database:
            raise RuntimeError("stock-connect bundle rollback requires an active fencing context")
        with self._database.transaction() as session:
            request_id = _validate_operation_identity(
                session,
                run_id=execution.run_id,
                fencing_token=execution.fencing_token,
                actor_ref=actor_ref,
                reason=reason,
            )
            existing_audit = session.execute(
                select(StockConnectBundleRollbackAudit)
                .where(StockConnectBundleRollbackAudit.operation_run_id == execution.run_id)
                .with_for_update()
            ).scalar_one_or_none()
            if existing_audit is not None:
                return _replay_existing_rollback(
                    session,
                    audit=existing_audit,
                    channel=channel,
                    trade_date=trade_date,
                    target_bundle_release_id=target_bundle_release_id,
                    actor_ref=actor_ref,
                    reason=reason,
                    fencing_token=execution.fencing_token,
                )

            target = session.execute(
                select(StockConnectBundlePublication)
                .where(StockConnectBundlePublication.bundle_release_id == target_bundle_release_id)
                .with_for_update()
            ).scalar_one_or_none()
            if target is None:
                _reject(
                    "rollback-target-unavailable", "stock-connect rollback target is unavailable"
                )
            if (
                target.trade_date != trade_date
                or target.channel != channel.channel
                or target.direction != channel.direction
            ):
                _reject(
                    "rollback-target-identity-mismatch",
                    "stock-connect rollback target does not match requested date and channel",
                )
            _validate_bundle(session, target)

            current_bundles = _current_bundle_map(session, trade_date=trade_date)
            channel_code = _channel_code(channel)
            current = current_bundles.get(channel_code)
            if current is None:
                _reject(
                    "rollback-current-unavailable",
                    "stock-connect current bundle is unavailable for requested date and channel",
                )
            if UUID(str(current.bundle_release_id)) == target_bundle_release_id:
                _reject(
                    "rollback-target-not-historical",
                    "stock-connect rollback target is already current without this run audit",
                )
            if target.superseded_at is None or target.superseded_at < target.published_at:
                _reject(
                    "rollback-target-not-historical",
                    "stock-connect rollback target is not a valid historical bundle",
                )
            for bundle in current_bundles.values():
                _validate_bundle(session, bundle)

            current_overviews = _current_overview_map(
                session,
                trade_date=trade_date,
                bundles=current_bundles,
            )
            desired_bundles = {**current_bundles, channel_code: target}
            transitions = _prepare_overview_transitions(
                session,
                trade_date=trade_date,
                current_overviews=current_overviews,
                desired_bundles=desired_bundles,
            )
            now = datetime.now(UTC)

            # 先关闭旧 current，再激活历史 target，满足部分唯一索引且两步仍属同一事务。
            session.execute(
                update(StockConnectBundlePublication)
                .where(StockConnectBundlePublication.bundle_release_id == current.bundle_release_id)
                .values(superseded_at=now)
            )
            session.execute(
                update(StockConnectBundlePublication)
                .where(StockConnectBundlePublication.bundle_release_id == target.bundle_release_id)
                .values(superseded_at=None)
            )
            _apply_overview_transitions(session, transitions=transitions, now=now)

            rollback_id = uuid4()
            from_overviews = {
                key: str(value.overview_release_id)
                for key, value in sorted(current_overviews.items())
            }
            to_overviews = {
                transition.channel_set: str(transition.target_release_id)
                for transition in transitions
            }
            session.execute(
                insert(StockConnectBundleRollbackAudit).values(
                    rollback_id=rollback_id,
                    operation_run_id=execution.run_id,
                    fencing_token=execution.fencing_token,
                    trade_date=trade_date,
                    channel=channel.channel,
                    direction=channel.direction,
                    from_bundle_release_id=current.bundle_release_id,
                    to_bundle_release_id=target.bundle_release_id,
                    from_overview_release_ids=from_overviews,
                    to_overview_release_ids=to_overviews,
                    actor_ref=actor_ref,
                    reason=reason,
                    request_id=request_id,
                    rolled_back_at=now,
                )
            )
            return RolledBackStockConnectBundle(
                rollback_id=rollback_id,
                from_bundle_release_id=UUID(str(current.bundle_release_id)),
                to_bundle_release_id=UUID(str(target.bundle_release_id)),
                target_data_version=target.data_version,
                overview_release_ids=tuple(
                    (transition.channel_set, transition.target_release_id)
                    for transition in transitions
                ),
                reused=False,
            )


def _validate_operation_identity(
    session: Session,
    *,
    run_id: UUID,
    fencing_token: int,
    actor_ref: str,
    reason: str,
) -> str:
    """要求 actor、reason 和 token 与当前权威 command/run 完全一致，拒绝仓储调用方伪造审计。"""
    row = session.execute(
        select(
            DataOperationCommand.actor_ref,
            DataOperationCommand.reason,
            DataOperationCommand.request_id,
            DataOperationRun.fencing_token,
        )
        .join(DataOperationRun, DataOperationRun.command_id == DataOperationCommand.command_id)
        .where(DataOperationRun.run_id == run_id)
    ).one_or_none()
    if row is None or row.fencing_token is None or int(row.fencing_token) != fencing_token:
        _reject(
            "rollback-operation-identity-mismatch",
            "stock-connect rollback run identity or fencing token is inconsistent",
        )
    if row.actor_ref != actor_ref or row.reason != reason:
        _reject(
            "rollback-operation-identity-mismatch",
            "stock-connect rollback actor or reason differs from authoritative command",
        )
    if not 1 <= len(actor_ref.strip()) <= 128 or not 8 <= len(reason.strip()) <= 2000:
        _reject(
            "rollback-operation-identity-mismatch",
            "stock-connect rollback actor or reason length is invalid",
        )
    return str(row.request_id)


def _replay_existing_rollback(
    session: Session,
    *,
    audit: StockConnectBundleRollbackAudit,
    channel: StockConnectChannel,
    trade_date: date,
    target_bundle_release_id: UUID,
    actor_ref: str,
    reason: str,
    fencing_token: int,
) -> RolledBackStockConnectBundle:
    """仅在请求与已提交审计完全一致且指针仍处于目标状态时重放结果。"""
    if (
        audit.trade_date != trade_date
        or audit.channel != channel.channel
        or audit.direction != channel.direction
        or UUID(str(audit.to_bundle_release_id)) != target_bundle_release_id
        or audit.actor_ref != actor_ref
        or audit.reason != reason
        or audit.fencing_token != fencing_token
    ):
        _reject(
            "rollback-idempotency-conflict",
            "stock-connect rollback run was already used for a different request",
        )
    bundles = _current_bundle_map(session, trade_date=trade_date)
    current = bundles.get(_channel_code(channel))
    if current is None or UUID(str(current.bundle_release_id)) != target_bundle_release_id:
        _reject(
            "rollback-idempotency-state-diverged",
            "stock-connect rollback target is no longer current during replay",
        )
    for bundle in bundles.values():
        _validate_bundle(session, bundle)
    overviews = _current_overview_map(session, trade_date=trade_date, bundles=bundles)
    actual_overviews = {
        key: str(value.overview_release_id) for key, value in sorted(overviews.items())
    }
    if actual_overviews != audit.to_overview_release_ids:
        _reject(
            "rollback-idempotency-state-diverged",
            "stock-connect overview pointers diverged after recorded rollback",
        )
    return RolledBackStockConnectBundle(
        rollback_id=UUID(str(audit.rollback_id)),
        from_bundle_release_id=UUID(str(audit.from_bundle_release_id)),
        to_bundle_release_id=target_bundle_release_id,
        target_data_version=current.data_version,
        overview_release_ids=tuple(
            (key, UUID(value)) for key, value in sorted(actual_overviews.items())
        ),
        reused=True,
    )


def _current_bundle_map(
    session: Session, *, trade_date: date
) -> dict[str, StockConnectBundlePublication]:
    """按稳定顺序锁定同日全部 current bundle，形成 overview 完整性校验基线。"""
    rows = (
        session.execute(
            select(StockConnectBundlePublication)
            .where(
                StockConnectBundlePublication.trade_date == trade_date,
                StockConnectBundlePublication.superseded_at.is_(None),
            )
            .order_by(
                StockConnectBundlePublication.channel,
                StockConnectBundlePublication.direction,
            )
            .with_for_update()
        )
        .scalars()
        .all()
    )
    result = {_bundle_channel_code(row): row for row in rows}
    if len(result) != len(rows):
        _reject(
            "rollback-current-inconsistent",
            "stock-connect current bundle identities are duplicated",
        )
    return result


def _current_overview_map(
    session: Session,
    *,
    trade_date: date,
    bundles: Mapping[str, StockConnectBundlePublication],
) -> dict[str, StockConnectOverviewPublication]:
    """锁定并验证同日每个非空通道子集恰有一个完整 current overview。"""
    rows = (
        session.execute(
            select(StockConnectOverviewPublication)
            .where(
                StockConnectOverviewPublication.trade_date == trade_date,
                StockConnectOverviewPublication.superseded_at.is_(None),
            )
            .order_by(StockConnectOverviewPublication.channel_set)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    current = {row.channel_set: row for row in rows}
    expected_sets = {
        ",".join(subset)
        for size in range(1, len(bundles) + 1)
        for subset in combinations(sorted(bundles), size)
    }
    if len(current) != len(rows) or set(current) != expected_sets:
        _reject(
            "rollback-overview-incomplete",
            "stock-connect current overview subset coverage is incomplete",
        )
    for channel_set, overview in current.items():
        selected = {key: bundles[key] for key in channel_set.split(",")}
        _validate_overview(overview, bundles=selected)
    return current


def _prepare_overview_transitions(
    session: Session,
    *,
    trade_date: date,
    current_overviews: Mapping[str, StockConnectOverviewPublication],
    desired_bundles: Mapping[str, StockConnectBundlePublication],
) -> tuple[_OverviewTransition, ...]:
    """为目标 bundle 与其他 current bundle 的每个子集解析历史 overview 或准备新版本。"""
    transitions: list[_OverviewTransition] = []
    channels = sorted(desired_bundles)
    for size in range(1, len(channels) + 1):
        for subset in combinations(channels, size):
            channel_set = ",".join(subset)
            current = current_overviews[channel_set]
            selected = {key: desired_bundles[key] for key in subset}
            (
                component_bundle_ids,
                quality_status,
                issues,
                refs,
                content_hash,
            ) = _overview_values(selected)
            historical = None
            if current.content_hash == content_hash:
                target_release_id = UUID(str(current.overview_release_id))
                target_data_version = current.data_version
            else:
                historical = session.execute(
                    select(StockConnectOverviewPublication)
                    .where(
                        StockConnectOverviewPublication.trade_date == trade_date,
                        StockConnectOverviewPublication.channel_set == channel_set,
                        StockConnectOverviewPublication.content_hash == content_hash,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if historical is not None:
                    _validate_overview(historical, bundles=selected)
                    target_release_id = UUID(str(historical.overview_release_id))
                    target_data_version = historical.data_version
                else:
                    target_release_id = uuid4()
                    target_data_version = str(uuid4())
            transitions.append(
                _OverviewTransition(
                    channel_set=channel_set,
                    current=current,
                    historical=historical,
                    target_release_id=target_release_id,
                    target_data_version=target_data_version,
                    component_bundle_ids=component_bundle_ids,
                    quality_status=quality_status,
                    quality_issues=issues,
                    source_refs=refs,
                    content_hash=content_hash,
                )
            )
    return tuple(transitions)


def _apply_overview_transitions(
    session: Session,
    *,
    transitions: Sequence[_OverviewTransition],
    now: datetime,
) -> None:
    """按稳定 channel-set 顺序应用已验证转换，旧 current 关闭后才激活或插入目标。"""
    for transition in transitions:
        if UUID(str(transition.current.overview_release_id)) == transition.target_release_id:
            continue
        session.execute(
            update(StockConnectOverviewPublication)
            .where(
                StockConnectOverviewPublication.overview_release_id
                == transition.current.overview_release_id
            )
            .values(superseded_at=now)
        )
        if transition.historical is not None:
            session.execute(
                update(StockConnectOverviewPublication)
                .where(
                    StockConnectOverviewPublication.overview_release_id
                    == transition.historical.overview_release_id
                )
                .values(superseded_at=None)
            )
            continue
        session.execute(
            insert(StockConnectOverviewPublication).values(
                overview_release_id=transition.target_release_id,
                data_version=transition.target_data_version,
                trade_date=transition.current.trade_date,
                channel_set=transition.channel_set,
                component_bundle_ids=transition.component_bundle_ids,
                quality_status=transition.quality_status,
                quality_issues=transition.quality_issues,
                source_refs=transition.source_refs,
                content_hash=transition.content_hash,
                published_at=now,
                superseded_at=None,
            )
        )


def _validate_bundle(session: Session, bundle: StockConnectBundlePublication) -> None:
    """验证 bundle 身份、质量、依赖、行数和内容摘要，不信任可修改 JSONB。"""
    if bundle.quality_status not in _APPROVED_QUALITY:
        _reject(
            "rollback-target-quality-rejected",
            "stock-connect bundle quality is not approved",
        )
    summary = bundle.summary_json
    active = bundle.active_securities_json
    issues = bundle.quality_issues
    refs = bundle.source_refs
    channel_code = _bundle_channel_code(bundle)
    if (
        not isinstance(summary, dict)
        or summary.get("channel") != channel_code
        or summary.get("direction") != bundle.direction
        or summary.get("tradeDate") != bundle.trade_date.isoformat()
        or summary.get("activeSecurityCount") != bundle.active_security_count
        or not isinstance(active, list)
        or len(active) != bundle.active_security_count
        or not isinstance(issues, list)
        or len(issues) > 20
        or not isinstance(refs, list)
        or not 1 <= len(refs) <= 12
        or (bundle.active_release_id is None) != (bundle.active_security_count == 0)
        or not bundle.data_version
    ):
        _reject(
            "rollback-target-incomplete",
            "stock-connect bundle payload is incomplete or identity-inconsistent",
        )
    if session.get(DatasetRelease, bundle.market_release_id) is None:
        _reject("rollback-target-incomplete", "stock-connect market release is unavailable")
    if (
        bundle.active_release_id is not None
        and session.get(DatasetRelease, bundle.active_release_id) is None
    ):
        _reject("rollback-target-incomplete", "stock-connect active release is unavailable")
    if session.get(StockConnectChannelStatusRevision, bundle.status_revision_id) is None:
        _reject("rollback-target-incomplete", "stock-connect status revision is unavailable")
    if session.get(StockConnectCalendarObservation, bundle.calendar_observation_id) is None:
        _reject("rollback-target-incomplete", "stock-connect calendar observation is unavailable")
    payload = {
        "tradeDate": bundle.trade_date.isoformat(),
        "channel": channel_code,
        "marketReleaseId": str(bundle.market_release_id),
        "activeReleaseId": (
            None if bundle.active_release_id is None else str(bundle.active_release_id)
        ),
        "statusRevisionId": str(bundle.status_revision_id),
        "calendarObservationId": str(bundle.calendar_observation_id),
        "summary": summary,
        "activeSecurities": active,
        "qualityIssues": issues,
        "sourceRefs": refs,
    }
    if _hash_payload(payload) != bundle.content_hash:
        _reject(
            "rollback-target-incomplete",
            "stock-connect bundle content hash does not match persisted payload",
        )


def _validate_overview(
    overview: StockConnectOverviewPublication,
    *,
    bundles: Mapping[str, StockConnectBundlePublication],
) -> None:
    """验证 overview 组件全集、质量、来源与内容摘要和指定 bundle 映射完全一致。"""
    (
        component_bundle_ids,
        quality_status,
        issues,
        refs,
        content_hash,
    ) = _overview_values(bundles)
    if (
        overview.channel_set != ",".join(sorted(bundles))
        or overview.component_bundle_ids != component_bundle_ids
        or overview.quality_status != quality_status
        or overview.quality_status not in _APPROVED_QUALITY
        or overview.quality_issues != issues
        or overview.source_refs != refs
        or overview.content_hash != content_hash
        or not overview.data_version
        or any(bundle.trade_date != overview.trade_date for bundle in bundles.values())
    ):
        _reject(
            "rollback-overview-incomplete",
            "stock-connect overview components or content are inconsistent",
        )


def _overview_values(
    bundles: Mapping[str, StockConnectBundlePublication],
) -> tuple[
    dict[str, str],
    str,
    list[dict[str, str]],
    list[dict[str, object]],
    str,
]:
    """按正式发布器口径构造 overview 的组件、质量、来源和确定性摘要。"""
    if not bundles:
        _reject("rollback-overview-incomplete", "stock-connect overview has no components")
    issues = _deduplicate_issues(
        [issue for bundle in bundles.values() for issue in bundle.quality_issues]
    )
    refs = _deduplicate_source_refs(
        [ref for bundle in bundles.values() for ref in bundle.source_refs]
    )
    if len(refs) > 12:
        _reject(
            "rollback-overview-incomplete",
            "stock-connect overview source reference count exceeds the contract",
        )
    component_bundle_ids = {
        channel: str(bundles[channel].bundle_release_id) for channel in sorted(bundles)
    }
    payload: dict[str, object] = {
        "components": {
            channel: {
                "bundleReleaseId": component_bundle_ids[channel],
                "dataVersion": bundles[channel].data_version,
            }
            for channel in sorted(bundles)
        },
        "qualityIssues": issues,
        "sourceRefs": refs,
    }
    return (
        component_bundle_ids,
        "APPROVED_WITH_WARNINGS" if issues else "APPROVED",
        issues,
        refs,
        _hash_payload(payload),
    )


def _deduplicate_issues(
    issues: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    """按正式发布器三字段键稳定去重，并把残缺质量问题视为 publication 损坏。"""
    try:
        values = {
            (str(item["code"]), str(item["component"]), str(item["detail"])) for item in issues
        }
    except (KeyError, TypeError) as error:
        raise StockConnectBundleRollbackRejected(
            "rollback-overview-incomplete",
            "stock-connect quality issue is malformed",
        ) from error
    return [
        {"code": code, "component": component, "detail": detail}
        for code, component, detail in sorted(values)
    ]


def _deduplicate_source_refs(
    refs: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """按正式发布器来源、产品和文件摘要键稳定去重。"""
    unique: dict[tuple[str, str, object], dict[str, object]] = {}
    try:
        for item in refs:
            value = dict(item)
            key = (
                str(value.get("sourceCode")),
                str(value.get("productName")),
                value.get("sourceFileSha256"),
            )
            unique[key] = value
    except (TypeError, ValueError) as error:
        raise StockConnectBundleRollbackRejected(
            "rollback-overview-incomplete",
            "stock-connect source reference is malformed",
        ) from error
    return [unique[key] for key in sorted(unique, key=lambda item: tuple(map(str, item)))]


def _hash_payload(payload: Mapping[str, object]) -> str:
    """使用正式 publication 的规范 JSON 编码计算 SHA-256。"""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _channel_code(channel: StockConnectChannel) -> str:
    """返回公开合同使用的稳定通道方向代码。"""
    return f"{channel.channel}_{channel.direction}"


def _bundle_channel_code(bundle: StockConnectBundlePublication) -> str:
    """从持久化 bundle 身份返回稳定通道方向代码。"""
    return f"{bundle.channel}_{bundle.direction}"


def _reject(code: str, message: str) -> NoReturn:
    """抛出带稳定低基数代码的 fail-closed 回滚拒绝。"""
    raise StockConnectBundleRollbackRejected(code, message)
