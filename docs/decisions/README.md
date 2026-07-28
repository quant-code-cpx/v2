# 架构决策记录

使用 ADR 保存影响架构、数据契约、部署或多个服务的重要决定。

## 命名

文件名格式：

```text
NNNN-short-title.md
```

编号递增且不复用。已废弃的 ADR 保留，并链接替代它的新 ADR。

## 流程

1. 复制 `0000-template.md`。
2. 填写背景、约束、候选方案和取舍。
3. 状态先设为 `Proposed`。
4. 评审后改为 `Accepted` 或 `Rejected`。
5. 实现并验证后改为 `Implemented`；被替代时改为 `Deprecated` 或 `Superseded`。
6. 实现与文档链接对应 ADR。

## 决策索引

- [0001：服务目录与技术方案文档布局](0001-service-repository-layout.md) — Accepted
- [0002：同步数据归属与跨服务访问](0002-data-sync-ownership-and-access.md) — Proposed
- [0003：数据同步服务运行时与存储技术栈](0003-data-sync-runtime-stack.md) — Proposed
- [0004：多数据源适配与路由](0004-market-data-provider-adapters.md) — Proposed
- [0005：API 服务最小运行时与 User/Auth 架构](0005-service-api-runtime-and-architecture.md) — Implemented
- [0006：Web 前端运行时与工程技术栈](0006-service-web-frontend-stack.md) — Accepted
- [0007：Docker Compose 开发与生产环境分层](0007-compose-environment-strategy.md) — Accepted
- [0008：默认鉴权与分层用户权限](0008-default-deny-auth-and-hierarchical-rbac.md) — Implemented
- [0009：A 股个股数据来源、复权语义与服务边界](0009-equity-data-source-and-serving-boundary.md) — Proposed
- [0010：行业、概念板块分类与派生数据边界](0010-sector-taxonomy-and-derived-data-boundary.md) — Proposed
- [0011：板块行情跨服务读取与公开 API 边界](0011-sector-market-data-api-boundary.md) — Implemented
- [0012：板块成分观测历史与跨服务读取模型](0012-sector-membership-temporal-model.md) — Proposed
- [0013：A 股证券身份、上市生命周期与跨服务读取边界](0013-equity-instrument-identity-lifecycle.md) — Proposed
- [0014：板块 EOD 横截面快照、修订与排行定义](0014-sector-eod-snapshot-definition.md) — Proposed
- [0015：财务 point-in-time 与估值边界](0015-financial-point-in-time-and-valuation.md) — Proposed
- [0016：日频资金流方法学与数据边界](0016-money-flow-methodology-boundary.md) — Proposed
- [0017：同步服务采用 SQLAlchemy Declarative ORM 作为持久化模型](0017-service-data-sync-declarative-orm.md) — Accepted（阶段 0–2 实施中）
- [0018：service-api 入站路由仅允许 POST](0018-service-api-post-only-http-method.md) — Implemented
- [0019：账户安全、审计读取与平台工作台边界](0019-account-security-audit-and-workspace-boundary.md) — Implemented
