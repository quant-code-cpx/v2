# service-api

NestJS 11 单进程 API。当前只有 `UserModule`、`AuthModule`、`RedisModule` 和 PostgreSQL 持久化。

技术方案：[API 服务基础架构与技术方案](../docs/service-api/0001-service-api-foundation/index.html)。
决策记录：[ADR-0005](../docs/decisions/0005-service-api-runtime-and-architecture.md)。

## 边界

- `UserModule`：用户资料、角色、状态、密码凭证、`securityVersion`。
- `AuthModule`：登录、JWT、Refresh Session、退出、Guard、Redis 安全限流。
- PostgreSQL：用户、凭证、会话、审计的唯一权威存储。
- Redis：登录/刷新限流、失败锁定、重放标记；不保存权威业务状态。
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
- `GET /openapi`：Swagger UI。
- `GET /openapi-json`：OpenAPI JSON。

首次创建管理员可在 `service-api/.env` 中设置 `BOOTSTRAP_ADMIN_EMAIL`、`BOOTSTRAP_ADMIN_PASSWORD`，然后运行：

```bash
pnpm bootstrap:admin
```

若使用 Compose，先启动 `api` profile，再将两个值作为一次性环境变量传给迁移镜像：

```bash
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env \
  --profile api run --rm --no-deps \
  -e BOOTSTRAP_ADMIN_EMAIL=admin@example.test \
  -e BOOTSTRAP_ADMIN_PASSWORD='replace-with-a-strong-password' \
  service-api-migrate node dist/scripts/bootstrap-admin.js
```

该命令仅在数据库没有任何用户时成功，避免意外提升权限；生产环境应从 secret 注入这两个变量。

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

迁移在独立的 `service-api-migrate` 容器中执行，应用启动不会自动修改生产 schema。初始 migration 的人工回滚脚本位于 [rollback.sql](prisma/migrations/20260726000000_initial_user_auth/rollback.sql)。

生产 Compose 不在主机构建镜像，只接收 `SERVICE_API_IMAGE_REF` 和
`SERVICE_API_MIGRATION_IMAGE_REF` 两个 immutable digest；完整入口见根
[README](../README.md)。
