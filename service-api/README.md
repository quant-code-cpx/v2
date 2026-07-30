# service-api

NestJS 11 单进程 API。当前包含 `UserModule`、`AuthModule`、`AuditModule`、`StockModule`、
`IndustryModule`、`MoneyFlowModule`、`MarketOverviewModule`、`StockConnectModule`、
`DataSyncModule`、Redis 和 PostgreSQL 持久化。

技术方案：[API 服务基础架构与技术方案](../docs/service-api/0001-service-api-foundation/index.html)。
决策记录：[ADR-0005](../docs/decisions/0005-service-api-runtime-and-architecture.md)。

## 边界

- 所有公开业务路由和 `/health`、`/ready` 运维路由仅使用 `POST`。禁止在 Controller 中声明其他
  HTTP method；CORS preflight 由框架处理，调用 `service-data-sync` 的出站 method 不受此限制。
- `UserModule`：用户资料、角色、状态、密码凭证、`securityVersion` 和角色范围统计。
- `AuthModule`：登录、JWT、Refresh Session、退出、本人 Session family 管理、Guard、Redis 安全限流。
- `AuditModule`：仅向 `SUPER_ADMIN` 提供 action registry 驱动的脱敏审计列表与详情。
- PostgreSQL：用户、凭证、会话、审计的唯一权威存储。
- Redis：登录/刷新限流、失败锁定、重放标记；不保存权威业务状态。
- `StockModule`：提供证券目录、详情、上市生命周期，以及个股日/周/月原生行情、复权因子、公司行动、公司概况、
  财务报表、来源与平台派生指标和历史估值查询。
- `IndustryModule`：提供行业/概念目录、日/周/月原生 K 线、固定 release 的板块成分观测，
  `post_close_observation` EOD 横截面与排行，以及申万三级 taxonomy、父级闭包和估值观察。
- `MoneyFlowModule`：提供已发布方法学目录、个股/板块/市场日频序列和供应商滚动排行；不可发布的
  research 方法学 fail-closed。
- `MarketOverviewModule`：提供单一市场首页 complete bundle、指数日线、全市场证券/资金流排行、交易日历、
  板块强弱，以及申万日线、正式成分和逐字段估值；只读取 data-sync publication，不在请求线程拼接横截面。
- `StockConnectModule`：只代理已批准的真实 bundle publication；保留币种、availability、lineage、
  来源 publication 与观察时间，不从成交额推导净额，也不把官方活跃榜描述为全市场排行。
- `DataSyncModule`：集中装配 `service-data-sync` 内部 HTTP Client 与运行时合同校验；不包含同步任务、
  供应商 SDK 或权威数据持久化。
- `DataOperationsModule`：实现数据运维公开控制面；只通过 `DataSyncModule` 调用 data-sync 内部 POST
  接口，在 API PostgreSQL 中可靠保存 Submission、Outbox 与审计，不直连 data-sync 数据库、Provider
  或 Redis 业务状态。
- 不含 Worker、Scheduler、队列、实时通信或其他业务模块。

Prisma schema 位于 `prisma/`，入口文件只定义 generator 与 datasource；数据模型按领域放在
`prisma/models/`。模型文件仍组成同一逻辑 schema，共用 Prisma Client 与 migration 历史。

`AuthModule → UserModule` 单向依赖。禁用、改密或角色变更递增 `securityVersion`，使旧 access token 与 refresh session 立即失效。

## 测试归属

每个 Nest module 自建 `test/`，Controller、Service、DTO、Guard 等测试集中到 module 内部：

```text
src/apps/auth/
├── auth.controller.ts
├── auth.service.ts
└── test/
    ├── auth.controller.spec.ts
    └── auth.service.spec.ts
```

`config/`、`data-sync/`、`lifecycle/`、`shared/` 与 `scripts/` 下的独立功能同样使用自身
`test/`。禁止把 `service.ts` 与 `service.spec.ts` 或 `service.test.ts` 放在同一目录；跨 module
集成测试才可进入服务级专用测试树。

## 本地运行

要求：Docker Desktop；若在宿主机运行，还需要 Node.js 24 与 pnpm 11。

以下 Compose 命令在仓库根目录执行：

```bash
cp .env.example .env
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env \
  --profile api up --build
```

服务地址：`http://127.0.0.1:13000`。

- `POST /health`：进程存活。
- `POST /ready`：PostgreSQL 与 Redis 可用。

运行时不暴露 Swagger UI 或 OpenAPI JSON，避免绕过默认拒绝鉴权边界；用户访问与账户安全机器合同以
[`docs/contracts/0002-user-access-management.openapi.yaml`](../docs/contracts/0002-user-access-management.openapi.yaml)
和
[`docs/contracts/0017-service-api-account-security-operations.openapi.yaml`](../docs/contracts/0017-service-api-account-security-operations.openapi.yaml)
为准。

方案 0011 个股市场数据公开路由均为 POST：

- `/api/v1/equities/{exchange}/{symbol}/bars`
- `/api/v1/equities/{exchange}/{symbol}/adjustment-factors`
- `/api/v1/equities/{exchange}/{symbol}/corporate-actions`
- `/api/v1/equities/{exchange}/{symbol}/company-profile`

前三条列表路由支持最长 1024 字符的不透明游标；游标与同一发布版本和查询范围绑定。机器契约见
[`0019-service-api-equity-market-data.openapi.yaml`](../docs/contracts/0019-service-api-equity-market-data.openapi.yaml)。

### 通用市场数据查询

个人部署可通过认证后的 `POST /api/v1/market-data/query` 读取方案 0019–0029 已注册的 typed canonical dataset。
它只代理到 data-sync 的内部 POST query，字段、筛选、时间范围、PIT 与分页仍由 data-sync 的严格 allowlist
校验；不会直连同步数据库、对象存储或 AKShare。已注册 dataset 尚无 publication 或来源暂不可用时，响应为
`200`、`records: []` 和 `meta.availability=SOURCE_UNAVAILABLE`，前端应正常显示空状态。完整边界见
[ADR-0023](../docs/decisions/0023-personal-market-data-query-gateway.md)。

方案 0012、0016、0017 的公开 POST 契约分别为：

- [`0021-service-api-sw-sector.openapi.yaml`](../docs/contracts/0021-service-api-sw-sector.openapi.yaml)：3 条申万读取路径。
- [`0014-service-api-financial-valuation.openapi.yaml`](../docs/contracts/0014-service-api-financial-valuation.openapi.yaml)：4 条财务与估值路径。
- [`0016-service-api-daily-money-flow.openapi.yaml`](../docs/contracts/0016-service-api-daily-money-flow.openapi.yaml)：5 条资金流路径。

POST-only 方法与动作路径规则见
[ADR-0018](../docs/decisions/0018-service-api-post-only-http-method.md)。读取类 POST 命中
`If-None-Match` 时返回 `204`，不会返回仅适用于条件 GET/HEAD 的 `304`。

### 市场概览与行业板块

公开合同见
[`0027-service-api-market-overview.openapi.yaml`](../docs/contracts/0027-service-api-market-overview.openapi.yaml)。
市场概览公开入口全部使用 POST，请求参数放在 JSON body；API 通过带 Bearer、`X-Request-Id`、
`If-None-Match` 的内部资源式 GET 读取 `service-data-sync`，严格校验响应 schema、强 ETag 与
`X-Data-Version`。公开路径为：

- `/api/v1/market/overview`
- `/api/v1/market/indices/{indexId}/bars`
- `/api/v1/market/equities/rankings`
- `/api/v1/market/money-flow/equity-rankings`
- `/api/v1/market/calendar/query`
- `/api/v1/market/sectors/strength`
- `/api/v1/market/sectors/money-flow-rankings`
- `/api/v1/market/industries/sw/{code}/bars`
- `/api/v1/market/industries/sw/{code}/constituents`
- `/api/v1/market/industries/sw/{code}/valuation`

十条能力返回 200 的共同前置是对应真实来源 publication 已完成、通过质量门且严格合同校验成功；缺失、
不完整、跨版本或合同漂移一律 fail-closed 为 404、424 或 503，不使用 fixture、零值或跨数据集拼接兜底。
首页只返回 `market.overview-bundle.eod` 的一个完整 publication；指定日期缺失为 404，latest 指针不存在或
下游合同漂移为 503，绝不返回 200 加零值。内部 304 在公开 POST 边界映射为无响应体的 204。latest 首页的
ETag 同时绑定 EOD 版本与分钟桶市场状态，精确历史读取固定返回 `historical_snapshot` 收盘状态；两者的
`X-Data-Version` 都只表示原子 EOD bundle。质量来源用 `external` 与 `derived` 区分 Tushare 外部事实和
`quant-v2-derivation` 写时派生，不能把平台计算伪装成供应商直报。

申万前收盘若由同一 `sw_daily` 行复算会携带派生方法学；PE_TTM 与股息率来源未报告时固定返回
`availability=source_not_reported` 和 `value=null`，不能补零。主要指数路径只接受
`sse-composite`、`szse-component`、`csi-300`、`chinext` 四个稳定 ID；申万行情的
`1d`、`1w`、`1mo` 均读取同步阶段已物化 publication，API 不做请求时聚合。板块资金流排行固定保留
`moneyflow_ind_dc` 的东财来源与方法学标签，不以板块涨跌排行替代。申万成分明确返回
`latest_revision_effective_interval`、知识截止与来源观测时刻，不能描述为“当时可知”历史快照。

### 沪深港通中心

沪深港通五条公开读取路由均为 POST：

- `/api/v1/market/stock-connect/readiness/query`
- `/api/v1/market/stock-connect/overview/query`
- `/api/v1/market/stock-connect/channels/query`
- `/api/v1/market/stock-connect/active-securities/query`
- `/api/v1/market/stock-connect/securities/context/query`

它们使用 `operationId + 规范请求体 + dataVersion` 生成 representation 强 ETag；业务响应的
`X-Data-Version` 返回 bundle 版本，readiness 响应返回删除顶层 `dataVersion`
后规范序列化正文的 SHA-256，并由 API 独立重算。活跃证券请求必须携带父 overview/channel 的
`parentPublicationDataVersion`，父版本变化或旧游标分别返回 409，不会跨 publication 拼接。
readiness 没有持久化证据时返回 `READINESS_NOT_OBSERVED`，不会从业务 409 或当前时间猜测休市/失败。
完成 data-sync、API 与 Web 的真实链路验收前保持 `STOCK_CONNECT_API_ENABLED=false`。

### 数据运维控制面

数据运维公开 POST 契约见
[`0023-service-api-data-operations.openapi.yaml`](../docs/contracts/0023-service-api-data-operations.openapi.yaml)，
下游内部 POST 契约见
[`0022-data-sync-operations-internal.openapi.yaml`](../docs/contracts/0022-data-sync-operations-internal.openapi.yaml)。
首次主动写操作固定返回 `202`、`deliveryStatus=PENDING` 和 `operationResult=UNKNOWN`；请求线程仅在同一
API PostgreSQL 事务写入 Submission、冻结 Outbox 与审计记录，绝不 best-effort 直调 data-sync。

根 Compose profile 会启动独立 dispatcher。宿主机排障时可在完成 migration 后运行：

```bash
pnpm data-operations:dispatch
```

该进程使用 PostgreSQL `FOR UPDATE SKIP LOCKED`、lease 与指数退避投递冻结 outbox，并持续对账已接受的
COMMAND、RUN、HEALTH_CHECK 和 SCHEDULE 权威资源。它与 API 进程共用 `DATABASE_URL`、
`DATA_SYNC_INTERNAL_BASE_URL` 以及三枚服务身份：既有内部路由使用
`DATA_SYNC_INTERNAL_API_BEARER_TOKEN`，0022 只读路由使用
`DATA_SYNC_INTERNAL_READ_API_BEARER_TOKEN`，0022 主动操作使用
`DATA_SYNC_INTERNAL_OPERATIONS_API_BEARER_TOKEN`。开发与测试环境可将后两项回退为既有 token；生产环境必须
分别注入三枚凭据，读凭据不能投递写操作。

普通内部读取继续使用 2 秒单次预算；Provider 全窗预检使用独立
`DATA_SYNC_INTERNAL_PREFLIGHT_TIMEOUT_MS`（默认 310000 ms）且不会自动重复整窗探针。若
service-data-sync 的全窗预检预算提高到 3600 秒上限，API 和入口网关必须同步设置至少 3610000 ms。

`DEAD_LETTER` 不开放浏览器 route。仅在受控维护窗口，由活动 `SUPER_ADMIN` 使用原 Submission ID 和明确确认值
重投同一条冻结 outbox：

```bash
DATA_OPERATIONS_REPLAY_SUBMISSION_ID="<submission-uuid>" \
DATA_OPERATIONS_REPLAY_ACTOR_ID="<active-super-admin-uuid>" \
DATA_OPERATIONS_REPLAY_CONFIRMATION="REPLAY_DEAD_LETTER" \
pnpm data-operations:replay-dead-letter
```

runbook 会在写事务内再次验证操作者仍为活动 `SUPER_ADMIN`，只把原 outbox 重置为 `PENDING`，不会创建新的
Submission、payload 或内部幂等键；审计仅保存下游 key 的 HMAC 摘要。

### 自动初始化超级管理员

独立 `service-api-bootstrap` job 在 `service-api-migrate` 完成 Prisma schema migration 后、`api` 服务启动前执行
超级管理员初始化；应用进程本身不执行 schema migration 或创建账号。Compose 使用仓库根目录 `.env`，宿主机直接运行
使用 `service-api/.env`。在首次运行前设置 `BOOTSTRAP_ADMIN_ACCOUNT`（5–32 位自定义账号，不是邮箱）与
`BOOTSTRAP_ADMIN_PASSWORD`：

```dotenv
BOOTSTRAP_ADMIN_ACCOUNT=replace-with-super-admin-account
BOOTSTRAP_ADMIN_PASSWORD=replace-with-strong-super-admin-password-2026
```

初始化在同一个 PostgreSQL advisory lock 与串行化事务内完成：

- 已有 ACTIVE `SUPER_ADMIN`：安全 no-op；不改密、不覆盖、不新增账号，即使两个 bootstrap 变量缺失也不失败。
- 用户库为空且没有 ACTIVE `SUPER_ADMIN`：两个变量必须同时存在，job 才会创建配置账号为唯一超级管理员，并写入脱敏审计。
- 禁用或删除的 `SUPER_ADMIN`、任意既有普通用户/管理员、变量缺失或账号冲突：bootstrap job 失败；不会提升、覆盖或修改既有账号。

因此 `service-api` 服务只会在 migration 与 bootstrap job 都成功后启动。生产环境从 secret 注入这两个值；不要将真实超级管理员凭证写入 `.env.example`、代码或日志。宿主机开发需显式执行：

```bash
pnpm prisma:deploy && pnpm bootstrap:admin
```

### 已有用户库迁移与 SUPER_ADMIN 提升

已有用户库不得从邮箱推导账号。先在维护窗口停掉旧 API，为每个既有 `users.id` 显式分配唯一账号，写入仅含
`userId` 与 `account` 的 JSON 数组（不提交仓库、不记录到日志）：

```json
[{ "userId": "00000000-0000-4000-8000-000000000001", "account": "market.admin" }]
```

先执行准备命令；它在串行化事务中锁定 `users` 表，拒绝缺失、重复、email 字段、格式错误和半完成映射，
只写 nullable `account`/`normalized_account`。随后执行 Prisma migration，migration 会再次验证映射并收紧约束：

```bash
LEGACY_ACCOUNT_MAPPING_FILE=/secure/path/legacy-accounts.json \
DATABASE_URL='postgresql://…' \
pnpm prepare:legacy-accounts
pnpm prisma:deploy
```

Compose 环境先只启动 PostgreSQL 和 Redis，再以只读挂载提供映射文件：

```bash
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env --profile api up -d \
  service-api-postgres service-api-redis
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env --profile api run --rm --no-deps \
  -v /secure/path/legacy-accounts.json:/run/secrets/legacy-accounts.json:ro \
  -e LEGACY_ACCOUNT_MAPPING_FILE=/run/secrets/legacy-accounts.json \
  service-api-migrate node dist/scripts/prepare-legacy-accounts.js
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env --profile api run --rm --no-deps \
  service-api-migrate ./node_modules/.bin/prisma migrate deploy
```

自动 bootstrap 故意拒绝含任何既有用户的库，避免把配置账号错误提升到非空环境。运维明确决定沿用某个既有
ACTIVE ADMIN 时，才可运行以下受控恢复命令；它不是普通 API，必须同时提供确认值，成功后会撤销该账号全部 Session、
递增 `securityVersion` 并写审计。重复运行、非 ACTIVE ADMIN 或已有 SUPER_ADMIN 都会失败：

```bash
PROMOTE_SUPER_ADMIN_ACCOUNT=market.admin \
PROMOTE_SUPER_ADMIN_CONFIRM=PROMOTE_ACTIVE_ADMIN \
pnpm promote:super-admin
```

Docker-only deployments run the same command through the migration image; pass both values only to this one-off container:

```bash
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env --profile api run --rm --no-deps \
  -e PROMOTE_SUPER_ADMIN_ACCOUNT=market.admin \
  -e PROMOTE_SUPER_ADMIN_CONFIRM=PROMOTE_ACTIVE_ADMIN \
  service-api-migrate node dist/scripts/promote-existing-admin.js
```

## 宿主机开发

以下命令在 `service-api/` 目录执行：

```bash
cp .env.example .env
corepack pnpm@11.17.0 install
pnpm prisma:deploy
pnpm start:dev
```

`.env.example` 使用宿主机端口 `15433`、`16380`；先启动基础设施：

```bash
docker compose -f ../compose.yaml -f ../compose.dev.yaml --env-file ../.env \
  --profile api-infra up -d
```

根 `.env` 是 Compose 配置源；本目录 `.env` 只供宿主机直接运行 NestJS。两者均禁止提交，
对应可提交假值分别位于根 `.env.example` 和本目录 `.env.example`。

## 审计保留与索引发布

`AuditLog` 在线保留 90 天，不归档。部署编排每日以独立一次性进程执行以下命令；任务使用 PostgreSQL
advisory lock、每批最多删除 5,000 行，不写清理动作审计：

```bash
DATABASE_URL='postgresql://…' pnpm audit:retention
```

普通 migration 前可显式运行容量门禁。迁移 SQL 自身也会在 `audit_logs` 达到 100 万行且目标索引不存在时
失败，因此即使编排没有调用预检也不能绕过：

```bash
DATABASE_URL='postgresql://…' pnpm audit:index-gate
pnpm prisma:deploy
```

达到门禁时，在维护窗口逐条、事务外执行
[并发索引 runbook](prisma/runbooks/account-security-indexes-concurrently.sql)，随后重新执行
`pnpm prisma:deploy` 登记 migration。回滚使用
[并发索引回滚 runbook](prisma/runbooks/account-security-indexes-concurrently.rollback.sql)；生产 API
启动流程不执行 migration 或保留期清理。

## 验证

```bash
pnpm format:check
pnpm lint
pnpm typecheck
pnpm test
pnpm build
docker build --target test --tag quant-v2/service-api:test .
docker build --tag quant-v2/service-api:local .
```

迁移在独立的 `service-api-migrate` 容器中执行，应用启动不会自动修改生产 schema。初始 migration 的人工回滚脚本位于
[00000 rollback.sql](prisma/migrations/20260726000000_initial_user_auth/rollback.sql)；Apex 用户访问 migration 的人工回滚脚本位于
[10100 rollback.sql](prisma/migrations/20260726010100_apex_user_access/rollback.sql)；账户安全查询索引回滚位于
[00000 account security rollback.sql](prisma/migrations/20260728000000_account_security_operations/rollback.sql)。
数据运维控制面 migration 的受控人工回滚位于
[data operations rollback.sql](prisma/migrations/20260729000000_data_operations_control_plane/rollback.sql)；
仅可在尚未写入 Submission、Outbox、搜索游标与新增审计 action 的维护窗口执行。
用户访问回滚仅可在尚未写入 Apex 账号、SUPER_ADMIN、软删除或新 Session family 状态时执行；
PostgreSQL enum 值不会被破坏性移除。

生产 Compose 不在主机构建镜像，只接收 `SERVICE_API_IMAGE_REF` 和
`SERVICE_API_MIGRATION_IMAGE_REF` 两个 immutable digest；完整入口见根
[README](../README.md)。
