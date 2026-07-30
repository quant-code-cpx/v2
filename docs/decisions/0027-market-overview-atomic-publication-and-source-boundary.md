# ADR-0027：市场概览原子发布与生产数据源边界

- 状态：Accepted
- 日期：2026-07-30
- 决策者：quant-v2 维护者
- 所有者：`service-data-sync`
- 主要消费者：`service-api`、`service-web`
- 关联方案：[市场概览与行业板块三服务方案](../service-web/0008-market-overview-and-sectors/index.html)
- 关联契约：
  [内部读取契约](../contracts/0026-data-sync-market-overview-internal.openapi.yaml)、
  [公开 POST 契约](../contracts/0027-service-api-market-overview.openapi.yaml)

## 背景

仓库已经拥有板块目录、板块行情、板块观察成分、申万分类、部分资金流模型以及 publication 基座，
但这些能力不能直接回答同一交易日的市场整体表现：

- 主要指数行情仍缺少正式 production publication，指数成分观察快照不是指数行情；
- 缺少同一交易日的全市场股票横截面，因而不能可靠计算市场宽度、沪深成交额、涨跌停和股票排行；
- 板块 EOD 的观察总体、来源和粒度不同，不能替代股票市场宽度；
- 资金流存在东财大盘、东财板块和 Tushare 订单规模等不同方法学，不能相加为“统一市场事实”；
- 逐组件读取各自 latest 会把不同交易日、不同 finality 的数值拼成伪同屏。

本决策在不改变三个服务所有权的前提下，冻结生产来源、canonical 粒度、完整性门禁、跨服务读取和
页面降级语义。

## 决策

### 1. 生产来源与启动门禁

1. 市场概览与本次补齐的板块能力只允许使用 Tushare Pro production adapter。AKShare 仍可用于隔离研究
   和交叉校验，但不能进入 production publication，也不能在 Tushare 失败时静默兜底。
2. 部署必须通过 secret 注入 `DATA_SYNC_TUSHARE_TOKEN`，并显式启用 Tushare 与市场 bundle 能力。
   token 不得进入代码、镜像、日志、错误正文、指标标签或 source payload。
   `DATA_SYNC_MARKET_DATA_LICENSE_SCOPE` 同时是启动门禁：本地研究可显式使用
   `personal-research`，预发和生产只接受 `commercial-redistribution-approved`，并必须提供可审计的
   `DATA_SYNC_MARKET_DATA_LICENSE_REFERENCE`。worker/scheduler 持有第三方凭据，内部只读 API 不持有。
3. worker 在接收生产任务前执行无副作用 preflight：逐一调用冻结的接口与最小字段集，验证授权、
   schema、最新已完成交易日、分页/单次行数上限和关键 identity。Tushare HTTP 业务错误、权限拒绝、
   schema 漂移和不足以完成全量截面的行数均为硬失败。
4. 生产账户以方案冻结的 6000 积分等级作为采购基线，但运行资格以全部目标端点真实探针成功为准；
   不能只根据积分数推断权限。
5. 所有 provider 请求固定使用 `https://api.tushare.pro`；重定向后的 scheme 仍必须为 HTTPS。不得接受
   HTTP 降级、在日志中记录 token，或在权限失败时切换到另一来源。

### 2. canonical 数据集与派生边界

`service-data-sync` 以 provider-neutral canonical 保存以下不可变数据集：

- `market.calendar` 与 `market.session-schedule`：场所、交易日和 schedule version；
- `index.quote.eod` 与 `index.bar.1d`：指数 identity、交易日和 source revision；
- `equity.market-snapshot.eod`：市场固定为 CN-A-SSE-SZSE，携带交易日、cutoff、universe version；
  行粒度为证券 identity，北交所证券不混入 P0 首页宽度、成交额或排行；
- `market.breadth.eod`、`market.turnover.eod`、`market.limit-breadth.eod`：
  只从同一股票横截面 publication 派生；沪深 A 股成交额另以 Tushare `daily_info` 的 SH_A/SZ_A
  同口径交易所统计做独立质量校验；
- `equity.market-ranking.eod` 与 `market.attention-signal.eod`：
  固定输入 publication、排行/规则版本和稳定 tie-break；
- `money-flow.market.dc.eod`、`money-flow.equity.order-size.eod`、
  `sector.money-flow.dc.eod`：按来源与方法学物理、逻辑隔离；
- 东财行业/概念目录、日/周/月 K 线、EOD、成分和强弱；
- 申万 L1/L2/L3 taxonomy、正式成分、行情与 source-reported 估值；
- `market.overview-bundle.eod`：只保存组件 manifest 与 current pointer，不复制业务明细。

金额在 canonical 和跨服务 JSON 中使用 Decimal CNY；百分比使用百分点；来源空值保持 `null`。所有派生
publication 必须保存输入版本、universe、规则/方法学版本、来源绑定、质量评估、`asOf` 和 finality。

### 3. 首页写时组合、读时原子

1. bundle coordinator 只接受同一交易日、production、final、质量全部通过的必需组件。
2. 四个固定指数、股票横截面、宽度、成交、涨跌停、股票排行、资金流和行业/概念排行任一缺失时，
   不推进 current pointer。已经成功的组件仍可供运维与详情查询。
3. latest 首页始终返回最新完整真实 bundle。若当日尚未完整，继续返回上一完整 bundle，并携带
   `latestAttemptedTradeDate`、落后原因和组件质量摘要；不得补零、混用日期或读取 fixture。
4. 精确请求一个没有完整 bundle 的交易日返回 404；从未形成任何完整 bundle 返回 503。
5. 修订产生新 data version。历史日期重跑只生成 candidate，不自动改写 active 历史链；只有当前时间线
   tip 可执行显式 rollback/forward，并在同一事务中重置受影响的派生输入指针。旧 bundle、组件和质量
   证据永不删除，避免历史修订使后续 5/20 日强度或周/月周期结果失去可复验输入。

### 4. 三服务调用边界

1. `service-data-sync` 独占同步数据库，完成全量采集、canonical 持久化、聚合、质量门和 publication。
2. `service-api` 只经版本化内部 HTTP 读取 publication；不得访问同步数据库，也不得在请求时扫描明细表
   重新计算宽度或拼接多个 latest。
3. `service-api` 的公开市场路由全部使用 `POST`，负责会话、授权、DTO、游标、条件请求和稳定错误映射。
   内部 304 映射为公开 204。
4. `service-web` 只调用 `service-api`。市场首页使用单一 bundle query；详情页按独立 publication 查询，
   每个区域显示自己的 `asOf`、data version、质量和错误。
5. 股票排行只提供摘要及指向股票中心的 `exchange + symbol` 深链接，本模块不复制个股详情。

### 5. 方法学与可比性

1. 指数行情只来自对应指数行情数据集。指数成分与权重只能解释构成，不能生成或替代指数点位、涨跌或 K 线。
2. 市场宽度、成交额、涨跌停和股票排行只基于经过完整性门禁的同日股票横截面。沪深 A 股成交额
   只与 `daily_info` 的 SH_A/SZ_A 同 universe 对账；`index_daily.amount` 仅表示对应指数成分成交额，
   禁止作为全市场成交额或质量校验基准。
3. 东财行业、东财概念与申万行业保留各自 scheme、taxonomy、成员和来源语义；不建立隐式等价映射，
   不跨 scheme 合榜。
4. “活跃”固定定义为成交额排行；持续性使用冻结的 5/20 个有效交易日方法学并返回有效样本数。
5. 大盘 DC、板块 DC 与股票订单规模资金流分别展示来源、方法学、版本、总体和 coverage；禁止跨方法学
   相加、对冲或描述为统一市场事实。

### 6. 失败、缓存与恢复

1. Provider 网络失败、429 可在同一幂等任务中按预算重试；鉴权失败、schema/单位漂移和质量阻断不可
   盲目重试，必须隔离并产生稳定错误码。
2. API 缓存键包含 route、规范化请求、data version 与 projection version；带版本游标不能跨 publication
   使用，版本变化返回 409。
3. 首页不得以 API 进程内旧对象代替权威 current pointer。data-sync 读端可用时，已发布旧 bundle 仍可
   正常服务；读端不可用时 API 返回 503。
4. Web 保留成功区域并对失败的详情区域局部重试；禁止用 EOD 点位伪造 K 线、用旧来源值填空或把
   `assembledAt` 当作行情更新时间。
5. EOD 完成资格固定为 Asia/Shanghai 交易日 17:20 后，但完整 bundle 还依赖官方 19:00 更新的
   `moneyflow`。`market.overview-and-sectors.bundle` 只允许配置一个 `TRADING_DAY`、Asia/Shanghai、
   19:20 的持久化 schedule；17:20 不是同步触发时间。执行器每次都扫描 correction lookback 内全部
   应发布但缺失的交易日，因此 T 日失败不会在 T+1 被永久跳过。同次任务只做有界网络重试；仍失败时
   由运维调用 `/commands/retry`，或由次日 incremental 补洞，不虚构 19:35/19:50 自动触发。
   17:20 至 20:00 的页面 stale 状态每 15 分钟重新查询，20:00 后停止自动轮询并保留明确的新鲜度原因；
   Web 轮询不等于同步调度或重试。
6. 指数跨日 K 线和其他跨 publication 响应以全部 active 输入版本生成稳定 composite `dataVersion`，
   同时返回有序且去重的 `inputDataVersions`；ETag、响应头和响应体必须引用同一 composite 版本。

## 实施与回滚

1. Expand：增加 canonical 表、Tushare adapter、preflight、内部读取和公开 DTO；旧能力不变。
2. Backfill：先交易日历与四指数，再股票横截面及派生，随后资金流、东财板块与申万；首次启动至少
   回补形成 20 个共同有效交易日窗口以及当前自然周、自然月所需输入。
3. Shadow：生成 bundle 候选但不推进页面指针，核对集合、金额单位、coverage、来源和响应 schema。
4. Cutover：只有合法授权引用、真实来源 preflight、同日全量质量门、迁移与回补、三服务契约测试和
   1440×900 浏览器验收全部通过，才启用同步和 current pointer。无合法 token/授权时允许代码、
   fail-closed 与容器链路验收，但不得宣称真实 publication 已上线。
5. 回滚：关闭公开功能开关并把 current pointer 切回上一已验证 bundle；不回退 schema，不删除新数据，
   不切换到 AKShare 或 fixture。

## 后果

- 首页新鲜度受最慢必需组件约束，但用户看到的始终是可解释、同日、完整的真实截面。
- 同步服务增加全量采集、质量计算和发布协调成本；API 与 Web 的读路径相应更简单且可验证。
- Tushare token、目标端点权限和网络出口成为明确部署前置项。缺失时 preflight 可观测地失败，而不是
  形成运行时隐患或上线假数据。
