# 0016：日频资金流方法学、来源与服务边界

- 状态：Proposed
- 日期：2026-07-27
- 决策者：项目维护者（待评审）
- 关联方案：[日频资金流同步与 API 技术方案](../service-data-sync/0017-daily-money-flow/index.html)
- 关联契约：
  [同步服务内部接口](../contracts/0015-data-sync-daily-money-flow-internal.openapi.yaml)、
  [service-api 对外接口](../contracts/0016-service-api-daily-money-flow.openapi.yaml)
- 继承决策：[ADR-0002](0002-data-sync-ownership-and-access.md)、
  [ADR-0004](0004-market-data-provider-adapters.md)、
  [ADR-0009](0009-equity-data-source-and-serving-boundary.md)、
  [ADR-0010](0010-sector-taxonomy-and-derived-data-boundary.md)、
  [ADR-0013](0013-equity-instrument-identity-lifecycle.md)

## 背景

行情软件中的“主力资金流入”“超大单”“大单”等通常是供应商根据成交方向和自有分桶规则计算的派生指标，
不是市场中可直接观测的现金增减。相同中文标签不能证明方向判定、订单阈值、统计范围、分母、窗口或修订规则相同。

AKShare 1.18.78 暴露以下研究候选：

- 东方财富 `stock_individual_fund_flow(stock, market)`：单只证券近期约 100 个交易日；
- 东方财富 `stock_individual_fund_flow_rank(indicator)`：全证券“今日/3日/5日/10日”排行快照；
- 东方财富 `stock_sector_fund_flow_rank(indicator, sector_type)`：行业、概念或地域的“今日/5日/10日”排行快照；
- 东方财富 `stock_sector_fund_flow_hist(symbol)`：单个行业的近期日序列；
- 东方财富 `stock_market_fund_flow()`：全市场近期日序列；
- 同花顺 `stock_fund_flow_individual/industry/concept(symbol)`：即时及 3/5/10/20 日供应商排行页。

东方财富个股、行业和市场历史接口主要返回主力、超大、大、中、小单的净额和净占比，不返回可验证的
gross inflow/gross outflow。同花顺即时页包含流入、流出和净额，滚动排行页则是供应商窗口快照，
不能伪装成逐日历史。AKShare 文档没有给出东方财富净额金额单位，也没有证明两家供应商方法学等价。

2026-07-26 的固定版本隔离探针中，东方财富个股、板块和市场资金流已连续两轮出现
`RemoteDisconnected`、`ReadTimeout` 或 `JSONDecodeError`。同花顺实现通过 HTTP 页面、JS 令牌和
`py_mini_racer` 解析，增加运行时、页面结构和合规风险。任何候选均未获得生产许可或稳定性批准。

## 约束

- 本能力只处理日频来源观测和供应商滚动窗口排行快照；完全排除分钟、分时、盘中轮询、推送和实时订阅。
- `service-data-sync` 独占 raw evidence、canonical 数据、修订、质量和 publication。
- SDK、URL、中文字段、单位解析和供应商错误只存在于独立 adapter；adapter 不写 canonical PostgreSQL。
- `service-api` 只通过版本化内部 HTTP 读取，不直连同步库、S3、AKShare 或上游网站。
- `service-api` 不复制 Prisma 资金流表，Redis 不保存权威业务数据。
- A 股证券身份依赖 0014/ADR-0013；板块身份依赖稳定 scheme/code。成份关系可解释覆盖范围，
  但不得用于自行计算或补齐供应商板块资金流。

## 候选方案

1. 把不同供应商的同名字段写入一条 `security + date + bucket` 序列。
   - 优点：表面覆盖率高，查询参数少。
   - 缺点：来源、阈值、分母和算法差异被抹除，结果无法审计或稳定回测。
2. 为来源和方法学建立强身份；历史日序列与供应商排行快照独立存储、独立发布。
   - 优点：语义可解释，修订可回放，缺口不会被异口径数据静默污染。
   - 缺点：消费者必须先选择方法学；数据不可用时明确返回缺口或 503。
3. 用供应商 3/5/10/20 日排行反推逐日值。
   - 优点：减少逐证券历史接口调用。
   - 缺点：滚动窗口不可逆，窗口成份和修订未知，反推值不是来源事实。
4. 用证券日序列按当日板块成份求和，冒充供应商板块或市场数据。
   - 优点：看似减少上游调用。
   - 缺点：供应商样本池、方向算法和覆盖可能不同；会制造错误同名指标。

## 决策

采用方案 2；方案 1、3、4 禁止。

### 数据集分离

- `money-flow.daily-series` 只保存上游明确给出的逐交易日观测；一个 series 绑定一个来源、方法学版本、
  scope、universe、bucket、窗口、分母、方向、最终态、币种和单位。
- `money-flow.supplier-ranking` 只保存一次供应商排行页的完整快照、页面顺序、窗口和快照截点。
  “3/5/10/20 日”是供应商报告窗口，不是内部日序列求和。
- 同一响应中的多个 bucket 作为受治理的固定列组或 bucket 定义行保存；未知新字段进入 quarantine，
  不塞入通用 JSON/EAV。
- gross inflow、gross outflow、net amount、net ratio 均允许为空。源端没有 gross 值时保持 `null`，
  禁止由净额反推；源端没有分母时 `ratio_denominator=unknown`。
- 方法学版本必须显式声明 `supported_measures`。不支持的 measure 在每个观测中恒为 `null`；
  已声明支持但单点为 `null` 是数据缺失，进入对应质量门，禁止让同一个 null 暗中表达两种语义。

### 方法学身份

每个 canonical series 或 ranking snapshot 必须绑定：

- `adapter_provider`、`upstream_source`、`source_dataset`；
- `semantic_family`、`methodology_id`、`methodology_version`、`methodology_status`；
- `scope_type`、稳定 scope 身份、`universe_id` 与 universe 版本；
- `window_type`、`window_size`、bucket 定义、占比分母和正负方向；
- `trade_date` 或目标交易日、`as_of_time`、`finality`；
- `currency`、`raw_unit`、标准单位、换算版本、`observed_at` 和 source batch。

无法获得算法或阈值时标记 `unknown`，不是补写合理猜测。方法学、分桶、分母、样本池、单位或来源变化时，
新增 methodology version 和 series；旧序列只关闭发布，不覆写。

### 来源不可混用

- 东方财富与同花顺永远以不同 `upstream_source`、`source_dataset` 和 methodology version 保存。
- 主来源失败时不自动 fallback，不从次来源补字段，不合并排行，不用一家的净额与另一家的成交额算占比。
- 个股、板块、市场是不同 scope；不得把个股求和结果冒充供应商板块或市场值。
- `stock_individual_fund_flow_rank`、`stock_sector_fund_flow_rank` 和同花顺排行页只进入 supplier ranking；
  不用于回补 daily series。
- 当前 AKShare rank 函数只向调用方返回内部翻页合并后的 DataFrame，不暴露逐页 raw 或可靠 upstream total。
  `sdk_returned` 只能证明一次 SDK 调用正常返回，不能单独满足生产完整性；若实现 direct HTTP/page-aware
  adapter，必须作为新 adapter/source policy/schema fixture 重新准入。
- `money_flow_ranking_manifest` 因此只登记一次 merged-observation evidence：
  source batch、返回行数、可空 upstream total、completeness basis 和 complete 状态；它不是逐页 manifest。
- 来源切换需新 ADR、双跑差异报告、方法学新版本、数据断点标记和消费者迁移。

### 时间与修订

- 日序列 `trade_date` 来自上游日期并须通过权威 A 股交易日历校验；历史回补的 `known_from` 是实际观测/
  发布时间，不能回填成历史当日已知。
- 排行页无可靠来源日期时，目标交易日由权威日历和配置化收盘后截点确定；
  `finality=post_close_observation` 只表示策略截点后的观测，不宣称交易所或供应商官方终值。
- 同一逻辑日期内容不变：仍登记独立 source batch/raw 观测，但 canonical no-op，不推进 `dataVersion`。
- 内容变化且质量通过：追加 revision、关闭旧知识区间并原子推进 publication；旧版本永久可审计。
- 交易日历未知、目标日缺失、页面不完整、字段漂移、单位突变或身份解析失败均阻断对应分区发布。

### source batch

0014 证券主数据方案是共享 `source_batch` expand 的唯一权威：

- 每次外部获取独立记录 `run_id + partition_key + observation_seq`，其中 `run_id` 归属 0014 统一的
  `sync_run`，分区重试归属 `sync_partition`；
- `UNIQUE(run_id, partition_key, observation_seq)`，`attempt` 只属于 `sync_partition`；
- payload hash 是普通查重索引和 raw object 字节去重键，不再是观测唯一身份；
- schema fingerprint、adapter version、upstream source、observed time 和 raw URI 必须留痕。

本能力依赖该兼容迁移，不再定义另一套共享 batch 身份。

### API 与服务边界

- 先发布方法学目录，再提供显式 daily-series 和 supplier-ranking 路由；不存在含义模糊的通用
  `/money-flow` 数值接口。
- daily-series 请求必须指定 methodology id/version、scope、bucket 和交易日范围；`knownAt` 用于无未来信息读取。
- 个股范围内每个 `tradeDate` 都通过 0014 时点身份解析。整个请求范围必须解析到同一 canonical security；
  历史代码复用导致跨身份边界时返回 409 并提示按边界拆分，cursor 同时绑定该内部身份。
- supplier-ranking 请求必须指定 methodology id/version、scope type、window type/size、bucket，
  并选择 `latest` 或精确 `tradeDate`。
- cursor 绑定完整查询和 `dataVersion`；revision 替换后旧 cursor 返回 409。ETag 绑定发布版本与投影。
- 内部契约 0015 由 `service-data-sync` 实现；公开契约 0016 由独立 `MoneyFlowModule` 通过内部 HTTP client 投影。
- 公开 API 要求有效 JWT 和 ACTIVE 用户角色；剥离内部 UUID、raw URI、adapter id、source batch 和质量样本，
  但保留来源名称、数据集、方法学、窗口、bucket、分母、方向、最终态、币种、单位和质量状态。
- 内部目录可展示 research 方法供受控诊断；公开目录只投影 `production_enabled=true` 的已发布 validated 方法。
  当前没有获批来源时返回 503，不能以空数组或 research 数据伪装正常生产能力。
- 下游超时、契约错误、熔断或无可发布版本映射为 Problem Details 503；不返回未标记陈旧值。

### 生产准入

所有 source policy 默认 `production_enabled=false`。任一 capability 进入生产必须同时通过：

1. 上游商业使用、采集、原始保存期限、内部使用和 API 再分发许可；
2. 固定 AKShare/adapter 版本，至少连续 5 个交易日，优先 30 个交易日的稳定性观测；
3. 字段单位、正负方向、算法/阈值、占比分母、交易日归属、窗口和最终态验证；
4. 完整性、身份解析、修订、限流、调用预算、超时和 schema drift 演练；
5. 经评审的 source policy 与独立生产开关。

全市场逐证券日序列约需数千次单证券调用/交易日；在供应商预算和新鲜度无法同时满足前，
只能运行有界 research/shadow universe，不承诺全 A 股 daily series。

## 后果

- 消费者必须先读取方法学目录，不能只凭“主力资金”中文名称查询。
- 覆盖率可能低于自动拼接方案，但每个值可解释、可复验，错误不会跨方法学扩散。
- historical series 与 supplier ranking 可独立上线、修订和停用；任一来源失败不会污染另一数据集。
- PostgreSQL 行数与 raw 对象量较大，需要按交易月分区、冷热保留和明确 provider 调用预算。
- `service-api` 新增只读下游依赖和 503 失败面，但不增加 Prisma canonical 表或 Redis 业务缓存。

## 生产前待决

- 东方财富、同花顺和 AKShare 的商业保存、采集频率与对外再分发许可。
- 东方财富净额金额单位、主力/分桶阈值、占比分母与最终态。
- 同花顺页面字段的金额后缀换算、JS 令牌运行依赖和页面结构稳定性。
- 全市场逐证券日序列的调用预算、目标覆盖 universe 和新鲜度。
- 权威 A 股交易日历、收盘后截点、连续验收窗口和 raw/revision 保留期。

## 替代关系

本 ADR 细化 ADR-0010 中“资金流按来源与方法学独立发布”的原则，不改变行业/概念分类、三周期行情、
板块 EOD 或成份关系。契约 0015/0016 是日频资金流独立增量能力，不修改 0003/0004 的行情、复权和财务路径，
也不修改 0005/0006 的板块目录与 K 线路径。
