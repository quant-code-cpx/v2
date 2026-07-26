# 0008：默认拒绝鉴权与分层角色授权

- 状态：Implemented
- 日期：2026-07-26
- 决策者：项目维护者
- 关联方案：[用户访问管理与登录安全方案](../service-api/0002-user-access-management/index.html)
- 关联契约：[User/Auth 已实施契约](../contracts/0002-user-access-management.openapi.yaml)
- 实施摘要：默认拒绝、常驻验证码、固定三级角色、显式 legacy 账号映射与一次性 ACTIVE ADMIN 提升均已落地；0001 已 Superseded。

## 背景

产品对外名称确定为“Apex数据智能分析平台”。`quant-v2`
只作为仓库代号，不进入产品标题、导航或用户文案。后续 AI Agent 定位为研究助手，不替代用户判断，
也不作收益承诺。

现有 `service-api` 已实现密码登录、Refresh Session、`USER`/`ADMIN`、用户列表/创建/修改和
`securityVersion`，但认证 Guard 由 Controller 局部挂载，新增路由可能因漏挂 Guard 变成匿名接口。
现有 `ADMIN` 还可以创建或修改另一个 `ADMIN`，不能表达“系统初始化超级管理员、超级管理员创建管理员、
管理员只能管理普通用户”的目标。

产品要求不开放注册，未登录用户只能进入登录所需接口，其余业务接口必须认证；部分能力只允许管理员或
超级管理员。原始表述中“未登录不允许访问登录接口和验证码接口”会使登录无法发生。本决策采用如下显式
假设：其意图是“除登录、常驻图形验证码、刷新与同源幂等退出清理外，未登录不得访问业务接口”。
`/health`、`/ready` 是编排探针，
属于受网络边界保护的运维例外，不是匿名业务能力。
该解释已作为实现边界固化：匿名 allowlist 仅服务登录与运维探针，不扩张为匿名业务能力。

约束：

- 保持 `AuthModule → UserModule` 与 `RedisModule` 单向依赖，禁止 `forwardRef()`。
- PostgreSQL 继续作为用户、凭证、会话和审计的唯一权威存储。
- Redis 只保存带 TTL 的登录安全计数、图形验证码和重放标记。
- 禁用、改密、重置密码、角色变化和软删除必须递增 `securityVersion`。
- `0002-user-access-management.openapi.yaml` 是已实施运行时合同；`0001-service-api-user-auth.openapi.yaml`
  已 Superseded，仅保留为历史 schema-and-migration 参考，不保留邮箱登录或运行时 API 兼容边界。

## 候选方案

1. 继续由每个 Controller 手工添加 JWT Guard，并保留 `USER`/`ADMIN`。
   - 优点：改动少。
   - 缺点：默认开放；无法限制管理员管理对象；任何管理员都能扩张管理员权限。
2. 全局默认认证，显式匿名 allowlist；使用固定三角色和目标级授权策略。
   - 优点：新增路由默认安全；权限矩阵小且可测试；满足当前角色边界，不引入动态 RBAC 数据维护成本。
   - 缺点：需要兼容迁移、全局 Guard、授权策略和更多越权测试。
3. 立即引入数据库驱动的角色、权限、用户角色和角色权限表。
   - 优点：可配置性强。
   - 缺点：当前没有自定义角色、租户或授权管理用例；增加缓存失效、管理 UI、迁移和误配置风险。

## 决策

采用方案 2。

### 默认认证与匿名 allowlist

- 使用全局 Authentication Guard；没有显式 `@Public()` 元数据的 HTTP 业务路由默认要求有效 Bearer
  access token，并继续校验 PostgreSQL Session、用户状态和 `securityVersion`。
- 业务匿名 allowlist 只有：
  - `POST /api/v1/auth/login`
  - `POST /api/v1/auth/captcha`，登录页打开及用户刷新图片时使用
  - `POST /api/v1/auth/refresh`，以 HttpOnly Refresh Cookie 作为凭据，不要求 Bearer token
  - `POST /api/v1/auth/logout`，只执行同源幂等清理；Cookie 有效时撤销对应 Session，任何已接受请求都清 Cookie
- `/health` 与 `/ready` 显式标注为运维公开路由，只返回最小状态；部署网络必须限制其暴露范围。
- 不注册运行时 Swagger UI 或 OpenAPI JSON，避免生成路由绕过默认拒绝 allowlist；机器可读合同只从
  `docs/contracts/0002-user-access-management.openapi.yaml` 提供。
- 浏览器调用登录、验证码、刷新和退出时必须携带精确匹配的可信 `Origin`；缺失或不匹配均拒绝，并校验
  Fetch Metadata。只有带 body 的登录要求 JSON Content-Type；空 body 的验证码、刷新和退出不作此要求。
  `SameSite=None` 只有在另有双提交或同步 CSRF token 方案时允许。
  所有设置、轮换或清除认证 Cookie 的响应使用 `Cache-Control: no-store`。
- Refresh 轮换使用单胜者 CAS。短并发宽限窗内检测到同一前序 token 时返回 `409` 与
  `Retry-After`，不撤销 session family；超出宽限窗的再次使用才按重放处理并只撤销该 family。
  Web 仍须用 single-flight 合并同一页面的刷新请求。
- Auth 注册全局认证能力；通用的 `@Public()`、权限元数据和当前请求上下文放在平台 HTTP 安全边界，
  避免 `UserModule` 反向依赖 Auth 的具体 Guard。

### 固定角色与目标级策略

- `USER`：只能读取/修改本人资料、修改本人密码、退出。
- `ADMIN`：在本人能力之外，只能列表、查看、创建、修改、软删除和重置 `USER`；不能创建
  `ADMIN`/`SUPER_ADMIN`，不能管理任何管理员。
- `SUPER_ADMIN`：在本人能力之外，可管理 `USER` 与 `ADMIN`，可创建 `ADMIN`；首期普通 API
  不创建、授予、降级、禁用或删除 `SUPER_ADMIN`。
- 管理列表除可管理目标外，可返回当前 Actor 自己且只返回自己这一条同级身份，作为只读上下文；搜索、状态
  和显式角色筛选仍适用。列表不得枚举其他 `SUPER_ADMIN`，详情、修改、重置密码和删除接口仍拒绝自身目标。
- 唯一初始 `SUPER_ADMIN` 只由一次性系统 bootstrap 创建。数据库以条件唯一索引保证最多一个
  `SUPER_ADMIN`；命令再通过 advisory lock/串行化事务原子完成“检查、创建凭证、创建用户、写审计”。
  既有环境由显式指定账号的一次性迁移命令提升一个现有 `ADMIN`，禁止按创建时间猜测。
- 自身角色、状态和删除不能通过管理员接口修改；超级管理员恢复使用单独受控运维流程。
- Controller 的权限元数据只做粗粒度能力判断。Actor 角色、目标角色、目标状态、自操作限制和并发版本
  必须在 UserModule 用例内再次校验，并与数据修改、`securityVersion` 和审计写入处于同一事务。
- 首期不建立动态权限表。角色到能力的映射集中在代码策略中，并通过 `/users/me` 返回只读
  `permissions`，供 Web 控制导航和操作显隐；后端不信任前端显隐结果。

### 生命周期、安全版本与审计

- `DELETE /api/v1/users/{id}` 执行软删除：设置 `status=DELETED` 与 `deletedAt`，保留用户、凭证关联和
  审计链；规范化账号不复用。列表默认排除已删除用户；只有 `SUPER_ADMIN` 可显式筛选 `DELETED`，
  已删除详情对 GET/PATCH/reset 返回 404。
- 管理员修改和删除使用 ETag/`If-Match`；版本不一致返回 `412 Precondition Failed`，避免覆盖并发修改。
  对已处于 `DELETED` 且仍在 Actor scope 内的目标，终态优先于旧 ETag，重复 DELETE 返回 204。
- 禁用、重新启用、角色变化、本人改密、管理员重置密码和软删除递增 `securityVersion`；资料字段修改只
  递增资源 `version`。
- 管理员在创建或重置请求中经 TLS 直接设置密码；用户收到后可立即登录，自主决定是否通过本人改密功能
  修改，系统不设置首次登录强制改密状态。API 只持久化 Argon2id hash，不生成、不记录也不回显明文。
  管理员必须通过组织批准的站外渠道交付；提交结束后 Web 无论成功或失败都清空密码输入。本人改密或
  管理员重置成功后必须递增 `securityVersion`、撤销目标既有 Session；只有本人改密会清除调用者当前浏览器
  Refresh Cookie。管理员重置不能也不会清除管理员自己的 Cookie，目标旧 Cookie 后续请求返回 401。
- 密码、验证码答案、challenge ID、access/refresh token、Cookie 和 Session 标识均列入敏感字段清单：
  网关、应用日志、审计、异常、链路追踪与 APM 统一按字段名和类型拦截/脱敏，禁止记录请求体原文。
  密码字段默认掩码、不得预填、不得进入 URL/浏览器持久化缓存/Toast；显示明文只允许用户主动触发，
  失焦、提交或关闭弹窗立即恢复掩码并清空内存状态。审计中的账号使用局部掩码，例如 `ad***01`。
- 权限、状态、密码和删除变更写追加式审计，至少记录 actor、actor role、action、target、requestId、
  脱敏的 before/after 字段与发生时间。审计写失败则业务事务失败。

### 常驻图形验证码

- 验证码是每次登录必填的反自动化控制，不是用户或会话凭据。登录页打开即调用
  `POST /api/v1/auth/captcha`，用户看不清时可刷新；接口无请求体、无前置票据、无按失败次数触发逻辑。
- 每个 challenge 使用不可猜测 ID，与 IP/可信代理上下文及 TTL 绑定，不与账号绑定；低熵答案以服务端
  密钥 HMAC 保存。登录请求必须同时提交 `captchaId` 与 `captchaAnswer`，答案验证和单次消费在 Redis
  原子完成，带短 TTL，使用后删除。错误、过期或已消费返回稳定 `422 captcha-invalid`，Web 刷新图片并
  聚焦验证码；无论登录成功或失败，下一次提交前都必须取得新 challenge。
- 后端只返回非活动 PNG，不返回可执行 SVG、音频或可供前端生成验证码的原始答案；Web 原样渲染
  `imageDataUrl`，只提供键盘可达的刷新入口。首期无语音验证码是明确的产品约束。
- 验证码接口自身按 IP 限流并返回 `Cache-Control: no-store`；Redis 不可用时验证码签发和所有登录
  fail closed。

## 兼容与迁移

- `service-web` 已通过 Contract 0002 接入真实 `/api/v1` Auth/User API；后续 Web/API/OpenAPI 变更继续协同升级，
  不以 fixture 或构建期开关替代运行时安全边界。
- `SUPER_ADMIN`、`DELETED` 是枚举扩展。旧 Prisma Client 读取新枚举值可能失败，因此先迁移 schema，
  再部署理解新枚举和角色继承的代码；确认旧实例全部退出后，才提升超级管理员或写入新状态。
- 新增 `account` 与 `normalizedAccount`：账号 trim 后转小写，必须匹配
  `^[a-z0-9][a-z0-9._-]{4,31}$`，即 5–32 个字符，并由数据库保证 `normalizedAccount` 全局唯一。
  该长度和字符集是创建用户时的校验；登录接口只要求账号非空且不超过 32 字符，格式不匹配、未知账号、
  禁用账号和错误密码统一返回凭据错误。
  既有环境先运行 `prepare-legacy-accounts`：只接受 JSON 中显式的 `userId` 与 `account`，在串行化事务和
  表锁内验证每个用户恰好映射一次、账号唯一且格式正确，再写入 nullable 两列。随后 Prisma migration
  复验完整性/格式/唯一性并收紧 NOT NULL；任何缺失、重复、email 字段或半完成状态均失败。登录切换到账号。
  原 email 可在兼容窗口内保留为非登录资料字段，目标契约不再暴露。
- 新 Session 字段先以 nullable 扩展部署兼容读：既有 Session 回填 `familyId=id`、
  `absoluteExpiresAt=expiresAt`；回填验证完成后收紧 NOT NULL/约束，再激活新写路径。无法可靠回填的
  遗留 Session 统一撤销，不猜测其 family。
- 已将 `0002` 标记 Implemented，`0001` 标记 Superseded。已实施的 `/api/v1` 不承诺旧邮箱登录或旧
  User/Auth payload 兼容；若未来发现独立消费者，必须从新版本 `/api/v2` 提供明确兼容窗口，不能回写 0001 行为。
- 空库只允许一次性 `bootstrap-admin` 创建 SUPER_ADMIN。已有库在账号迁移后只能运行
  `promote-existing-admin`，并同时提供精确 ACTIVE ADMIN 账号和 `PROMOTE_ACTIVE_ADMIN` 确认值；命令以
  advisory lock、条件唯一索引、会话撤销和审计保证单次提升，普通 API 永不提供该能力。
  Docker-only 部署通过 migration image 运行同一命令，且两个值只传给该一次性容器：

  ```bash
  docker compose -f compose.yaml -f compose.dev.yaml --env-file .env --profile api run --rm --no-deps \
    -e PROMOTE_SUPER_ADMIN_ACCOUNT=market.admin \
    -e PROMOTE_SUPER_ADMIN_CONFIRM=PROMOTE_ACTIVE_ADMIN \
    service-api-migrate node dist/scripts/promote-existing-admin.js
  ```
- PostgreSQL 枚举值不做破坏性回滚。激活前可回滚应用并保留未使用枚举值和 nullable 扩展列。
  **阶段 3 激活是旧鉴权镜像的 point of no return**：首次写入 `SUPER_ADMIN`/`DELETED`，或签发依赖
  family/absolute expiry 的新 Session 后，禁止回滚到不理解这些状态的旧版本，也禁止降级转换后宣称
  安全回滚。故障只能 roll forward，或部署仍理解新状态机的兼容修复镜像；软删除数据不因应用回滚而恢复，
  Session 绝对期限也不得被旧逻辑绕过。

## 后果

- 新业务路由漏写认证声明时仍默认受保护；匿名面可由架构测试完整枚举。
- 管理员无法自我提权，也无法管理同级或超级管理员；权限扩张路径只剩受审计的超级管理员动作。
- ADMIN 与 SUPER_ADMIN 可在管理列表辨认当前账号，但该行没有管理员编辑、重置密码或删除动作。
- Web 可基于服务端权限显示菜单，但所有安全结论仍由 API 重算。
- 增加全局 Guard、目标级策略、常驻验证码、并发控制、迁移顺序和测试成本；登录页每次打开会多一次
  CAPTCHA 签发请求。
- 固定三角色不能表达自定义权限；出现真实自定义角色或多租户需求时必须新建 ADR，不能直接扩张本策略。
- 软删除保留账号与审计链，会限制账号复用；若出现法定删除或账号复用需求，需单独设计匿名化与保留策略。

## 替代关系

本决策扩展 [ADR-0005](0005-service-api-runtime-and-architecture.md) 的 User/Auth 边界，不替代其运行时与
存储决策。新 OpenAPI 契约已替代 `0001` 的 User/Auth 接口基线；0001 保留为迁移历史，不是兼容接口。
