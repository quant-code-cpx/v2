# 跨服务契约

本目录存放跨服务、可版本化的 API、事件和数据契约。

技术选型完成后，在此提交 OpenAPI、AsyncAPI 或 schema 文件；接口兼容性、版本策略和破坏性变更须记录 ADR。

## 契约索引

- [0001：service-api User/Auth OpenAPI](0001-service-api-user-auth.openapi.yaml) — Superseded（仅历史 schema/migration 参考）
- [0002：service-api 用户访问管理契约](0002-user-access-management.openapi.yaml) — Implemented
- [0003：service-data-sync 个股内部 OpenAPI](0003-data-sync-equity-internal.openapi.yaml) — Proposed
- [0004：service-api 个股公开 OpenAPI](0004-service-api-equity.openapi.yaml) — Proposed
- [0005：service-data-sync 板块内部 OpenAPI](0005-data-sync-sector-internal.openapi.yaml) — Implemented
- [0006：service-api 板块公开 OpenAPI](0006-service-api-sector.openapi.yaml) — Implemented
