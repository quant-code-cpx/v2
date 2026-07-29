# ADR-0023：个人市场数据通用查询网关

- 状态：Accepted
- 日期：2026-07-29
- 决策者：quant-v2 维护者

## 背景

方案 0019–0029 已提供 data-sync 的 typed catalog/query 内部边界，但 service-api 原先只规划按领域拆分的公开路由。
个人使用时，这会使已入库但尚未逐域注册 DTO 的数据无法被 API 或前端读取，并把 AKShare 暂不可用误放大为链路阻断。

## 决策

新增认证后的 `POST /api/v1/market-data/query`：

- `service-api` 仅通过 `MarketDataAccessClient` 调用 data-sync 的
  `POST /internal/v1/market-data/query`；不读取 data-sync PostgreSQL、对象存储或 Provider。
- 请求仍由 data-sync 的 dataset/schema、字段、筛选、排序、窗口、PIT 与页大小 allowlist 严格校验；网关不拼接 SQL。
- 对已注册但无 canonical publication 的 dataset，data-sync 返回 `200`、`records: []` 与
  `meta.availability=SOURCE_UNAVAILABLE`，不伪造 `dataVersion`。有合法空发布时同样返回成功空集合。
- 公开网关接受现有认证用户，适用于个人部署；它不承诺多租户 entitlement、公开再分发、缓存、批量导出或替代后续领域 DTO。
- 响应仅透传 typed canonical 投影；raw URI、Provider 凭据、数据库键、候选、隔离与失败证据不得出现。

## 后果

同步→入库→API 的首条访问链路不再依赖每个专题单独完成公开 DTO。后续可按领域增加更窄的路由、权限、缓存和展示模型；它们不得改变本路由的空结果语义。

本 ADR 覆盖 ADR-0020 与 service-api 方案 0005 中“不得提供通用公开 query”的默认限制，仅限本仓库的认证个人部署。
