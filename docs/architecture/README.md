# 架构边界

## 系统上下文

```text
外部财经数据源
       |
       v
service-data-sync  --->  同步服务专属存储
       |
       | 版本化内部 API
       v
service-api  <---  service-web
```

同步服务独占其数据库和原始数据对象存储，API 服务不得直连。跨服务访问决策见
[ADR-0002](../decisions/0002-data-sync-ownership-and-access.md)。

该图表示目标边界，不代表当前已实现接口或数据模型。`0001-data-sync-foundation`
仅搭工程骨架与基础设施接线；接口、表、迁移、调度和真实同步均由后续编号方案实现。

## 服务职责

### service-data-sync

- 获取、校验、标准化和持久化财经与股票基础数据。
- 所有外部数据只能通过 provider-neutral port 和独立 adapter 获取；同步核心不认识具体供应商。
- 数据源优先级由能力、成本、权益和健康策略配置，不硬编码在任务或领域模型中。
- 保存数据来源、同步时间、版本和质量状态。
- 支持幂等重试、增量同步、断点恢复与失败审计。

### service-api

- 提供稳定、可版本化的业务 API。
- 承担授权、查询编排、领域逻辑和错误转换。
- 隔离 Web 服务与数据库、缓存及第三方数据源。

### service-web

- 提供数据浏览、分析和管理界面。
- 只通过 API 服务的公开契约获取业务数据。
- 不持有服务端密钥，不直接访问数据库。

## 跨服务约束

- 契约必须显式、可版本化并有兼容策略。
- 时间统一存储为明确时区的值；展示层再做本地化。
- 金额、价格和比率避免使用二进制浮点表达关键计算。
- 数据修订、复权、停牌、退市等市场语义必须可追溯。
- 可观测性应统一关联请求、任务、数据批次和错误。

## 尚未决定

生产部署平台、统一入口、TLS、secret manager 和监控平台仍待 ADR 确认。同步服务基础选型见
[技术方案](../service-data-sync/0001-data-sync-foundation/index.html)。

## 部署与容器环境

- [0001：Docker 开发与生产环境方案](0001-docker-environments/index.html)
- [ADR-0007：Docker Compose 开发与生产环境分层](../decisions/0007-compose-environment-strategy.md)

当前生产 Compose 是单节点部署基线，不代表高可用或最终云平台拓扑。

## 跨服务控制面

- [0002：数据运维控制面](0002-data-operations-control-plane/index.html) — Proposed
- [ADR-0024：数据运维控制面与全局串行同步](../decisions/0024-data-operations-control-plane.md) — Proposed

数据运维控制面统一来源目录、同步命令、全局执行槽、健康评估、动态计划和双层审计；
`service-data-sync` 仍是同步与数据事实权威，`service-api` 只负责身份、授权和可靠 HTTP 投递。
