# ADR-0024：数据运维控制面与全局串行同步

- 状态：Proposed
- 日期：2026-07-29
- 决策者：quant-v2 维护者（待评审）
- 所有者：`service-data-sync`
- 主要消费者：`service-api`、`service-web`
- 关联方案：
  [跨服务总方案](../architecture/0002-data-operations-control-plane/index.html)、
  [data-sync 方案](../service-data-sync/0031-data-operations-control-plane/index.html)、
  [API 方案](../service-api/0006-data-operations-control-plane/index.html)、
  [Web 方案](../service-web/0006-data-operations-console/index.html)

## 背景

仓库已存在多个数据域、Provider Adapter、CLI、Celery 任务、硬编码 beat schedule、领域 checkpoint、
质量结果和 publication，但没有覆盖所有入口的统一运维控制面：

- `sync_run`、`sync_partition` 及租约只被部分链路使用；
- CLI、Celery 手工任务和自动计划可从不同路径直接进入同步用例；
- 当前不存在跨数据集、跨 worker、跨触发方式的全局同步互斥；
- 自动时间主要固化在 Celery beat 配置，无法安全地由运维界面动态修改；
- 来源、最近执行、发布、健康和操作审计分散在不同表与日志中；
- `service-api` 已有认证、角色和安全审计，但不拥有同步事实、任务锁或数据质量结论。

[ADR-0002](./0002-data-sync-ownership-and-access.md) 已提出：`service-data-sync` 独占数据存储，并通过版本化
内部 HTTP 接口让 `service-api` 查询数据、提交手工同步、读取任务状态和修改允许动态变更的同步配置。
本 ADR 细化该控制面，不改变 [ADR-0004](./0004-market-data-provider-adapters.md) 的 Provider 隔离、
[ADR-0016](./0016-money-flow-methodology-boundary.md) 的方法学边界或
[ADR-0021](./0021-failure-only-source-payload-retention.md) 的失败留证策略。

## 已确认事实与约束

- `canonical_dataset`、source batch、质量评估、隔离、release、publication 和 checkpoint 已提供可复用基座，
  但历史能力尚未全部迁入统一模型。
- `sync_run` 当前状态为 `queued/running/succeeded/partial/failed/cancelled`，模式为
  `manual/scheduled/backfill/legacy`；它不是全局队列，也没有全局唯一运行约束。
- `SyncPartition` 具有分区租约和恢复 checkpoint；这些租约只防同一分区重复执行，不能保证全平台只运行一个同步。
- 当前真实 Provider Adapter 以 AKShare 为入口，并记录东财、同花顺、腾讯、新浪、巨潮、中证、国证、
  交易所等进一步来源；仓库没有已实现的 Tushare Adapter。
- 所有来源开关默认关闭。代码存在不代表目标环境已启用、已生产批准或已有数据。
- `service-api` 所有业务与运维路由只能使用 `POST`；`service-web` 只能访问 `service-api`。
- 用户要求任意时刻最多运行一个同步任务。该约束必须覆盖自动、手工、单个、批量、重试、恢复和旧 CLI，
  不能只依赖前端禁用按钮或单个 Celery queue 的 worker concurrency。

## 候选方案

| 方案                                          | 优势                                                 | 主要风险                                                              | 结论       |
| --------------------------------------------- | ---------------------------------------------------- | --------------------------------------------------------------------- | ---------- |
| Web/API 看到运行中后拒绝新请求                | 实现最少                                             | 竞态明显；CLI、scheduler、多个 API 实例可绕过                         | 排除       |
| Celery worker `concurrency=1`                 | 复用现有队列                                         | 多 worker、多个 queue、CLI 直调和 worker 重启可绕过；队列不是权威账本 | 排除       |
| PostgreSQL 权威命令队列 + 全局租约/fencing    | 可跨实例、跨入口串行；可审计、恢复、取消和防僵尸提交 | 需要迁移所有触发入口并增加恢复状态机                                  | **推荐**   |
| PostgreSQL session advisory lock 持有整个任务 | 强互斥、表少                                         | 长任务占连接；断连失锁；无法表达队列、心跳和业务恢复                  | 不单独采用 |

## 拟议决策

若本 ADR 获接受，采用 **`service-data-sync` PostgreSQL 权威命令队列 + 一个全局同步执行槽 +
租约心跳 + 单调 fencing token**。

### 1. 所有权与调用方向

1. `service-data-sync` 拥有数据集运维目录、命令、运行、分区、全局执行槽、自动计划、健康评估和执行事件。
2. `service-api` 拥有用户身份、授权决定、安全审计，以及本地 `DataOperationSubmission` 和 HTTP outbox；
   这些记录只表示授权与交付意图，不是同步命令、运行或质量事实。
3. `service-web` 只展示 `service-api` 投影并提交用户意图，不计算任务状态、不选择 Provider、不直连 data-sync。
4. 两条机器合同为：
   [data-sync 内部合同](../contracts/0022-data-sync-operations-internal.openapi.yaml) 与
   [service-api 公开合同](../contracts/0023-service-api-data-operations.openapi.yaml)。
5. v1 使用版本化内部 HTTP，不新增跨服务事件平台。API outbox、Celery/Redis 只负责可靠交付或唤醒，
   不是命令或状态权威。

### 2. 全局同步执行槽

1. PostgreSQL 保存唯一 `global-sync` 槽，包含 `run_id`、`lease_owner`、`lease_until`、
   `heartbeat_at` 和单调 `fencing_token`。
2. dispatcher 在一个短事务内锁定该行、回收过期持有者、按稳定优先级/FIFO 选择一条 `queued` 子任务，
   原子标记 `running` 并递增 fencing token。
3. 同一时刻最多一个未过期槽持有者。`sync_run` 的 `running` 部分唯一约束作为第二道数据库防线。
4. worker 定期续租；只有当前 `run_id + fencing_token` 可写 checkpoint、publication 和终态。
   过期 worker 即使恢复，也不能提交数据或覆盖新 worker 状态。
5. 租约过期后 reaper 把原尝试标记为 `interrupted`，依据 retry policy 重回队列或终止，再释放执行槽。
6. queued 任务可立即取消；running 任务只进入 `cancel_requested`，worker 在 Provider 分页、
   质量门和数据库事务之外的安全点确认取消。系统不把“已请求取消”伪装为“已取消”。

### 3. 统一入口与批量语义

1. 自动计划、Web/API 手工请求、重试、恢复及保留的 CLI 都只提交 command，不直接调用同步用例。
2. 旧 CLI 迁为“提交并可选等待结果”的受控客户端；旧 Celery task 迁为 command handler 或兼容转发器。
3. 一个批量请求产生一个 parent command 和有序 child runs。机器合同上限为 100 个 target；
   运行环境可配置更低安全上限并由 preflight 返回。child runs 逐条竞争同一个全局槽，
   因而任何批量内部也不并行。
4. `full`、`incremental`、`date_range`、`observation_date` 是按数据集显式声明的能力：
   - `full` 使用该数据集冻结的历史边界，不把任意固定日期当作全域默认；
   - `incremental` 从成功 publication checkpoint 和版本化修订回看窗口计算；
   - `date_range` 使用包含端日期并按数据集日历、最大跨度和来源历史验证；
   - snapshot 数据只接受观察日，不能伪装为日期范围。
5. 提交前执行无副作用 preflight，返回支持模式、解析后的分区数、预计 Provider 调用量、日期/日历风险、
   当前队列和冲突；preflight 不是锁定、批准或执行成功保证。
6. parent command 提供权威详情查询，返回聚合状态和按提交顺序排列的 child runs。取消与重试必须显式指定
   `COMMAND` 或 `RUN`：整批取消会取消未开始 child 并合作式取消活跃 child；整批重试只复制可重试的失败、
   部分成功或中断 child，单个 child 操作不得误伤整批。动作回执同时返回原 target、targetStatus 和结果
   command；命令在生成 child 前被拒绝时，详情的 child runs 可以为空。回执的 `queuePosition` 只表示
   submit/retry 新命令首个 child 在受理事务快照中的位置；cancel 为 null，其余 child 位置从命令详情读取。
   取消动作结论与目标终态分开：只有目标进入 `CANCELLED` 才算取消达成；若 publication 已提交，
   记录 `cancel_too_late` 与 `operationResult=FAILED`，同时保留目标真实的 `SUCCEEDED` 状态和跳转。
7. run 详情中的 partitions 与 timeline 分别使用独立 cursor，默认 100、单页最多 200，并返回总数和
   nextCursor；checkpoint 只返回不含 Provider cursor、token 或原始 payload 的有界哈希摘要。
   来源绑定、健康规则和 preflight 警告等嵌套集合同样设上限，公开响应不得依赖截断猜测；公开 timeline
   把内部 `actorRef` 投影为 `ActorDisplay`。

### 4. 数据目录、来源与健康

1. `canonical_dataset` 继续作为稳定数据集身份；新增一对一 operational profile，声明显示名称、
   capability、支持模式、参数形状、freshness policy、计划能力和运行开关。仅建模的 `MODEL_ONLY`
   数据集明确返回 `freshnessStatus=NOT_APPLICABLE` 与 `freshnessPolicy=null`，不得伪造阈值。
2. 目录同时返回 `providerId` 与 `upstreamSource/sourceDataset`。AKShare、Tushare 等入口层不能替代
   东财、同花顺、交易所等真实来源层；每个 run 在受理时冻结来源、adapter 与方法学版本快照，历史详情
   不得用当前目录配置回填。
3. 最近同步、最近成功、最近 publication 和事实最大日期分别返回，禁止把“任务成功”解释成“数据新鲜”。
4. ingestion quality gate 与发布后 health evaluation 分离。健康检查绑定不可变 release/dataVersion，
   覆盖 freshness、完整性、唯一性、有效性、时序、身份、schema 和领域不变量。发布后 CRITICAL 只标记
   已发布版本为严重异常，不撤销或阻断既有 publication；只有发布前 quality gate 的 BLOCKED 阻止新版本发布。
   `HealthEvaluation` 只保存不可变评估事实；列表 summary 和详情另带查询时的 current open issue 投影及
   `issueProjectionAsOf`，不能因 ACK/RESOLVED 改写历史评估。详情的脱敏规则结果设有 500 条上限，
   开放问题以独立 cursor 分页、每页最多 100 条。单个或最多 100 个目标的主动健康检查生成
   `healthCheckId`；批次详情按原 target 顺序返回每项状态、绑定版本、evaluationId 或错误，API 用它持续对账。
5. 缺少权威交易日历时返回 `UNKNOWN`，不能把缺失日期判为休市；不同资金流方法学的差异只作观测，
   不能作为数值应相等的错误规则。
6. 目录分别返回来源可用性、观测数据状态、运行结果和健康结论；合法空集、异常空集、从未同步、
   来源不可用、同步失败与健康未知不得合并成一个“无数据”状态。freshness 状态、落后量和原因由
   data-sync 按版本化 policy 计算，Web 不自行推导。

### 5. 自动计划

1. 可变计划保存在 data-sync PostgreSQL，不把用户输入直接改写成 Celery beat 配置。v1 以
   `datasetCode` 作为唯一键，每个数据集至多一个计划；重复创建返回 409，更新不可改绑数据集。
2. 计划使用结构化频率、IANA 时区、当地执行时间、交易日历、misfire/coalesce policy 和乐观版本；
   默认时区为 `Asia/Shanghai`，但每条计划仍显式保存。
3. 数据集分别声明人工与计划支持模式。v1 自动计划禁止 `date_range`；`full/incremental` 使用
   `dateResolution=NONE`，`observation_date` 必须冻结为“计划本地日期”或“最近已完成交易日”解析策略。
   capability 必须按 mode 返回允许的版本化 target policy 及唯一默认项，Web 只从这些选项构造新计划。
4. 固定 scheduler tick 查找到期计划，并以稳定 idempotency key 向同一 command queue 投递。
   计划碰撞只会排队，不会并行。
5. 计划修改保存不可变 revision；禁用优先于删除。创建时 `scheduleId/expectedVersion` 必须同时为 null，
   更新时必须同时非 null 且版本匹配；不允许用半空组合绕过乐观锁或最后写入静默覆盖。

### 6. 权限与审计

1. 初始最小权限：
   - `ADMIN`、`SUPER_ADMIN` 可读取目录、运行、健康、计划和运维记录；
   - 只有 `SUPER_ADMIN` 可提交同步/健康检查、取消、重试、修改或启停计划。
2. `service-api` 在同一数据库事务中写入 `DataOperationSubmission`、API outbox 与 `AuditLog`：
   分别记录授权/交付意图、待投递 HTTP 请求和授权后的用户动作。data-sync 的 append-only operational event
   记录受理、排队、开始、心跳摘要、成功、部分成功、失败、取消和计划变更结果。
3. API 只向 data-sync 传递不透明 `actorRef`、角色快照、request/trace ID 和必要的原因摘要；
   不转发用户 JWT、账号、邮箱或敏感资料。
4. UI 的“操作记录”以 data-sync 执行结果为权威，并可通过 request ID 关联 API 安全审计。
   用户删除后仍保留不透明 actorRef，显示层使用“已删除用户”兜底。
   `authorityResource` 按动作固定映射：submit/retry 跟踪新 command，cancel 跟踪原 COMMAND/RUN target，
   健康检查和计划动作分别跟踪 health check 与 schedule。
   自动计划、legacy 和恢复命令没有 API submission，`submissionId=null`、交付状态为 `NOT_APPLICABLE`；
   API 把系统 actor 显示为“系统计划”“遗留任务”或“系统恢复”，不得误标成“已删除用户”。
5. 公开写请求按 `actorId + 用户 Idempotency-Key` 去重；相同 key 与相同请求返回原 submission，
   相同 key 与不同请求返回冲突。首次创建 submission 后，API 用 `submissionId` 确定性派生唯一内部 key，
   outbox 的所有投递与未知结果对账都复用该内部 key。禁止把不同用户可能相同的公开 key 直接透传到
   仅按调用服务分区的 data-sync 幂等域。
6. API 提交事务成功后一律先返回 `202 delivery=PENDING, queuePosition=null`，不能宣称 data-sync
   已受理。只有独立 dispatcher 使用 `FOR UPDATE SKIP LOCKED` 与有界 lease 投递，并始终复用
   submission 对应的内部 key；后续查询才能观察到 `ACCEPTED`、`REJECTED` 或 `DEAD_LETTER`。健康检查
   被接受后，reconciler 通过批次详情追踪全部目标到终态，不能只凭首次 202 判定成功。
7. 超过重试上限的 outbox 进入 `DEAD_LETTER` 并告警；`SUPER_ADMIN` 人工 replay 只重投原 outbox，
   不重新授权、不生成新内部 key。submission 通过 `submissionId/requestId` 与 data-sync 事件持续对账到终态。
8. API 在写 outbox 前只做身份、角色、DTO 结构、限流、公开幂等及 preflight 引用形状校验，不复制
   data-sync 的实时 capability、计划资格或资源状态权威。preflight 本身依赖 data-sync，不可用时返回 503；
   已持有有效 preflight 的提交及其他结构合法操作可先进入 PENDING，再由 data-sync 权威地 ACCEPTED 或 REJECTED。

## 失败与恢复不变量

- 投递、Provider 超时、限流、schema drift、质量阻断、健康评估、持久化、计划解析和取消分别保存稳定
  错误码、阶段和 retryable；
  不向 Web 暴露堆栈、供应商响应正文、raw URI 或凭据。
- 任务失败不推进 canonical checkpoint；质量阻断不替换上一 production publication。
- 空数据、来源不可用、同步失败和健康未知是不同状态。
- schedule/API/data-sync 暂时不可用时不绕过队列直调 Provider，也不授予 API 数据库凭据。
- API 已提交但下游结果未知时保留 `PENDING`；dispatcher 只能用原 submission 派生的内部幂等键重试，
  拒绝“换 key 再发一次”。
- 全局执行槽恢复只决定谁能执行，不能把未完成任务直接标记成功。

## 迁移与启用条件

1. Expand：data-sync 新增控制面表、索引和内部只读目录；API 新增 submission/outbox 表、dispatcher
   和终态对账任务；旧入口不变。
2. Shadow：所有旧 CLI/Celery/schedule 双写 command/run 账本，但仍由旧路径执行；核对状态和来源。
3. Enforce：逐 capability 切换到 dispatcher；publication/checkpoint 开始强制校验 fencing token。
4. Lock gate：AST/架构测试证明没有可绕过 handler 的生产入口，双 worker/CLI/schedule 故障注入证明
   `max(concurrent sync)=1`，才宣称满足全局串行。
5. UI/API 灰度：先只读，再验证 outbox 超时、重复投递与 DEAD_LETTER replay，开放单数据集手工任务，
   最后开放批量和计划编辑。
6. 回滚：关闭公开写操作并停止 dispatcher；保留 command/run/audit 历史。已经强制 fencing 的 capability
   不回退到可绕过全局槽的旧 writer，只能 roll forward 修复。

## 后果

- 任务启动可能排队，批量请求耗时增加，但同步互斥、来源配额和可恢复性获得统一保证。
- `service-data-sync` 增加控制面状态机、dispatcher、reaper 和计划持久化责任。
- `service-api` 增加权限、幂等、submission/outbox dispatcher、下游写操作可靠性、终态对账和审计投影责任，
  但仍不承担任务执行。
- 动态计划不再等同于修改环境变量或重启 scheduler；环境变量只保留全局功能开关与安全上限。
- 历史链路必须渐进迁移；在 Lock gate 通过前，界面只能标为“互斥迁移中”，不能承诺全局唯一运行。

## 未决事项

1. 首批开放写操作是否仅限 `SUPER_ADMIN`，或后续新增独立数据运维角色；当前安全默认仅 `SUPER_ADMIN`。
2. 各数据集 full 边界、修订回看窗口、freshness SLO、Provider 调用预算，以及不超过合同上限 100 的
   初始运行时批量上限。
3. 最终内部工作负载身份采用短期 JWT、mTLS 绑定 token 或服务网格身份；进入预发前必须与读取 token 分权。
4. 运维事件在线保留期。当前安全默认是不启用删除，建议基线为 365 天在线后受控归档。
5. 运行中强制终止的最大等待时间；当前只支持合作式取消，不引入不安全的进程 kill。
6. AKShare 等访问层背后各真实上游的许可、再分发边界与生产 SLA；证据未冻结前只标记研究或候选，
   不因技术链路可用自动批准为生产来源。

## 替代关系

本 ADR 细化 ADR-0002 中“手工同步、任务状态和运行时配置经版本化内部接口”的方向，不替代其数据所有权决策。
它不改变 ADR-0020/0023 的市场数据读取合同，也不把 Celery/Redis 提升为跨服务事实总线。
