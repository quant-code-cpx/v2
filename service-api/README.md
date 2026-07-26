# service-api

NestJS 11 单进程 API。当前包含 `UserModule`、`AuthModule`、`SectorMarketDataModule`、`RedisModule` 和 PostgreSQL 持久化。

技术方案：[API 服务基础架构与技术方案](../docs/service-api/0001-service-api-foundation/index.html)。
决策记录：[ADR-0005](../docs/decisions/0005-service-api-runtime-and-architecture.md)。

## 边界

- `UserModule`：用户资料、角色、状态、密码凭证、`securityVersion`。
- `AuthModule`：登录、JWT、Refresh Session、退出、Guard、Redis 安全限流。
- PostgreSQL：用户、凭证、会话、审计的唯一权威存储。
- Redis：登录/刷新限流、失败锁定、重放标记；不保存权威业务状态。
- `SectorMarketDataModule`：仅经 `service-data-sync` 内部 HTTP 契约读取已发布行业/概念目录与日、周、月原生 K 线；不直连同步数据库、不写 Redis 权威缓存、不接入分钟数据。
- 不含 Worker、Scheduler、队列、实时通信或其他业务模块。

Prisma schema 位于 `prisma/`，入口文件只定义 generator 与 datasource；数据模型按领域放在
`prisma/models/`。模型文件仍组成同一逻辑 schema，共用 Prisma Client 与 migration 历史。

`AuthModule → UserModule` 单向依赖。禁用、改密或角色变更递增 `securityVersion`，使旧 access token 与 refresh session 立即失效。

## 本地运行

要求：Docker Desktop；若在宿主机运行，还需要 Node.js 24 与 pnpm 11。

以下 Compose 命令在仓库根目录执行：

```bash
cp .env.example .env
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env \
  --profile api up --build
```

服务地址：`http://127.0.0.1:13000`。

- `GET /health`：进程存活。
- `GET /ready`：PostgreSQL 与 Redis 可用。

运行时不暴露 Swagger UI 或 OpenAPI JSON，避免绕过默认拒绝鉴权边界；机器可读合同以
[`docs/contracts/0002-user-access-management.openapi.yaml`](../docs/contracts/0002-user-access-management.openapi.yaml)
为准。

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
[
  { "userId": "00000000-0000-4000-8000-000000000001", "account": "market.admin" }
]
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
[10100 rollback.sql](prisma/migrations/20260726010100_apex_user_access/rollback.sql)。后者仅可在尚未写入 Apex 账号、
SUPER_ADMIN、软删除或新 Session family 状态时执行；PostgreSQL enum 值不会被破坏性移除。

生产 Compose 不在主机构建镜像，只接收 `SERVICE_API_IMAGE_REF` 和
`SERVICE_API_MIGRATION_IMAGE_REF` 两个 immutable digest；完整入口见根
[README](../README.md)。
