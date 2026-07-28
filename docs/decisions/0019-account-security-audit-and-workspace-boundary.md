# 0019：账户安全、审计读取与平台工作台边界

- 状态：Implemented
- 日期：2026-07-28
- 决策者：项目维护者
- 关联 API 方案：[账户安全与运营查询技术方案](../service-api/0004-account-security-and-operations/index.html)
- 关联 Web 方案：[个人中心、安全审计与平台工作台技术方案](../service-web/0005-account-security-and-workspace/index.html)
- 关联契约：[账户安全与运营查询 OpenAPI](../contracts/0017-service-api-account-security-operations.openapi.yaml)

## 背景

当前 User/Auth 已支持本人资料修改、本人改密、Refresh Session 轮换、分层用户管理和审计追加写，
但 Web 尚未暴露个人资料与改密入口，用户不能查看或撤销其他登录会话，超级管理员也不能查询已经写入
PostgreSQL 的审计记录。受保护首页仍是占位页。

本阶段暂不建设 `service-data-sync`、行情、全局搜索、通知、批量用户操作、导入导出或帮助中心。
所有 `service-api` 入站路由继续遵守 ADR-0018，只允许 `POST`。

需要冻结以下跨服务边界：

- 会话管理是否允许管理员查看或撤销他人会话；
- 审计读取权限、返回字段和原始 `metadata` 暴露边界；
- 首页是否增加一个后端聚合接口；
- 是否为设备识别保存原始 IP、User-Agent 或设备指纹；
- 新页面如何在未来“今日市场”首页上线后继续复用。

## 候选方案

### 方案 A：管理员可查看全部会话，保存原始 IP 与 User-Agent

优势：

- 管理员能集中处置账号风险；
- 用户能看到浏览器、系统和登录位置等熟悉信息。

成本与风险：

- 原始 IP、User-Agent 和设备指纹扩大个人信息与安全数据面；
- 管理员查看同级或更高角色会话容易形成横向信息泄露；
- 浏览器标识不可靠，可能造成虚假安全感；
- 需要新增解析、保留、脱敏和数据主体治理规则。

### 方案 B：只做本人会话管理，使用现有 Session family

优势：

- 复用 `Session.familyId`、`createdAt`、`absoluteExpiresAt` 和当前 `sessionId`；
- 不新增原始 IP、User-Agent、设备指纹或新的权威状态；
- 用户可撤销单个其他会话或全部其他会话；
- 目标授权简单，风险面最小。

成本与风险：

- 首期只能以最近活动时间和会话短标识区分不同登录；
- 管理员无法代替用户撤销某一个会话，仍需通过禁用、改密或重置密码撤销该用户全部会话。

### 方案 C：工作台使用单一 `/workspace/summary` 聚合接口

优势：

- 首屏只发一个请求；
- 服务端可以按角色返回不同聚合结果。

成本与风险：

- 新模块会重复 User、Auth、Audit 的读取逻辑；
- 角色分支响应容易泄露不该出现的字段；
- 前端页面与一个大而不稳定的聚合契约强耦合。

### 方案 D：工作台组合稳定的小型领域查询

优势：

- 当前身份、本人会话、可管理用户统计和审计事件仍由各自模块拥有；
- 每个查询可独立授权、缓存、降级和演进；
- 普通用户不会请求管理员数据。

成本与风险：

- 管理员首屏有 2–4 个并行请求；
- Web 必须明确 partial 状态，不能把一个查询失败扩大为整页失败。

## 决策

采用方案 B 与方案 D，并作以下约束：

1. **本人会话管理**
   - `AuthModule` 只允许用户列出和撤销自己的 Session family。
   - 首期不提供管理员查看或撤销他人单个会话的 API。
   - 管理员仍可通过禁用、角色变更或密码重置使目标用户全部会话失效。
   - 撤销单个 family 不递增 `securityVersion`；Session 已绑定 access token，每次鉴权读取
     `revokedAt` 即可立即拒绝。

2. **会话数据最小化**
   - 不保存或返回原始 IP、User-Agent、设备指纹和地理位置。
   - 列表只返回随机 `familyId`、是否当前会话、最近活动时间和绝对过期时间。
   - “最近活动时间”取该 family 当前未撤销 Session 行的 `createdAt`；Refresh 轮换会创建新行，
     因而它是可验证的近似活动时间，不宣称为设备最后操作时间。

3. **审计读取**
   - 新建 `AuditModule`，PostgreSQL `AuditLog` 继续是唯一权威来源。
   - 首期只有 `SUPER_ADMIN` 获得 `audit:read`；`ADMIN` 不读取审计集合。
   - 列表返回服务端映射的 category、severity、summary、actor、target 和 requestId。
   - 禁止直接返回原始 `metadata`；详情只输出按 action 明确允许的脱敏字段。
   - `auth.refresh.rotated` 保留为既有例行记录，但默认列表不展示，除非显式启用例行事件。

4. **工作台组合**
   - 不新增 `WorkspaceModule` 或 `/workspace/summary`。
   - `/` 页面并行组合 `/users/me`、本人会话列表、可管理用户统计和超级管理员审计列表。
   - 普通用户只请求本人能力；`ADMIN` 增加其可管理 USER 统计；`SUPER_ADMIN` 再增加
     USER/ADMIN 统计和近期重要审计。
   - 每个区块独立 loading/error/retry；身份请求失败仍走现有全局会话边界。

5. **页面与未来首页**
   - 新建 `/account` 个人中心和 `/security/audit` 安全审计中心。
   - `/` 的阶段性“平台工作台”只承载账户安全、用户运营和快捷任务，不展示个人资产或虚构行情。
   - 未来真实“今日市场”契约冻结后，其内容进入 `/` 首屏；本阶段工作台区块下移复用，不阻塞或替代市场总览。

6. **方法与状态所有权**
   - 所有新增 API operation 使用 `POST`。
   - 远程状态进入 TanStack Query；审计筛选和分页、个人中心 section 进入 URL；
     Dialog 开关等短期交互留在组件。

7. **审计保留与索引发布**
   - `AuditLog` 在线保留 90 天，首期不归档；超过保留期的数据不可恢复。
   - 清理由独立维护任务每日执行，不绑定 API 启动；单批最多删除 5,000 行，并用 PostgreSQL
     advisory lock 保证同一时刻只有一个实例运行。
   - 清理任务可重试、可重复执行，只记录删除数量、耗时和失败原因；不为清理动作再写审计事件，
     避免形成自增循环。
   - 三个查询索引默认由独立 Prisma migration job 在维护窗口创建。发布前检查
     `audit_logs` 行数；达到 100 万行时阻断普通 migration，改走单独评审的
     `CREATE INDEX CONCURRENTLY` 发布任务。
   - 生产应用启动不得执行 migration 或保留期清理。

## 后果

### 正面影响

- 资料、改密、会话自助处置形成完整闭环。
- 审计从“只写不可见”变成最小权限可查询。
- 不增加敏感设备数据或 Redis 权威状态。
- 首页模块可独立失败，未来市场首页上线时仍可复用。
- Auth → User 单向依赖不变，不需要 `forwardRef()`。

### 成本

- `AuditLog`、`Session` 和 `User` 需要补充查询索引。
- Web 需要三个新路由页、权限导航、Query 缓存和多区块 partial 状态。
- 无设备名称时，会话辨识能力有限；页面必须明确展示“会话标识”而非伪造设备名称。

### 风险与缓解

- 审计表增长导致慢查询：采用 90 天在线保留、每日分批清理、时间范围、最大页长、opaque cursor
  和新索引；百万行门禁避免普通 migration 长时间锁表。
- 撤销当前会话后 UI 仍显示已登录：当前 family 撤销成功后立即清理 Web 内存 token 和查询缓存。
- 工作台并行请求产生局部错误：每个 Card 独立恢复，禁止整页 fail closed；只有身份失败进入登录恢复。
- action 字符串持续扩散：实现时建立服务端 action registry 和响应映射，未知 action 返回通用摘要，
  不透传 metadata。

## 已冻结运维口径

- 在线保留 90 天，不归档；API/运维共同负责每日独立清理任务和失败告警。
- `audit_logs` 少于 100 万行时使用普通 Prisma migration；达到 100 万行时发布门禁必须阻断，
  并改用受控并发建索引流程。
- 保留期清理和索引任务均由部署编排触发，不进入 API 应用进程。

## 后续触发项

- 如后续需要可信设备名称，必须另行评审个人信息、数据保留和欺骗风险；首期不做。

## 替代关系

本决策扩展 [ADR-0008](0008-default-deny-auth-and-hierarchical-rbac.md) 的鉴权与分层角色模型，
并继续受 [ADR-0018](0018-service-api-post-only-http-method.md) 约束；不替代既有 ADR。
