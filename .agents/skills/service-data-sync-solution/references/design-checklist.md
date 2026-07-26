# Data Sync 方案检查表

## 任务定义

- 业务目的、消费者、数据集与 capability 明确。
- 市场、交易所范围、时区、交易日历、粒度、历史区间和新鲜度明确。
- 数据量、增长率、峰值、供应商配额、成本和许可状态有证据或标为待决。
- 全量、增量、回补、修订和删除语义分别定义。
- 派生指标的语义族、方法论、样本池、分桶、占比分母、窗口、截点和最终态明确。

## Provider 边界

- 供应商 SDK、URL、字段和错误只存在于独立 adapter。
- application 仅消费 provider-neutral port/batch。
- adapter 不直接写 canonical 数据库。
- schema 漂移、超时、限流、鉴权失败和空响应映射为中立错误。
- 路由、fallback 和对账只在已有决策支持时设计。
- adapter 与上游方法论来源分别留痕；provider-neutral 不抹去来源和算法身份。
- 同名异口径来源独立发布，主源缺失不会触发静默跨口径补洞。

## 执行与恢复

- 定义 trigger、schedule、partition、checkpoint 和幂等键。
- 定义同分区并发、锁租约、超时、取消和僵尸任务处理。
- 定义 retryable/terminal 错误、退避、重试上限和人工恢复入口。
- 重跑、断点恢复、回补和供应商修订不会产生重复或静默缺口。
- 任务状态至少区分 queued/running/succeeded/partial/failed/cancelled。

## 数据与质量

- canonical schema、主键、时间语义、来源、observed/effective time 和版本明确。
- canonical 身份包含来源和方法论；原始单位、标准单位、窗口、截点、最终态和覆盖率可追溯。
- 原始批次、标准化数据、质量结果和发布数据的所有权明确。
- 校验覆盖类型、范围、唯一性、完整性、时序、跨源对账和异常波动。
- 跨源计算先通过可比性准入；供应商滚动值、内部聚合值和日频源事实分别存储。
- 定义 reject、quarantine、warn、publish 的阈值与处置。
- 写入事务、upsert/replace、修订历史、保留和清理策略明确。
- migration、backfill、rollback、备份恢复和兼容窗口可执行。

## 运行与安全

- 仅通过环境变量和 secret 注入凭据；无真实账号进入方案或仓库。
- egress、资源上限、速率限制、健康检查和依赖诊断明确。
- 日志可关联 task/run/batch/partition/provider；指标与告警有阈值。
- 明确 on-call/运营负责人、故障定位入口、人工补偿和退出条件。
- 消费者只通过版本化 API/事件读取，不直连数据库或供应商。

## 验收

- 同一分区连续运行两次结果一致。
- 中断后可从 checkpoint 恢复。
- provider timeout、schema drift、重复数据和数据库失败路径可验证。
- 同名异口径、主源缺失、盘中/EOD 冲突、单位突变和来源切换不会造成静默混算。
- 数据范围、时区边界、交易日历和质量阈值有真实样本验证。
- Docker 构建、Ruff、Pyright、现有测试、架构测试、迁移和健康检查命令明确。
