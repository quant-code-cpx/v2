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
- [板块成分股与观测历史技术方案](../service-data-sync/0013-sector-membership-history/index.html) — Proposed；
  扩展现有 `SectorMarketDataModule`，提供板块到成分及证券到板块的观测历史。
- [A 股证券主数据与上市生命周期技术方案](../service-data-sync/0014-equity-instrument-master/index.html) — Proposed；
  新增只读证券目录、详情及上市状态历史，局部取代 0011 中尚未实施的主数据接口设计。
- [板块 EOD 横截面快照与排行技术方案](../service-data-sync/0015-sector-eod-snapshot-ranking/index.html) — Proposed；
  扩展现有 `SectorMarketDataModule`，提供收盘后横截面及确定性排行。
- [财务报表与估值技术方案](../service-data-sync/0016-financial-statements-valuation/index.html) — Proposed；
  提供具备 point-in-time 语义的报表、指标与估值只读查询。
- [日频资金流向技术方案](../service-data-sync/0017-daily-money-flow/index.html) — Proposed；
  提供显式方法学约束的日频序列与供应商滚动排行，不包含分钟或分时。

以上能力均由 `service-api` 经版本化 internal HTTP 读取；不得为读取方便复制 canonical Prisma 表、
直连同步数据库或把 Redis 作为权威数据源。

当前基础实现采用 Node.js 24 LTS + NestJS 11 单一 API 进程，包含 UserModule、AuthModule，
以及为鉴权限流与短期安全状态服务的 RedisModule。
最终技术决策见 [ADR-0005](../decisions/0005-service-api-runtime-and-architecture.md)。
