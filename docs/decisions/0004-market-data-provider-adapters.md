# 0004：多数据源适配与路由

- 状态：Proposed
- 日期：2026-07-25
- 决策者：项目维护者

## 背景

目标是建设可持续接入多种金融数据源的同步服务，而不是建设 AKShare 专用同步器。
AKShare 因免费成为首期第一优先级；Tushare 因项目当前仍有半年以上会员权益成为第二优先级。
优先级是可变的成本、权益、能力和健康策略，不是领域模型或代码边界。任一任务、应用服务或持久化模块若直接依赖供应商 SDK、HTTP 接口、DataFrame 或字段，将破坏替换能力。

## 候选方案

1. 任务直接调用 AKShare，并在失败时临时调用 Tushare。
   - 优点：代码少。
   - 缺点：任务认识具体供应商；来源逻辑、字段映射、重试和入库耦合；无法稳定增加或替换来源。
2. 建立 provider-neutral port、来源注册表/路由器、独立适配器、标准化、质量门和统一提交管线。
   - 优点：SDK 与核心完全隔离；来源顺序、能力和切换可配置；质量与血缘一致；新来源不修改同步核心。
   - 缺点：需要维护 canonical schema、映射和契约样本。
3. 先把各来源原样写入来源专属表，再由离线 SQL 合并。
   - 优点：最大限度保留原始结构。
   - 缺点：来源 schema 泄漏到数据库；合并和版本管理复杂；不适合作为在线数据权威。

## 决策

采用方案 2。

- 应用层只定义并依赖 `DataSourcePort` 和来源无关的请求、结果、错误类型。任务、scheduler、应用服务、领域模型、质量管线和持久化层不得 import、实例化或调用任何具体来源 SDK/adapter。
- `SourceRegistry` 通过依赖注入注册 adapter。未来路由器根据 capability、配置顺序、成本/权益、限流和健康状态选择 adapter；只有 bootstrap/DI 组合根可以引用具体 adapter 类型。
- 只有 `infrastructure/providers/<provider_id>/` 可以 import 对应 SDK、调用供应商 HTTP 接口或处理供应商字段。adapter 只负责获取与转换为 `ProviderBatch`，不得直接访问 canonical repository 或写数据库。
- 首期默认路由为 `akshare → tushare`：AKShare 第一优先级因为免费，Tushare 第二优先级因为现有会员权益。顺序存入数据集策略，可在会员、价格、权限或质量变化时无代码调整。
- Tushare 不只是“AKShare 挂掉时的硬编码兜底”；当 AKShare 不支持某数据集、质量不达标、被限流或策略被调整时，路由器可以选择 Tushare。未来来源遵循相同机制。
- provider ID 是开放字符串和配置数据，不在领域枚举、任务名称、数据库表名或跨服务契约中硬编码 `akshare`/`tushare`。
- CI 必须运行架构测试：AST 扫描禁止 adapter 目录外出现供应商 SDK import/调用，禁止应用层引用具体 adapter，禁止 adapter 引用 persistence/canonical repository。
- 新增来源必须只新增 adapter、注册配置和 capability；不得要求同步核心直接识别具体供应商。

当前 `0001-data-sync-foundation` 只建立 port、注册/依赖注入边界、fake adapter 和架构测试，不安装或调用 AKShare/Tushare，不实现真实 adapter、来源路由、fallback、标准化、质量校验或持久化。上述行为分别在后续同步执行与数据模型方案中细化。

## 后果

- AKShare/Tushare 字段变动只影响对应适配器和样本，不直接触发数据库 schema 或同步核心变化。
- 来源成本、会员有效期、权限和优先级需要运维维护；Tushare 会员变化只修改配置，不要求发版。
- 真实 adapter、能力探测、fallback、差异对账和审计要求留给后续方案；任何后续实现仍必须遵守本 ADR 的适配层边界。

## 替代关系

无。
