# 0005：API 服务最小运行时与 User/Auth 架构

- 状态：Implemented
- 日期：2026-07-25
- 最后修改：2026-07-28
- 决策者：项目维护者
- 变更摘要：业务范围收缩为 User/Auth；保留 Redis 安全基础设施，删除 Worker、Scheduler、队列、
  实时通信及其他未使用模块和依赖。
- 关联方案：[API 服务基础架构与技术方案](../service-api/0001-service-api-foundation/index.html)

## 背景

`service-api` 仍是空骨架。参考项目 `quant-code/server-code` 已验证 NestJS、Prisma、PostgreSQL、
Redis、BullMQ、WebSocket 和多个业务模块可以共同工作，但新服务当前只需要建立可靠骨架，并承载用户管理与鉴权。
若现在复制参考项目全部模块、进程和依赖，会在没有业务需求时引入部署、测试、升级和故障成本。

本 ADR 将首期范围缩到最小可用后端：一个 HTTP 进程、`UserModule`、`AuthModule`、
PostgreSQL 与 Redis。
未来模块只有在需求出现并完成独立设计后才加入。

## 约束

- `service-api`、`service-data-sync`、`service-web` 必须独立构建、测试、运行和部署。
- 当前业务范围只有用户管理与鉴权，不接入行情、研究、策略、回测、组合、提醒、Agent 或数据同步接口。
- 当前没有异步任务、定时任务和实时推送；Redis 只服务鉴权安全控制，不承载通用业务缓存。
- 配置与秘密通过环境变量或 secret 注入；数据库变更必须可迁移、可回滚。
- 只安装当前源码会直接使用的依赖；禁止为“以后可能需要”预装包或创建空模块。
- 初始方案只确定架构；实现阶段必须保持依赖最小、迁移可回滚并补齐验证。

## 候选方案

1. 复制参考项目完整模块化单体与多进程结构。
   - 优点：未来功能入口齐全。
   - 缺点：当前绝大部分代码、基础设施和依赖无使用者，增加维护面并诱导过早设计。
2. NestJS 单一 API 进程，保留 `UserModule`、`AuthModule` 与 Redis 基础设施模块。
   - 优点：沿用参考项目清晰边界；用户与鉴权可独立测试；Redis 满足登录/刷新限流与临时锁定。
   - 缺点：需要明确单向依赖和用户安全版本，避免 User/Auth 循环引用。
3. 将用户管理与鉴权合并为一个 `IdentityModule`。
   - 优点：事务协调直接，文件更少。
   - 缺点：用户资料管理与凭证/会话职责混合，不符合当前明确保留两个模块的边界。
4. 使用轻量 Node.js HTTP 框架并自建依赖注入、校验和测试约定。
   - 优点：框架依赖更少。
   - 缺点：需要重复建设参考项目已验证的基础能力，后续扩展约定成本更高。

## 决策

采用方案 2。

### 运行时与直接依赖

- 使用 Node.js 24 LTS、TypeScript `strict`、pnpm 单一锁文件。
- 使用 NestJS 11 与 Express 5 adapter；Nest 包使用核验过的 npm `latest` 精确版本。
- 工程骨架只直接安装 `@nestjs/common`、`@nestjs/core`、`@nestjs/platform-express`、
  `@nestjs/config`、`@nestjs/cli` 和 `@nestjs/testing`。
- 实现 User/Auth 接口时再加入 `@nestjs/jwt`、`@nestjs/passport` 与 `@nestjs/swagger`；
  这些包必须有直接源码使用者。登录和刷新限流直接由 Auth 的 Redis 安全服务实现，避免额外的 Nest
  限流存储适配层。
- Redis 使用官方 `redis` client，由本地 `RedisModule` 封装连接、命名空间、健康与关闭。
- 不安装 BullMQ、Socket.IO、Schedule、Terminus、Prometheus Nest 集成、
  `@nestjs/microservices` 或对应 adapter。出现真实需求后单独评审。

### 应用与模块边界

- 只提供一个 `src/main.ts` 和一个 `AppModule`，只启动 HTTP API。
- 业务模块只保留 `UserModule` 与 `AuthModule`。
- `UserModule` 负责用户资料、角色、账户状态、密码凭证和 `securityVersion`；仅向 Auth 导出窄化的
  凭证校验与用户认证视图，不导出密码哈希或 repository。
- `AuthModule` 负责登录、JWT、refresh session、退出与认证审计；只允许
  `AuthModule → UserModule` 单向依赖。
- 用户禁用、角色变化或其他安全变更递增 `securityVersion`。Auth 校验 session/token 中版本，
  因此无需 `UserModule` 反向调用 `AuthModule`。
- 平台代码只保留配置、数据库、Redis、HTTP 生命周期、健康检查和结构化日志。
- 不创建 Worker、Scheduler、Queue、通用 Cache、Realtime、Market Data 或其他空目录、模块和配置。

### 数据与接口

- PostgreSQL 只保存 `users`、`credentials`、`sessions` 和 `audit_logs` 等 User/Auth 权威状态。
- Prisma 只在 User/Auth infrastructure/repository 内使用；Controller 不直接访问 Prisma。
- refresh token 只保存不可逆摘要，会话轮换与重用检测在数据库事务内完成。
- Redis 只保存登录/刷新限流计数、短期失败锁定和可丢失安全标记；不得成为用户、凭证或会话权威存储。
- Redis 不可用时登录与刷新 fail closed；已认证请求仍以 PostgreSQL 用户/session 校验为准。
- URI 主版本路径为 `/api/v1`；入站路由按 [ADR-0018](0018-service-api-post-only-http-method.md)
  仅使用 POST，并使用显式状态码。
- 成功响应直接返回资源或分页对象；错误使用 `application/problem+json`。
- OpenAPI 契约作为编号 YAML 文件提交；运行时同时暴露 Swagger UI 与 JSON 文档。CI 校验在引入 API 工作流时补充。

## 后果

- 首期只有一个部署进程、User/Auth 两个业务模块、一个 PostgreSQL 和一个限权 Redis，
  启动、调试、升级与故障面保持可控。
- User/Auth 保持显式职责；`securityVersion` 解决禁用/改权后的旧 token 失效，不产生循环模块依赖。
- Redis 成为本地开发和生产依赖，但用途被限制在鉴权安全控制；数据丢失不破坏权威用户与会话状态。
- `/health` 与 `/ready` 由普通 Controller、Prisma 和 Redis ping 实现，不引入 Terminus。
- 当前方案不依赖数据同步契约。任何行情或跨服务读取需求必须先新增独立方案。
- 参考项目只迁移 UserModule、AuthModule、Redis、请求上下文、异常处理、Prisma 生命周期和测试思路；
  不复制其他业务源码。
- 后续新增模块、进程或基础设施必须有当前需求、失败模式和验收标准，不以“预留”作为引入理由。

## 变更历史

- 2026-07-25：首次提出模块化单体与多角色架构。
- 2026-07-26：按当前必要性收缩为单 API 进程，只保留 User/Auth；Redis 保留为鉴权安全基础设施，
  多角色和异步基础设施移出范围。
- 2026-07-26：开始实现时确认 Redis 安全服务已覆盖登录/刷新限流，移除无直接使用者的
  `@nestjs/throttler`。
- 2026-07-26：实现 User/Auth、PostgreSQL migration、Redis 安全控制、容器编排与 OpenAPI 契约；
  通过迁移、健康检查、管理员引导、登录、刷新、登出、lint、typecheck 与单元测试验证。
- 2026-07-28：标准 HTTP method 条款由 ADR-0018 替代；全部入站业务与运维路由改为 POST-only。

## 替代关系

无。
