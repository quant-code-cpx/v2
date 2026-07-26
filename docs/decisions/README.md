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
4. 评审后改为 `Accepted`、`Rejected`、`Deprecated` 或 `Superseded`。
5. 实现与文档链接对应 ADR。

## 决策索引

- [0001：服务目录与技术方案文档布局](0001-service-repository-layout.md) — Accepted
- [0002：同步数据归属与跨服务访问](0002-data-sync-ownership-and-access.md) — Proposed
- [0003：数据同步服务运行时与存储技术栈](0003-data-sync-runtime-stack.md) — Proposed
- [0004：多数据源适配与路由](0004-market-data-provider-adapters.md) — Proposed
- [0005：API 服务最小运行时与 User/Auth 架构](0005-service-api-runtime-and-architecture.md) — Implemented
- [0006：Web 前端运行时与工程技术栈](0006-service-web-frontend-stack.md) — Accepted
- [0007：Docker Compose 开发与生产环境分层](0007-compose-environment-strategy.md) — Accepted
