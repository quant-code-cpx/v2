# service-api

NestJS 11 单进程 API。当前包含 `UserModule`、`AuthModule`、`AuditModule`、`StockModule`、
`IndustryModule`、`MoneyFlowModule`、`DataSyncModule`、Redis 和 PostgreSQL 持久化。

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
- `DataSyncModule`：集中装配 `service-data-sync` 内部 HTTP Client 与运行时合同校验；不包含同步任务、
  供应商 SDK 或权威数据持久化。
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

方案 0012、0016、0017 的公开 POST 契约分别为：

- [`0021-service-api-sw-sector.openapi.yaml`](../docs/contracts/0021-service-api-sw-sector.openapi.yaml)：3 条申万读取路径。
- [`0014-service-api-financial-valuation.openapi.yaml`](../docs/contracts/0014-service-api-financial-valuation.openapi.yaml)：4 条财务与估值路径。
- [`0016-service-api-daily-money-flow.openapi.yaml`](../docs/contracts/0016-service-api-daily-money-flow.openapi.yaml)：5 条资金流路径。

POST-only 方法与动作路径规则见
[ADR-0018](../docs/decisions/0018-service-api-post-only-http-method.md)。读取类 POST 命中
`If-None-Match` 时返回 `204`，不会返回仅适用于条件 GET/HEAD 的 `304`。

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
用户访问回滚仅可在尚未写入 Apex 账号、SUPER_ADMIN、软删除或新 Session family 状态时执行；
PostgreSQL enum 值不会被破坏性移除。

生产 Compose 不在主机构建镜像，只接收 `SERVICE_API_IMAGE_REF` 和
`SERVICE_API_MIGRATION_IMAGE_REF` 两个 immutable digest；完整入口见根
[README](../README.md)。
