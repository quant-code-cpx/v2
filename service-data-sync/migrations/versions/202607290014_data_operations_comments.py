"""补齐数据运维控制面物理表中文说明。

控制面基线迁移已在开发环境应用时，不重写既有建表迁移；本迁移仅为已存在的权威账本补充
`PostgreSQL COMMENT`，与 ORM 中冻结的数据字典保持一致，不改变任何业务记录或约束。

Revision ID: 202607290014
Revises: 202607290013
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op

revision = "202607290014"
down_revision = "202607290013"
branch_labels = None
depends_on = None

_TABLE_COMMENTS = {
    "data_operation_idempotency": "数据运维内部写操作幂等账本；键相同但请求不同必须拒绝。",
    "data_operation_preflight": "同步预检冻结结果；不是锁、批准或执行成功证明。",
    "data_operation_command": "数据运维同步 parent command；child run 的顺序由 target_index 固定。",
    "data_operation_run": (
        "同步 child run；source_snapshot 受理后不可变，fencing token 防止陈旧 worker 写入。"
    ),
    "data_operation_partition": (
        "数据运维 run 内分区；checkpoint 只存摘要，不保存 Provider cursor。"
    ),
    "data_operation_execution_slot": (
        "全平台同步唯一槽；租约失效后 token 递增，旧 worker 不得再提交。"
    ),
    "data_operation_event": "不可变数据运维事件；错误只允许稳定脱敏摘要。",
    "data_operation_health_check": "主动发布后健康检查批次；不回滚既有 publication。",
    "data_operation_health_check_target": "主动健康检查有序 target；成功时关联不可变 evaluation。",
    "data_operation_health_evaluation": "不可变健康评估事实；开放问题状态不嵌入本表。",
    "data_operation_health_issue": "当前开放健康问题投影；历史评估仍保留原始事实。",
    "data_operation_schedule": "数据库持久化结构化自动计划；禁止任意 cron 和并行同步。",
}

_COLUMN_COMMENTS = {
    "data_operation_idempotency": {
        "idempotency_id": "幂等记录 UUID。",
        "operation": "内部写操作稳定名称。",
        "idempotency_key": "调用方稳定复用的内部幂等键。",
        "request_hash": "规范化请求 SHA-256，用于识别同键不同请求。",
        "resource_type": "受理后返回的权威资源类型。",
        "resource_id": "受理后返回的权威资源 UUID。",
        "response_json": "首次受理的安全响应，幂等重试必须原样重放。",
        "created_at": "幂等意图首次持久化时间。",
    },
    "data_operation_preflight": {
        "preflight_id": "预检 UUID。",
        "request_hash": "预检目标规范化摘要。",
        "targets_json": "按用户提交顺序冻结的同步目标。",
        "result_json": "逐目标预检结果，不含供应商正文。",
        "expires_at": "预检可用于提交的最晚时刻。",
        "created_at": "预检生成时间。",
    },
    "data_operation_command": {
        "command_id": "同步命令 UUID。",
        "submission_id": "API submission；系统、遗留或恢复命令为空。",
        "status": "命令聚合状态。",
        "actor_ref": "不透明操作主体引用，系统操作使用 `system`。",
        "actor_role": "提交者授权角色。",
        "reason": "强制操作原因。",
        "request_id": "跨服务关联请求标识。",
        "retry_of_command_id": "重试时关联原 parent command。",
        "error_json": "脱敏稳定错误摘要。",
        "requested_at": "命令受理时间。",
        "started_at": "首个 child run 实际开始时间。",
        "finished_at": "全部 child run 进入终态时间。",
    },
    "data_operation_run": {
        "run_id": "child run UUID。",
        "command_id": "所属 parent command。",
        "target_index": "提交顺序，从零开始且永不重排。",
        "dataset_code": "目标 canonical datasetCode。",
        "mode": "FULL 等用户请求同步模式。",
        "target_json": "日期范围或观察日等冻结同步目标。",
        "source_snapshot": "Provider、真实上游、adapter 与方法学冻结快照。",
        "execution_intent_json": (
            "仅 SYSTEM 兼容入口使用的私有执行意图；不得通过内部 HTTP 或公开投影暴露。"
        ),
        "status": "运行状态。",
        "queue_position": "受理快照中的队列位置；运行后可为空。",
        "attempt": "已开始执行的尝试次数。",
        "completed_partitions": "已完成分区数。",
        "total_partitions": "计划总分区数。",
        "processed_records": "已处理记录数。",
        "estimated_records": "上游可可靠估计时的记录数。",
        "fencing_token": "当前或最后持有的单调 slot token。",
        "cancel_requested": "合作式取消请求标志。",
        "error_json": "脱敏错误摘要。",
        "quality_gate_json": "发布前质量门结论，与发布后健康评估分离。",
        "requested_at": "运行入队时间。",
        "started_at": "获取全局 slot 后开始时间。",
        "finished_at": "进入终态时间。",
    },
    "data_operation_partition": {
        "run_id": "所属 child run。",
        "partition_key": "稳定分区键。",
        "status": "分区状态。",
        "attempt": "分区尝试次数。",
        "checkpoint_hash": "定长 SHA-256 位置摘要。",
        "checkpoint_kind": "位置摘要解释类型。",
        "checkpoint_updated_at": "最后安全 checkpoint 写入时间。",
        "error_json": "脱敏分区错误摘要。",
    },
    "data_operation_execution_slot": {
        "slot_key": "固定全局槽键 `global`。",
        "state": "执行槽状态。",
        "run_id": "当前持槽 child run。",
        "dataset_code": "当前持槽数据集。",
        "lease_until": "worker 必须续租的截止时刻。",
        "heartbeat_at": "最近有效心跳。",
        "fencing_token": "每次取得槽时递增的单调 token。",
    },
    "data_operation_event": {
        "event_id": "事件 UUID。",
        "resource_type": "COMMAND、RUN、HEALTH_CHECK 或 SCHEDULE。",
        "resource_id": "关联资源 UUID。",
        "action": "审计动作代码。",
        "result": "动作结果，不等同资源终态。",
        "actor_ref": "不透明主体或 system。",
        "request_id": "请求链路标识。",
        "error_json": "稳定脱敏错误摘要。",
        "occurred_at": "事件发生时间。",
    },
    "data_operation_health_check": {
        "health_check_id": "健康检查 UUID。",
        "submission_id": "API submission；系统检查可为空。",
        "status": "批次聚合状态。",
        "actor_ref": "提交主体。",
        "actor_role": "提交主体角色。",
        "reason": "主动检查原因。",
        "request_id": "请求链路标识。",
        "error_json": "批次错误。",
        "requested_at": "受理时间。",
        "started_at": "开始时间。",
        "finished_at": "终态时间。",
    },
    "data_operation_health_check_target": {
        "health_check_id": "所属健康检查。",
        "target_index": "原提交顺序。",
        "dataset_code": "目标数据集。",
        "requested_data_version": "请求版本。",
        "resolved_data_version": "受理绑定版本。",
        "status": "目标状态。",
        "evaluation_id": "成功评估 UUID。",
        "error_json": "失败脱敏摘要。",
    },
    "data_operation_health_evaluation": {
        "evaluation_id": "评估 UUID。",
        "health_check_id": "主动检查批次；自动评估为空。",
        "dataset_code": "被评估数据集。",
        "data_version": "冻结 dataVersion。",
        "release_id": "冻结 release。",
        "policy_code": "健康策略代码。",
        "policy_version": "健康策略版本。",
        "status": "健康聚合状态。",
        "score": "可计算时的零到一百分。",
        "results_json": "有界脱敏规则结果。",
        "evaluated_at": "评估时间。",
    },
    "data_operation_health_issue": {
        "issue_id": "问题 UUID。",
        "dataset_code": "问题数据集。",
        "rule_code": "来源规则代码。",
        "dimension": "健康维度。",
        "severity": "WARN 或 CRITICAL。",
        "status": "OPEN 或 ACKNOWLEDGED。",
        "first_detected_at": "首次发现时间。",
        "last_detected_at": "最近发现时间。",
        "affected_count": "可估计受影响记录数。",
        "evidence_summary": "脱敏聚合证据。",
    },
    "data_operation_schedule": {
        "schedule_id": "计划 UUID。",
        "dataset_code": "v1 唯一计划数据集。",
        "mode": "自动计划同步模式。",
        "selector_json": "已审核业务目标选择器；计划触发时冻结到 command。",
        "target_policy_json": "版本化目标解析策略。",
        "frequency_json": "受限结构化频率。",
        "misfire_policy": "SKIP 或 RUN_ONCE。",
        "coalesce": "错过触发是否合并。",
        "enabled": "计划启停状态。",
        "version": "乐观锁版本。",
        "revision_id": "当前修订 UUID。",
        "recent_run_at": "最近成功投递时间。",
        "next_run_at": "下一次计划触发。",
        "updated_at": "最近更新。",
        "updated_by_actor_ref": "最近更新主体。",
    },
}


def upgrade() -> None:
    """为控制面权威表添加可直接在数据库中查看的中文职责说明。"""
    for table_name, comment in _TABLE_COMMENTS.items():
        escaped_comment = comment.replace("'", "''")
        op.execute(f"COMMENT ON TABLE {table_name} IS '{escaped_comment}'")
    for table_name, column_comments in _COLUMN_COMMENTS.items():
        for column_name, comment in column_comments.items():
            escaped_comment = comment.replace("'", "''")
            op.execute(f"COMMENT ON COLUMN {table_name}.{column_name} IS '{escaped_comment}'")


def downgrade() -> None:
    """删除本迁移添加的表说明，不触碰控制面命令、审计和健康事实。"""
    for table_name, column_comments in _COLUMN_COMMENTS.items():
        for column_name in column_comments:
            op.execute(f"COMMENT ON COLUMN {table_name}.{column_name} IS NULL")
    for table_name in _TABLE_COMMENTS:
        op.execute(f"COMMENT ON TABLE {table_name} IS NULL")
