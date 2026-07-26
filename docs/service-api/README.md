# API 服务技术方案

服务实现目录：[service-api/](../../service-api/)。

本目录记录 API 风格、认证授权、领域边界、查询策略、错误模型与版本策略。

## 方案索引

- [0001：API 服务基础架构与技术方案](0001-service-api-foundation/index.html) — Implemented
- [0002：用户访问管理与分层鉴权方案](0002-user-access-management/index.html) — Implemented
- [0003：板块行情 API 访问技术方案](0003-sector-market-data-access/index.html) — Implemented

## 跨服务关联方案

- [个股数据同步、存储与查询 API 技术方案](../service-data-sync/0011-equity-data-sync-and-api/index.html) — Proposed；
  计划新增 `EquityModule`，通过版本化内部 HTTP API 读取 `service-data-sync`。
- [板块行情 API 访问技术方案](0003-sector-market-data-access/index.html) — Implemented；
  已实现只读 `SectorMarketDataModule`，通过版本化内部 HTTP API 读取 `service-data-sync`。

当前基础实现采用 Node.js 24 LTS + NestJS 11 单一 API 进程，包含 UserModule、AuthModule，
以及为鉴权限流与短期安全状态服务的 RedisModule。
最终技术决策见 [ADR-0005](../decisions/0005-service-api-runtime-and-architecture.md)。
