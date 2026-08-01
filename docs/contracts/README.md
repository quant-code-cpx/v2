# 跨服务契约

本目录存放跨服务、可版本化的 API、事件和数据契约。

技术选型完成后，在此提交 OpenAPI、AsyncAPI 或 schema 文件；接口兼容性、版本策略和破坏性变更须记录 ADR。

所有 `service-api` 公开及运维 OpenAPI operation 必须是 `POST`，并声明
`x-http-method-policy: post-only`；禁止新增其他 method。内部 `service-data-sync` 合同不受此规则约束。
详见 [ADR-0018](../decisions/0018-service-api-post-only-http-method.md)。

## 契约索引

- [0001：service-api User/Auth OpenAPI](0001-service-api-user-auth.openapi.yaml) — Superseded（仅历史 schema/migration 参考）
- [0002：service-api 用户访问管理契约](0002-user-access-management.openapi.yaml) — Implemented
- [0003：service-data-sync 个股内部 OpenAPI](0003-data-sync-equity-internal.openapi.yaml) — Proposed
- [0004：service-api 个股公开 OpenAPI](0004-service-api-equity.openapi.yaml) — Proposed
- [0005：service-data-sync 板块内部 OpenAPI](0005-data-sync-sector-internal.openapi.yaml) — Implemented
- [0006：service-api 板块公开 OpenAPI](0006-service-api-sector.openapi.yaml) — Implemented
- [0007：service-data-sync 板块成分内部 OpenAPI](0007-data-sync-sector-membership-internal.openapi.yaml) — Proposed
- [0008：service-api 板块成分公开 OpenAPI](0008-service-api-sector-membership.openapi.yaml) — Proposed
- [0009：service-data-sync 证券主数据内部 OpenAPI](0009-data-sync-equity-instrument-internal.openapi.yaml) — Implemented
- [0010：service-api 证券主数据公开 OpenAPI](0010-service-api-equity-instrument.openapi.yaml) — Implemented
- [0011：service-data-sync 板块 EOD 内部 OpenAPI](0011-data-sync-sector-eod-internal.openapi.yaml) — Proposed
- [0012：service-api 板块 EOD 公开 OpenAPI](0012-service-api-sector-eod.openapi.yaml) — Proposed
- [0013：service-data-sync 财务与估值内部 OpenAPI](0013-data-sync-financial-valuation-internal.openapi.yaml) — Implemented
- [0014：service-api 财务与估值公开 OpenAPI](0014-service-api-financial-valuation.openapi.yaml) — Implemented
- [0015：service-data-sync 日频资金流内部 OpenAPI](0015-data-sync-daily-money-flow-internal.openapi.yaml) — Implemented
- [0016：service-api 日频资金流公开 OpenAPI](0016-service-api-daily-money-flow.openapi.yaml) — Implemented
- [0017：service-api 账户安全与运营查询 OpenAPI](0017-service-api-account-security-operations.openapi.yaml) — Implemented
- [0018：service-data-sync 个股行情与参考数据内部 OpenAPI](0018-data-sync-equity-market-data-internal.openapi.yaml) — Implemented
- [0019：service-api 个股行情与参考数据公开 OpenAPI](0019-service-api-equity-market-data.openapi.yaml) — Implemented
- [0020：service-data-sync 申万行业内部 OpenAPI](0020-data-sync-sw-sector-internal.openapi.yaml) — Implemented
- [0021：service-api 申万行业公开 OpenAPI](0021-service-api-sw-sector.openapi.yaml) — Implemented
- [0022：service-data-sync 数据运维控制面内部 OpenAPI](0022-data-sync-operations-internal.openapi.yaml) — Proposed
- [0023：service-api 数据运维公开 POST OpenAPI](0023-service-api-data-operations.openapi.yaml) — Proposed
- [0024：service-data-sync 沪深港通内部 OpenAPI](0024-data-sync-stock-connect-internal.openapi.yaml) — Accepted
- [0025：service-api 沪深港通公开 POST OpenAPI](0025-service-api-stock-connect.openapi.yaml) — Accepted
- [0026：service-data-sync 市场概览与行业板块内部 OpenAPI](0026-data-sync-market-overview-internal.openapi.yaml) — Implemented
- [0027：service-api 市场概览与行业板块公开 POST OpenAPI](0027-service-api-market-overview.openapi.yaml) — Implemented

0007/0008、0011/0012、0015/0016 是对应既有市场数据契约的增量能力。0009/0010 对
0003/0004 中证券目录、详情和上市状态相关路径具有局部权威性；0013/0014 取代其中基于
`instrumentId` 的财务报表与财务指标路径，并新增估值路径。0003/0004 的行情、复权、公司行动等
早期路径由 0018/0019 取代；新契约统一使用 `exchange + symbol`，并明确周/月线为上游接口直取。

0022/0023 共同定义数据目录、同步命令、运行、健康评估、自动计划和运维记录。0022 的
`service-data-sync` 命令/run 是执行权威；0023 的 `service-api` submission/outbox 只表示授权与交付意图。
公开写操作返回 `202 delivery=PENDING` 时，不得解释为同步服务已经受理。
对首批 A 股 `equity.*` target，来源 binding 中的 `approvalStatus`、rights/license 与归属字段仅用于审计和
lineage，不能改变 command、checkpoint、publication 或技术验收；详见
[ADR-0028](../decisions/0028-source-metadata-nonblocking-data-operations.md)。

0024/0025 共同定义沪股通、深股通、港股通（沪）、港股通（深）的共同 bundle、通道统计、
官方活跃证券榜、证券上下文和独立持久化 readiness 证据。0024 的已批准 publication
是业务事实权威，readiness 以官方日历、交付、执行和发布证据的规范快照为权威；
0025 只做认证、严格校验和版本化转发，禁止由成交额推导净买入、跨币种求和或用查询时钟猜测就绪状态。

0026/0027 共同定义市场概览、主要指数、全市场股票横截面、资金流、东财板块和申万行业能力。
0026 的完整、已批准原子 publication 是事实权威；0027 只负责公开认证、严格合同校验、版本与
缓存头透传。任一必需组件缺失或质量检查失败时不得拼接跨版本数据，也不得用板块 EOD、指数成分或
供应商资金流标签补造其他市场事实。
