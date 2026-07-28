# 0018：service-api 入站路由仅允许 POST

- 状态：Implemented
- 日期：2026-07-28
- 决策者：项目维护者
- 关联合同：[User/Auth 2.0](../contracts/0002-user-access-management.openapi.yaml)
- 关联实现：[service-api](../../service-api/README.md)、[service-web](../../service-web/README.md)

## 背景

项目明确要求 `service-api` 只使用 `POST`。现有实现同时使用 GET、PATCH、DELETE 与 POST，
健康探针也使用 GET；User 资源还依赖同一路径上的不同 method 区分读取、更新和删除。
若只修改部分 Controller，Web、Compose 健康检查、OpenAPI 或后续代码仍可能重新引入非 POST。

该约束改变公开兼容边界，也影响 `service-web` 和部署探针，因此必须由跨服务 ADR、机器合同和自动化
测试共同固化。

## 候选方案

1. 只把当前非 POST Controller 改为 POST。
   - 优点：改动最小。
   - 缺点：前端、合同和未来代码仍可重新引入其他 method，不能形成强制规范。
2. 所有应用路由 POST-only，并在代码、合同、Web 传输层和测试中同时强制。
   - 优点：规则唯一且可自动验证；调用方没有 method 漂移空间。
   - 缺点：失去标准 HTTP method 的缓存与语义优势，资源操作需要动作路径。
3. 业务路由只用 POST，但保留 GET 健康探针。
   - 优点：兼容常见编排默认值。
   - 缺点：出现“哪些路由例外”的长期歧义，不符合全部入站路由统一的目标。

## 决策

采用方案 2。

- `service-api` 声明的业务与运维 Controller 路由全部使用 `@Post()`，包括 `/health` 与 `/ready`。
- 禁止声明或引入 `@Get()`、`@Put()`、`@Patch()`、`@Delete()`、`@Head()`、`@Options()`、
  `@All()`；公开 OpenAPI 也只能声明 `post` operation。
- 浏览器 CORS preflight 是框架级传输行为，不注册为应用 Controller 路由；`service-api` 调用
  `service-data-sync` 等下游的出站 method 不受本决策约束。
- 不发生路径冲突的读取路由只替换 method。User 多操作资源采用明确动作路径：
  - `POST /api/v1/users/me`：读取本人；
  - `POST /api/v1/users/me/update`：更新本人；
  - `POST /api/v1/users/list`：查询用户列表；
  - `POST /api/v1/users`：创建用户；
  - `POST /api/v1/users/{id}`：读取用户；
  - `POST /api/v1/users/{id}/update`：更新用户；
  - `POST /api/v1/users/{id}/delete`：软删除用户。
- 查询型 POST 可继续使用 query 参数，减少 DTO 与缓存键迁移范围；变更操作继续使用
  `If-Match`、事务和审计控制并发与重试。
- HTTP `304` 只适用于条件 GET/HEAD。公开读取型 POST 命中 `If-None-Match` 时返回无响应体的
  `204`；内部 data-sync GET 返回的 `304` 由 API Controller 映射为 `204`。
- `service-web` 共享 HTTP 传输层硬编码 `POST`，业务调用选项不再暴露 method。
- 架构测试递归扫描所有 Nest Controller 和公开 service-api OpenAPI，发现非 POST 即失败。

## 后果

- 规则可在 Controller、合同、Web 传输层和 CI 测试四处交叉验证。
- 现有非 POST 消费者必须与 2.0 合同原子迁移；不提供双 method 兼容窗口。
- 普通共享 HTTP 缓存不能再依赖 GET 语义；前端远程状态缓存继续由 TanStack Query 管理。
- User 更新和删除路径发生破坏性变化，监控、E2E mock、Compose 探针与运维脚本必须同步更新。
- 原始非 POST 请求不再命中应用路由。若未来需要恢复标准 HTTP method，必须新建 ADR 并同时修改
  Controller、合同、客户端和强制测试，不能局部放宽。

## 替代关系

本决策仅替代 [ADR-0005](0005-service-api-runtime-and-architecture.md) 中“接口使用标准 HTTP 方法”的
条款；其余运行时、模块和存储决策继续有效。
