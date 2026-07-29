# 0020：高价值市场数据的跨服务访问方式

- 状态：Proposed
- 日期：2026-07-28
- 决策者：项目维护者（待评审）
- 所有者：`service-data-sync`
- 主要消费者：`service-api`

## 背景

指数成分、ETF、融资融券、沪深港通、主营构成、公司事件、交易事件和衍生品将由
`service-data-sync` 采集、规范化、质量判定并发布。`service-api` 需要把这些数据映射为对外业务
API，同时保持三个服务独立部署，不泄漏数据库结构、Provider 字段或供应商凭据。

已有 [ADR-0002](./0002-data-sync-ownership-and-access.md) 提议由 `service-data-sync` 独占数据存储，
通过版本化内部 HTTP 提供数据，事件平台只在出现多个消费者、分钟级推送或 HTTP 成为瓶颈后另行决策。
[0027 canonical data model](../service-data-sync/0027-canonical-data-model/index.html) 又把
`dataset_release`、`dataset_publication` 与不可变 `data_version` 设为读取边界。本 ADR 只裁决这批
市场数据如何跨服务消费，不接受任何生产实现或外部 API 变更。

## 已确认事实与约束

- `service-data-sync` 当前已有 FastAPI 内部查询接口；`service-api` 的 `DataSyncModule` 已有 Bearer
  认证、超时、运行时 DTO 校验和 Problem 映射模式，但现有市场数据合同均使用 `GET`。
- `service-api` 当前没有跨服务 durable broker、schema registry、transactional outbox、inbox、
  DLQ、事件重放或完整市场数据读模型。Celery/Redis 是同步任务基础设施，不是可直接复用的跨服务总线。
- 当前内部认证是单一静态 service bearer；它不能等同于最终的短期工作负载身份。当前游标签名材料也
  不能与 service bearer 复用。
- 所有新增 `service-api` 业务与运维 HTTP 路由只能使用 `POST`。本方案把同一约束主动应用到新增内部
  市场数据端点；已实现的旧内部 `GET` 合同保持原样。
- `service-api` 不得读取 data-sync 数据库或对象存储，不得访问 Provider；`service-web` 只能访问
  `service-api`。

## 候选方案

| 方案 | 一致性与时点语义 | 运行复杂度 | 查询能力 | 本次结论 |
|---|---|---|---|---|
| 共享数据库 / 只读账号 | 表结构成为隐式合同；迁移和权限耦合 | 表面低、长期高 | SQL 灵活但绕过发布与质量边界 | 排除 |
| 版本化内部 HTTP 查询 | 请求可固定 `dataVersion`、`asOf`、`knownAt`；分页可绑定版本 | 与现有模式一致；需超时、限流、熔断 | 适合按证券、时间、字段的交互查询 | **推荐 v1** |
| 完整事件 + API 自有读模型 | 可重放，但顺序、修订、删除、PIT 投影均需额外协议 | 当前缺失 broker/outbox/inbox/DLQ/DR | 读快，写放大和双存储显著 | 本次不采用 |
| 发布通知事件 + HTTP 拉取 | 事件只通知版本，事实仍由 HTTP 读取 | 比纯 HTTP 多一套关键基础设施 | 适合多个消费者降低轮询 | 延后新 ADR 评估 |

## 拟议决策

若本 ADR 获接受，第一版采用 **POST-only 的版本化内部 HTTP 查询，不引入事件平台，也不在
`service-api` 建立完整市场数据读模型**。

1. `service-data-sync` 继续拥有 canonical 数据、发布、质量、来源与修订语义。
2. 内部合同只新增：
   - `POST /internal/v1/market-data/datasets/search`：发现可查询数据集、字段白名单、口径、覆盖与限制；
   - `POST /internal/v1/market-data/query`：按一个 dataset、身份、业务时间、`asOf`、`knownAt`、
     `dataVersion`、方法学、质量状态、投影、排序和游标读取。
3. 机器合同为 [data-sync-market-data-v1.yaml](../contracts/data-sync-market-data-v1.yaml)，状态保持
   `Proposed`；在 ADR 接受且实现、合同测试、运行门禁通过前，不是可调用的生产接口。
4. `service-api` 通过新的 `MarketDataAccessClient` 消费合同，再映射为领域化公开 DTO。公开 API
   不暴露通用 dataset 查询器，不直接复用内部响应，也不暴露 Provider code、raw URI、数据库 ID 或
   内部质量诊断。
5. 查询是无副作用、retry-safe 的读取操作。幂等性由规范化请求、显式或服务端选定的不可变
   `dataVersion` 和版本绑定游标保证，不创建服务端幂等记录。
6. `selection.knownDataVersion` 是私有应用层新鲜度提示；相同版本可返回 `204`。它不是
   `If-None-Match`，不声明共享缓存对 POST 的标准语义。
7. 合法查询没有事实记录时返回 `200` 空页；发布不存在返回 `404`；未知错误、响应 schema drift、
   上游内部 `401/403` 均由 `service-api` 对外 fail-closed 为 `503` 并告警，避免泄漏内部拓扑。

## 数据与时点不变量

- 一页结果只来自一个 dataset schema、一个 `dataVersion` 和一个方法学版本；不得在分页之间静默切换。
- `PUBLIC_PIT` 同时要求 UTC 的 `asOf` 与 `knownAt`，并只返回
  `public_usable_at <= knownAt` 且在有效区间内的修订。
- 每条记录返回 `publicUsableAt` 和 `availabilityBasis`。只有日期而没有时刻时采用保守可见规则；
  仅能证明观察时间时标为 `OBSERVED_ONLY`。
- `OPERATIONAL_REPLAY` 是受限审计能力，不等于公开 PIT，不得把后采集记录伪装为历史公开可见。
- `PASSED`、显式请求允许的 `WARNED` 才可读取；隔离、失败或未知质量状态一律不出服务。
- 来源摘要表示真实发布者和上游数据集，不表示 Adapter 或聚合库；同名不同来源、口径或粒度不得静默混算。
- 游标由独立 HMAC keyring 签名，绑定调用方、规范化请求、排序、dataset schema 和 `dataVersion`，
  有界 TTL，支持 current/previous key 与 `keyId` 轮换；不得使用 service bearer 作为签名密钥。

## 版本治理

五个版本轴相互独立：

| 版本轴 | 例子 | 变化规则 |
|---|---|---|
| HTTP URI major | `/internal/v1` | 破坏性传输或资源语义变化进入 `/v2` |
| 合同 SemVer | `info.version: 1.0.0` | 记录机器合同发布；不能代替 URI major |
| dataset schema | `index.constituent.membership.reported` + `schemaVersion: 1` | 稳定 code 不嵌版本；字段类型、单位、空值或时间语义变化升 schema major |
| publication | UUID `dataVersion` | 每个不可变发布唯一；不表达 schema 兼容性 |
| 方法学 | `methodology.version` | 派生算法、复权、连续合约或口径变化独立升级 |

字段删除、必填项增加、字段类型/单位/空值/时区/可见性语义变化、枚举收窄、排序或游标语义变化、
权限收紧以及错误语义变化均视为 breaking change。Proposed 兼容假设是新旧 major 并行至少 90 天且
至少两个稳定发布周期，并在旧版连续 30 天调用量为零后才删除；最终期限由实施评审确认，不能把业务审批
当技术门禁。

## 可靠性与迁移

- Proposed 起始预算：单次客户端总 deadline `5s`，首次尝试最多 `2s`；只对 network、timeout、
  `502/503/504` 在总预算内做至多一次 `50–150ms` full-jitter 重试。`429` 不自动重试，只透传有界
  `Retry-After`；`400/401/403/409` 不重试。
- 熔断器按 dataset 成本桶隔离，避免衍生品或批量 PIT 查询拖垮低成本最新查询。阈值必须由压测校准，
  文档中的初值不是已满足 SLO 的事实。
- 初始合同上限：请求体 `64KiB`、一个 dataset、100 个身份、64 个字段、3 个排序键、游标 2048
  字节、默认页 100 / 最大页 500、响应 `2MiB`、日频默认窗口 366 天。
- 已有 26 条内部 `GET` 路由与旧客户端继续兼容运行，不改 method/path。灰度期先 fixture，再 shadow
  双读对比 rows、业务时间和 `dataVersion`，游标绝不跨协议。
- 只有新 POST 返回 `404/405/501`、明确表明能力尚未部署时，客户端才可在灰度期有界回退旧 GET；
  `400/401/403/409/429/5xx` 不回退。公开路由通过 dataset allowlist 按 `1% → 10% → 50% → 100%`
  放量。
- 回滚只切回旧 client 或关闭新领域路由；不可变 publication 与历史数据不回写、不删除。无安全旧路径时
  返回 `503`，不绕过 data-sync 直连数据库或 Provider。

## 事件演进触发器

下列任一信号只触发“创建新 ADR 并实测评估”，不会自动授权事件实现：

1. 独立生产消费者达到 3 个及以上，并持续 30 天；
2. 已批准的发布到可见 P95 目标低于 60 秒，而 HTTP 轮询在容量测试后仍无法满足；
3. 连续 5 个交易日，去除无效轮询后目录/版本探测仍占 data-sync 读取容量 20% 以上；
4. 完成查询索引、分页和私有缓存优化后，HTTP 查询仍连续 5 个交易日违反已批准 P95/SLO。

后续若选择“通知 + 拉取”，候选事件仅携带 dataset、partition、`dataVersion`、`publishedAt`，
不得携带完整事实或 Provider 载荷。新 ADR 必须先设计 broker、transactional outbox、schema
registry/AsyncAPI、每键顺序、至少一次投递、保留与重放、DLQ、消费者 inbox/幂等、读模型和灾备演练；
本 ADR 不接受 topic、保留期或事件 schema。

## 技术硬门禁

| 门禁 | 准入条件 | 可验证证据 | 验证人 | 退出条件 / 技术兜底 |
|---|---|---|---|---|
| G1 合同完整性 | 进入联调前 | OpenAPI 3.1.1 可解析、ref 全可解、operationId 唯一、paths 仅 POST、八域 typed oneOf 合同测试 | 两服务负责人 + QA | 全部通过；否则停用新 client，旧合同继续 |
| G2 身份与密钥隔离 | 进入预发前 | TLS、网络 allowlist、独立 service token 轮换、日志脱敏；游标签名独立 current/previous keyring 的负向与轮换测试 | 安全验证人 + 两服务负责人 | 无共享签名材料、无 token 泄漏、旧 key 可控过渡；否则 fail-closed |
| G3 PIT 与质量 | 开放 PIT/回放前 | 八域 golden fixture 验证 `asOf`、`knownAt`、`publicUsableAt`、修订、质量隔离及未来字段排除 | 数据 QA + 量化验证人 | 零未来泄漏、零跨版本混页；否则该 dataset 只开放 latest 或禁用 |
| G4 恢复与过载 | 生产放量前 | timeout/retry/429/熔断/2MiB 截断/中途故障注入；证明无部分页、无重试风暴 | SRE + 两服务负责人 | 错误映射、总 deadline、熔断恢复达到压测基线；否则缩窗/限页/关闭高成本域 |
| G5 边界不可绕过 | 每次发布前 | 依赖与部署检查证明 API 无 data-sync DB、对象存储和 Provider 凭据；web 仅指向 API | 架构验证人 + 安全验证人 | 零直连；发现即阻断并撤销凭据 |
| G6 来源与再分发 | 某字段公开前 | 字段级来源、单位、口径、许可/公开披露证据与对外投影清单 | 数据负责人 + 合规验证人 | 只开放证据充分字段；否则字段留在内部或整域关闭 |

## 非技术风险与跟进

| 跟进项（不阻断方案评审） | 负责人 | 时间点 | 技术兜底 |
|---|---|---|---|
| 最终用户套餐、PIT、批量与衍生品权限定价未定 | 产品负责人 | 实施规划前 | 默认只开放低成本 latest；PIT/bulk/derivatives scope fail-closed |
| 各来源再分发授权确认可能滞后 | 数据采购/合规 | dataset 上线前 | 使用官方公开字段；限制投影或仅内部研究 |
| SLO 和容量预算尚无生产基线 | SRE | 首个 P0 灰度前 | 使用保守窗口/页限额与 dataset allowlist，压测后再调参 |
| 新旧客户端迁移排期未定 | 两服务负责人 | 实施计划评审 | 旧 GET 合同保持，不共享游标，不删除历史 |

## 后果

- 数据所有权、修订、PIT 和质量语义集中在 `service-data-sync`，`service-api` 不依赖物理表。
- `service-api` 增加一次网络跳转和客户端可靠性责任，但不承担同步任务、Provider 或双写读模型。
- HTTP 查询的可用性成为业务 API 的下游依赖，需要明确限额、观测、降级与错误映射。
- 事件推送延后，避免在没有 outbox、重放和灾备能力时制造第二份不一致事实。
- 通用内部合同降低八域重复传输设计，但外部仍保持领域 DTO，防止把内部 schema 变成用户 API。

## 关联方案

- [0028 data-sync 数据访问合同方案](../service-data-sync/0028-data-access-contract/index.html)
- [0005 service-api 市场数据访问影响方案](../service-api/0005-market-data-access-impact/index.html)
- [拟议机器合同](../contracts/data-sync-market-data-v1.yaml)
- [0027 canonical data model](../service-data-sync/0027-canonical-data-model/index.html)

## 未决事项

1. Proposed 初始延迟、容量、缓存 TTL 和熔断阈值需要用 P0 数据量压测校准。
2. 工作负载身份最终采用短期 JWT、mTLS 绑定 token 或服务网格身份，需在安全实施设计中定稿。
3. 各公开业务路由的 entitlement 组合是产品策略；技术默认必须 fail-closed。
