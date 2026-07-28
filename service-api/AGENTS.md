# AGENTS.md

## 适用范围

本文件适用于 `service-api/`。同时继承仓库根目录 `AGENTS.md`；冲突时以本文件为准。

## 架构边界

- `AuthModule` 只能单向调用 `UserModule` 与 `RedisModule`；禁止 `forwardRef()`。
- Redis 只保存短期鉴权安全状态，不得保存用户、凭证或会话权威数据。
- 任何用户禁用、改密、角色变化必须递增 `securityVersion`。
- 所有 Controller 路由，包括 `/health` 与 `/ready`，只能使用 `@Post()`；禁止使用或引入
  `@Get()`、`@Put()`、`@Patch()`、`@Delete()`、`@Head()`、`@Options()`、`@All()`。
- 同一路径存在多个操作时，保留一个资源读取路径，其他操作使用明确动作后缀，例如 `/list`、
  `/update`、`/delete`。CORS preflight 由框架处理，不得为它声明 Controller 路由。

## 测试目录与归属

- 每个 Nest module 使用 `src/apps/<ModuleName>/test/`，该 module 的 Controller、Service、DTO、
  Guard 等测试全部集中于此。
- `config/`、`data-sync/`、`lifecycle/`、`shared/` 与 `scripts/` 下的独立功能也在自己目录内建立
  `test/`。

## 验证要求

使用 Node.js 24 与 pnpm 11；宿主机 Node.js 版本不满足时，全部命令通过 Docker 运行。
以下 pnpm 命令在 `service-api/` 目录执行：

- 安装：`corepack pnpm@11.17.0 install`
- 开发：`pnpm start:dev`
- 格式化与静态检查：`pnpm format:check`、`pnpm lint`、`pnpm typecheck`
- 单元测试：`pnpm test`
- 构建：`pnpm build`
- 迁移：`pnpm prisma:deploy`；生产应用启动不得自动迁移

以下 Docker 与 Compose 命令在仓库根目录执行：

- 容器测试：`docker build --target test --tag quant-v2/service-api:test service-api`
- 容器构建：`docker build --tag quant-v2/service-api:local service-api`
- 本地完整启动：`docker compose -f compose.yaml -f compose.dev.yaml --env-file .env.example
--profile api up --build`
- 容器健康检查：请求 `POST /health` 和 `POST /ready`

完成任务前，至少运行受影响范围内可执行的格式化、静态检查、单元测试与容器构建，并报告未运行项目及原因。
