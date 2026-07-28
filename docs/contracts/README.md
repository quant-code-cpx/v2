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
- [0009：service-data-sync 证券主数据内部 OpenAPI](0009-data-sync-equity-instrument-internal.openapi.yaml) — Proposed
- [0010：service-api 证券主数据公开 OpenAPI](0010-service-api-equity-instrument.openapi.yaml) — Proposed
- [0011：service-data-sync 板块 EOD 内部 OpenAPI](0011-data-sync-sector-eod-internal.openapi.yaml) — Proposed
- [0012：service-api 板块 EOD 公开 OpenAPI](0012-service-api-sector-eod.openapi.yaml) — Proposed
- [0013：service-data-sync 财务与估值内部 OpenAPI](0013-data-sync-financial-valuation-internal.openapi.yaml) — Proposed
- [0014：service-api 财务与估值公开 OpenAPI](0014-service-api-financial-valuation.openapi.yaml) — Proposed
- [0015：service-data-sync 日频资金流内部 OpenAPI](0015-data-sync-daily-money-flow-internal.openapi.yaml) — Proposed
- [0016：service-api 日频资金流公开 OpenAPI](0016-service-api-daily-money-flow.openapi.yaml) — Proposed
- [0017：service-api 账户安全与运营查询 OpenAPI](0017-service-api-account-security-operations.openapi.yaml) — Implemented

0007/0008、0011/0012、0015/0016 是对应既有市场数据契约的增量能力。0009/0010 对
0003/0004 中证券目录、详情和上市状态相关路径具有局部权威性；0013/0014 取代其中基于
`instrumentId` 的财务报表与财务指标路径，并新增估值路径。0003/0004 的行情、复权、公司行动等
未重叠能力继续有效。
