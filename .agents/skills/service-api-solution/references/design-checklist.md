# API 方案检查表

## 业务与所有权

- Actor、权限、用例、成功结果、失败结果和消费者明确。
- 每个读写字段有唯一权威来源和负责模块。
- 模块依赖单向，无循环依赖和 `forwardRef()`。
- 同步数据只经 data-sync 版本化接口或事件访问。

## 契约

- Method、path、version、request、response、status 和 Problem Details 完整。
- 输入校验、未知字段、枚举、时间、金额和精度规则明确。
- 分页、过滤、排序、最大 payload 和返回数量有边界。
- 幂等键、重复请求、并发修改、ETag/version 或冲突状态明确。
- 兼容、废弃、消费者迁移和 contract test 计划明确。

## 安全

- Authentication、role/resource authorization、审计和敏感字段最小化明确。
- 登录、刷新、登出、禁用、改密、角色变化对 token/session 的影响明确。
- `securityVersion` 递增条件完整。
- CORS、cookie、CSRF、proxy trust、rate limit 和 abuse cases 已分析。
- 错误不泄露内部堆栈、凭据、账号存在性或受限数据。

## 数据

- Prisma 模型、约束、关系、索引、唯一性和删除策略明确。
- transaction/isolation、并发冲突、审计写入和失败回滚明确。
- migration 使用独立 job；生产应用不自动迁移。
- expand-contract、backfill、兼容窗口、rollback 和恢复命令可执行。
- Redis 仅保存短期安全状态，不成为权威存储。

## 下游与可靠性

- 下游 timeout、retry safety、schema validation、503/fallback 和熔断行为明确。
- 重试不会重复不可逆写操作；依赖失败影响范围可隔离。
- 延迟、吞吐、错误率、连接池和 payload 预算有目标或标为待决。
- `/health` 与 `/ready` 语义、日志、指标、trace、告警和 runbook 入口明确。

## 验收

- OpenAPI 或共享 schema 可机器解析并与方案链接。
- 正常、无权限、校验失败、重复请求、并发冲突和依赖失败可验证。
- migration deploy/rollback 与旧版本兼容可演练。
- `format:check`、`lint`、`typecheck`、现有测试、build、Docker build、health/ready 命令明确。
