# 0011：板块行情跨服务读取与公开 API 边界

- 状态：Implemented
- 日期：2026-07-26
- 决策者：项目维护者（待评审）
- 关联方案：[板块行情 API 访问技术方案](../service-api/0003-sector-market-data-access/index.html)
- 关联契约：[同步服务内部接口](../contracts/0005-data-sync-sector-internal.openapi.yaml)、
  [API 对外接口](../contracts/0006-service-api-sector.openapi.yaml)
- 继承决策：[ADR-0002](0002-data-sync-ownership-and-access.md)、
  [ADR-0010](0010-sector-taxonomy-and-derived-data-boundary.md)

## 背景

当前 `service-data-sync` 已保存东方财富行业/概念板块日、周、月三个独立周期的 canonical revision 与
publication；`service-api` 当前没有对应业务模块或下游 HTTP client。若 API 直接查询同步库，会绕过
`service-data-sync` 的数据版本、revision、质量与迁移边界；若把同步库复制进 API，又会引入尚未需要的
第二权威存储。

板块 `scheme + code` 是稳定身份，中文名称只能是可修订展示属性。周/月数据是上游直接周期事实，不允许
被 API 由日线计算；`volume_unit=provider_native` 未确认跨 scheme 可比性，不能被 API 聚合、排名或换算。

## 候选方案

1. `service-api` 使用只读账号直接查询 `service-data-sync` PostgreSQL。
   - 优点：低延迟、实现快。
   - 缺点：共享表结构成为隐式契约，绕过 publication/revision，违反 ADR-0002。
2. `service-data-sync` 提供版本化内部 HTTP 读接口，`service-api` 通过受控 client 转发为公开 API。
   - 优点：数据所有权、版本、质量和限额可由同步服务统一执行；API 不依赖同步表结构。
   - 缺点：新增下游超时、鉴权、协议兼容和 503 失败面。
3. 同步服务发布事件，`service-api` 建立完整板块读模型。
   - 优点：运行时读隔离、低延迟。
   - 缺点：需要事件平台、回放、顺序和一致性治理；当前消费者和规模不足以证明成本合理。

## 决策

采用方案 2；方案 1 禁止；方案 3 保留为未来扩展路径。

- `service-data-sync` 独占板块 canonical 表、raw evidence、数据版本和内部读接口；内部契约为
  `docs/contracts/0005-data-sync-sector-internal.openapi.yaml`。
- `service-api` 新增独立的只读板块行情能力，公开路径位于 `/api/v1/market/sectors`；对外契约为
  `docs/contracts/0006-service-api-sector.openapi.yaml`。
- API 只公开 `eastmoney.industry` 与 `eastmoney.concept` 的 ACTIVE 发布板块。`PENDING` 身份、raw URI、
  provider 行结构、内部 `sectorId`、质量明细和历史 revision 不对外暴露。
- 日、周、月由 `period=1d|1w|1mo` 显式选择；每个响应只能来自一个对应的 `dataVersion`。周/月不读日线，
  不聚合，不补洞。
- `volumeValue` 只能与 `volumeUnit=provider_native` 成对返回。首期不提供跨板块成交量排名、加总或归一化接口。
- 公开读取默认要求现有有效登录态；是否给所有 ACTIVE 角色统一授予市场读取权限、是否新增细粒度权限，
  是本 ADR 的评审门槛。未决前不得把 controller 标记为 Public。
- 内部接口仅接受专用服务凭据与服务网络访问。凭据形态、轮换、网络策略和生产 secret 注入在实施前冻结；
  禁止复用用户 JWT、同步库账号或对象存储凭据。
- `service-api` 对安全 GET 最多重试一次，且仅限尚未开始返回响应时的可重试连接错误；下游超时、契约校验
  失败、断路器打开或 5xx 统一映射受控 503，不返回未标记的陈旧业务数据。
- Redis 不保存板块行情、publication 或缓存权威副本；首期只允许短期防滥用状态。API `/ready` 继续只检查
  自己的 PostgreSQL/Redis，数据同步暂时不可用以业务 503 与指标暴露，不造成 API 进程重启循环。

## 后果

- `service-data-sync` 需新增内部 HTTP 层、`get/list` 读端口、cursor/dataVersion 绑定、服务鉴权和契约测试；
  现有 CLI 与数据库表不变。
- `service-api` 需新增 SectorMarketDataModule、DTO、下游 client、Problem Details 映射、限流、OpenAPI
  contract test 与可观测性；不新增 Prisma 业务表或 Redis 数据缓存。
- 内部与公开接口均采用 `/v1`。仅新增 optional 字段可在 v1 兼容；修改 scheme、周期语义、精度、身份或
  单位需新增 v2 与消费者迁移窗口。
- 当前对外 API 和内部接口均已实现：内部静态 Bearer、仅已认证用户读取、目录/三周期 HTTP 路由和关键合同回归测试均已落地。
  服务凭据的生产签发、轮换和来源生产准入仍由部署与运行治理承担。

## 替代关系

本 ADR 落实 ADR-0002 对同步数据“版本化 HTTP 访问”的方向，并将 ADR-0010 的板块 scheme、三周期和
单位边界带入服务契约；不改变个股数据 ADR-0009 的复权或 API 语义。
