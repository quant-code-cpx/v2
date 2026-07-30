# API 服务技术方案

服务实现目录：[service-api/](../../service-api/)。

本目录记录 API 风格、认证授权、领域边界、查询策略、错误模型与版本策略。

- 方案 0011 个股市场数据已实现：`POST /api/v1/equities/{exchange}/{symbol}/bars`、
  `adjustment-factors`、`corporate-actions` 和 `company-profile`。service-api 仅通过
  service-data-sync 内部 HTTP 契约读取，周/月线不由日线派生。
- 方案 0012、0016、0017 已分别实现申万三级行业、财务与估值、日频资金流公开 POST API；
  均只读取已发布的内部 HTTP 视图。
- 个人部署已实现 `POST /api/v1/market-data/query`：它经 `MarketDataAccessClient` 调用 data-sync
  typed query；已注册但无 publication 时返回带可用性状态的成功空 records。
- 公开契约：[0019-service-api-equity-market-data.openapi.yaml](../contracts/0019-service-api-equity-market-data.openapi.yaml)
  所有 `service-api` 路由仅允许 `POST`；强制规则与迁移边界见
  [ADR-0018](../decisions/0018-service-api-post-only-http-method.md)。

## 方案索引

- [0001：API 服务基础架构与技术方案](0001-service-api-foundation/index.html) — Implemented
- [0002：用户访问管理与分层鉴权方案](0002-user-access-management/index.html) — Implemented
- [0003：板块行情 API 访问技术方案](0003-sector-market-data-access/index.html) — Implemented
- [0004：账户安全与运营查询技术方案](0004-account-security-and-operations/index.html) — Implemented
- [0005：高价值市场数据访问的 service-api 影响方案](0005-market-data-access-impact/index.html) — Proposed；
  个人部署的最小通用 query 网关已按 ADR-0023 实现，领域化 DTO、细粒度权限、缓存与导出仍为后续工作。
- [0006：数据运维控制面 API 方案](0006-data-operations-control-plane/index.html) — Proposed；
  定义 POST-only 管理契约、RBAC、submission/outbox 可靠投递、幂等与双层审计投影。
- [沪深港通中心 service-api 方案](../service-web/0010-stock-connect-center/service-api.html) — Proposed；
  方案目录按跨服务模块统一归档，定义四条认证、限流、条件读取的公开 POST API。

## 跨服务关联方案

- [个股数据同步、存储与查询 API 技术方案](../service-data-sync/0011-equity-data-sync-and-api/index.html) — Implemented；
  已扩展 `StockModule`，通过版本化内部 HTTP API 读取 `service-data-sync`。
- [板块行情 API 访问技术方案](0003-sector-market-data-access/index.html) — Implemented；
  已实现只读 `IndustryModule`，通过 `DataSyncModule` 的版本化内部 HTTP API Client 读取
  `service-data-sync`。
- [行业与板块数据同步技术方案](../service-data-sync/0012-industry-and-sector-data-sync/index.html) — Implemented；
  `IndustryModule` 已提供申万三级 taxonomy、父级闭包与供应商估值观察。
- [板块成分股与观测历史技术方案](../service-data-sync/0013-sector-membership-history/index.html) — Implemented；
  `IndustryModule` 已提供板块到成分及证券到板块的观测历史。
- [A 股证券主数据与上市生命周期技术方案](../service-data-sync/0014-equity-instrument-master/index.html) — Implemented；
  已提供只读证券目录、详情、上市状态历史和日期感知身份语义。
- [板块 EOD 横截面快照与排行技术方案](../service-data-sync/0015-sector-eod-snapshot-ranking/index.html) — Implemented；
  `IndustryModule` 已提供收盘后横截面及确定性排行。
- [财务报表与估值技术方案](../service-data-sync/0016-financial-statements-valuation/index.html) — Implemented；
  `StockModule` 已提供具备 point-in-time 语义的报表、来源指标、平台派生指标与估值查询。
- [日频资金流向技术方案](../service-data-sync/0017-daily-money-flow/index.html) — Implemented；
  `MoneyFlowModule` 已提供显式方法学约束的日频序列与供应商滚动排行，不包含分钟或分时。
- [高价值市场数据扩展路线图](../service-data-sync/0029-market-data-expansion-roadmap/index.html) — Proposed；
  汇总八类数据、统一 canonical model、分阶段门禁与回滚边界。
- [data-sync 市场数据访问契约方案](../service-data-sync/0028-data-access-contract/index.html) — Proposed；
  拟议通过 POST-only 内部 HTTP catalog/query 提供不可变版本查询，事件平台延后另行决策。
- [高价值市场数据访问的 service-api 影响方案](0005-market-data-access-impact/index.html) — Proposed；
  个人部署已提供认证后的 `POST /api/v1/market-data/query` 最小网关，领域化 DTO、权限、限额、缓存和
  批量导出仍按方案逐步收敛。
- [拟议机器合同](../contracts/data-sync-market-data-v1.yaml) 与
  [ADR-0020](../decisions/0020-data-sync-market-data-access.md) 均为 Proposed。
- [沪深港通与跨境互联互通中心](../service-web/0010-stock-connect-center/index.html) — Proposed；
  service-api 只消费
  [0024 内部契约](../contracts/0024-data-sync-stock-connect-internal.openapi.yaml)，并按
  [0025 公开契约](../contracts/0025-service-api-stock-connect.openapi.yaml) 对 Web 暴露领域化 POST 查询。

以上能力均由 `service-api` 经版本化 internal HTTP 读取；不得为读取方便复制 canonical Prisma 表、
直连同步数据库或把 Redis 作为权威数据源。

当前基础实现采用 Node.js 24 LTS + NestJS 11 单一 API 进程，包含 `UserModule`、`AuthModule`、
`StockModule`、`IndustryModule`、`DataSyncModule`，以及为鉴权限流与短期安全状态服务的
`RedisModule` 与 `MoneyFlowModule`。
最终技术决策见 [ADR-0005](../decisions/0005-service-api-runtime-and-architecture.md)。
