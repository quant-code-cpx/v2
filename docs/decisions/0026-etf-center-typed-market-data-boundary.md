# ADR-0026：ETF 中心 typed market-data 全链路边界

- 状态：Accepted
- 日期：2026-07-30
- 决策者：quant-v2 维护者

## 背景

仓库已经具备 ETF 主数据、日线、净值和状态的领域模型与部分同步代码，但原有 typed
market-data 描述符没有形成可供 Web 稳定消费的完整生产链路，ETF 目录来源还存在按代码前缀猜测
交易所、把目录可见性当作上市状态、缺少产品名称等问题。

“基金与 ETF 中心”不能把任务名称中的“基金”解释为所有基金类型已经可用。当前仓库没有形成场外
公募基金、LOF、REITs、普通货币基金或其他交易所基金的完整目录、净值、状态和公开查询契约；第一阶段
必须只交付由来源明确标注为 ETF 的上交所、深交所上市 ETF。交易所目录明确归为 ETF
的货币市场产品仍属于一期产品目录，但其收益型 NAV 不能冒充普通单位净值。

## 候选方案

1. 为 Web 增加独立 ETF 聚合接口，由 `service-api` 临时拼接或补齐数据。页面接入快，但会在 API
   层复制数据口径，并容易用空值、推导值或假数据掩盖同步缺口。
2. Web 直接读取第三方或同步库。链路短，但破坏服务边界、鉴权、publication、质量与版本语义。
3. 沿用统一 typed market-data 网关，为 ETF 增加严格的 schema v2，并让四类数据独立发布、独立
   查询和独立失败。

## 决策

选择方案 3，并作出以下约束：

- 第一阶段仅支持来源目录明确分类为 ETF 的 `SSE`、`SZSE` 产品。不得通过代码前缀推断交易所或
  基金类别；目录本次缺席不得解释为退市。来源未给出明确上市状态时发布 `UNKNOWN`，不得默认
  `LISTED`。
- `service-data-sync` 是 canonical 数据与 publication 的唯一所有者。ETF 目录使用交易所官方目录；
  未复权日线使用腾讯行情，NAV、申购和赎回状态使用东财净值明细。上述来源均封装在 provider
  adapter 内，并保留明确的来源、字段指纹和 publication 证据。来源不可用时发布明确的
  availability 证据，不得回退 fixture、缓存样例或合成业务值。
- 沿用 `POST /internal/v1/market-data/query` 与认证后的
  `POST /api/v1/market-data/query`，contract envelope 保持 `1.0.0`，新增并保留以下
  `schemaVersion=2` typed dataset：
  - `fund.etf.profile.reported`
  - `fund.etf.bar.1d.reported`
  - `fund.etf.nav.1d.reported`
  - `fund.etf.trading_state.reported`
- schema v1 与其他 dataset 保持向后兼容。ETF v2 请求必须经过 dataset-specific 字段、筛选、
  排序、分页与响应记录校验；契约漂移不得透传到 Web。
- 四个 dataset 使用独立 publication、质量、来源、更新时间和 availability。目录成功不代表
  行情、NAV 或状态成功；任一子查询失败不得抹掉其他已成功分区。
- 来源明确报告货币市场 ETF 的收益型 NAV，而冻结合同只能安全承载 UNIT/ACCUMULATED 时，NAV
  查询返回
  `CURRENTLY_UNSUPPORTED + NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET`，records 为空且没有
  dataVersion。全量同步把这些产品固化为可审计跳过集合，不得触发全局来源熔断或拖累其他 ETF。
- NAV 与收盘价仅在各自来源和日期可见时并列或归一化比较。折溢价口径冻结前不得由 Web、API 或
  同步服务擅自计算；跟踪误差、基金规模、资金流、成分或申赎清单等未接入字段不得以占位数字展示。
- 状态维度保持 `TRADING`、`SUBSCRIPTION`、`REDEMPTION` 独立。来源只提供申购或赎回状态时，不得
  合成交易状态。
- `/market/funds` 第一阶段仅作为基金类型入口，并明确普通基金尚未接入；ETF 列表和详情独立于股票
  中心。Web 只能通过共享 POST transport 调用 `service-api`，不得访问同步库或 Provider。
- 生产验收必须覆盖真实 Provider → canonical persistence → publication → data-sync 内部查询 →
  service-api 公开查询 → Web 渲染。测试 fixture 只允许存在于测试环境，不能成为运行时 fallback。

## 后果

本决策让同步、API 与 Web 共享一个可版本化数据边界，并允许目录、行情、NAV 和状态按真实可用性
渐进呈现。代价是 ETF 首次上线前必须完成 profile 名称迁移、v2 reader/executor 注册、API 严格
校验和真实来源端到端验证；外部来源网络、许可或凭据不可用会作为可观察依赖失败暴露，而不会被假数据
掩盖。

场外公募基金、LOF、REITs、普通货币基金和其他交易所基金进入后续阶段前，必须分别补齐权威分类、产品
身份、申赎与交易语义、净值口径、publication 和授权边界，并新增或评审相应 typed dataset 契约。

## 替代关系

本决策细化 [ADR-0023](0023-personal-market-data-query-gateway.md) 在 ETF 中心的 dataset 边界，
不替代统一查询网关或空观测语义。
