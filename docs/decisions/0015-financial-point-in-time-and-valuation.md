# 0015：财务点时模型与估值序列边界

- 状态：Proposed
- 日期：2026-07-27
- 决策者：项目维护者（待评审）
- 关联方案：[财务报表与估值技术方案](../service-data-sync/0016-financial-statements-valuation/index.html)
- 关联决策：[ADR-0009](0009-equity-data-source-and-serving-boundary.md)
- 关联契约：
  [同步服务内部接口](../contracts/0013-data-sync-financial-valuation-internal.openapi.yaml)、
  [service-api 对外接口](../contracts/0014-service-api-financial-valuation.openapi.yaml)

## 背景

财务报表同时具有报告期、公告或更新日期、平台观测时间和平台获知时间。今天回补一份十年前报表，
只能证明平台今天取得该版本，不能证明十年前的策略当时可以看见它。供应商主要指标、平台派生指标和
估值时序又分别拥有不同方法学、单位与修订行为；把它们塞进同一张“财务指标”表会制造前视偏差和
不可审计的口径混用。

AKShare 1.18.78 提供东方财富三表、主要指标以及百度估值候选，也有新浪三表候选。现有探针只证明
候选函数及短时响应，不证明许可、持续稳定、历史修订、公告时点或字段语义。ADR-0009 已明确本项目
不采用 Tushare，并要求每项 capability 独立准入。

## 约束

- `service-data-sync` 是财务 canonical 数据、原始证据和发布版本的唯一所有者。
- 外部 SDK、URL、参数和供应商字段只能存在于 adapter；adapter 不得写 canonical 数据库。
- `service-api` 只能调用版本化内部 HTTP API，不得读取同步库、对象存储或供应商。
- 当前没有财务或估值新增来源获得生产批准；生产启用受许可、稳定性、语义和点时性门禁阻塞。
- 金额、比率和估值使用 PostgreSQL `NUMERIC` 与 API 十进制字符串，不使用二进制浮点。

## 候选方案

### 方案 A：每个报告期只保留最新一行

实现简单，但覆盖更新会抹掉旧版本，也无法回答“某时点知道什么”。拒绝。

### 方案 B：以供应商更新时间作为历史可见时间

字段存在时读取容易，但供应商更新时间不等于首次公告时间；回补时还可能把当前汇总结果错误回填到过去。
拒绝。

### 方案 C：双时态修订、能力隔离、保守获知时间

每个逻辑事实追加修订，分别记录业务有效区间与系统知识区间。无可信公告时间时，以本次
`observedAt` 作为最早 `knownAt/effectiveFrom`，并标记依据和置信度。报表事实、供应商指标、
平台派生指标和估值分别存储与发布。采用。

## 决策

### 1. 时间语义

每个发布修订至少保存：

- `reportPeriod`：报表或指标对应的会计期末；不是可见时间。
- `announcementDate`：上游明确提供且语义通过治理后才写；缺失保持 `null`。
- `providerUpdateDate`：供应商更新字段；只作来源事实，不自动等于公告时间。
- `observedAt`：平台成功取得该 payload 的带时区时间。
- `effectiveFrom/effectiveTo`：该版本按公开信息可使用的半开业务区间。
- `knownFrom/knownTo`：平台实际知道该版本的半开系统知识区间。
- `knowledgeBasis`：`OFFICIAL_ANNOUNCEMENT`、`PROVIDER_UPDATE` 或 `OBSERVED_AT`。
- `knowledgeConfidence`：`HIGH`、`MEDIUM` 或 `CONSERVATIVE`。

可信官方公告日期存在时，`effectiveFrom` 可取公告日，`knownFrom` 仍不得早于平台首次实际取得时间；
历史回放同时应用 `asOf` 和 `knownAt`。只有供应商更新时间时，必须先证明它代表对市场公开的时间，
否则采用 `observedAt`。回补数据永不把 `knownFrom` 回填到历史报告期或未知公告日。

同一逻辑事实值或口径变化时，事务内关闭旧修订的两个当前区间并追加新修订。迟到的可靠公告日期属于
新知识：追加修订，不重写旧知识历史。

### 2. 数据集与方法学隔离

以下 capability 独立存储、质量门禁和发布：

| Capability | 事实性质 | 禁止行为 |
| --- | --- | --- |
| `financial.statement.reported` | 三大报表披露行项目 | 不转成单季或 TTM 后覆盖源值 |
| `financial.metric.provider` | 供应商计算的主要指标 | 不伪装成披露事实，不与平台公式拼接 |
| `financial.metric.derived` | 平台按版本公式计算 | 不缺失输入血缘，不覆盖供应商指标 |
| `equity.valuation.observation` | 指定来源、方法的日期序列 | 不跨来源补洞，不把抓取日宣称为官方终值 |

每种方法学拥有稳定 code、version、来源、适用范围、计量单位、基数和状态。不同版本不组成一条无标记连续
序列。报表保存上游原始累计口径；资产负债表为时点，利润表与现金流量表为累计。单季与 TTM 只能进入
独立派生表，并记录输入报表修订集合与公式版本。

### 3. 报表口径

逻辑报表和每个事实必须明确：

- `statementType`：`BALANCE_SHEET`、`INCOME_STATEMENT`、`CASH_FLOW_STATEMENT`。
- `periodBasis`：`POINT_IN_TIME`、`YEAR_TO_DATE`、`SINGLE_QUARTER` 或 `TTM`。
- `statementScope`：`CONSOLIDATED`、`PARENT` 或 `UNKNOWN`。
- ISO 4217 币种、上游单位、标准单位与精确缩放因子。
- 报告类型、审计状态和来源方法学；未知值保持显式 `UNKNOWN`。

禁止用虚构 ISO 代码表达未知币种，也禁止静默推断合并范围、单位、审计状态、累计/单季或报告类型。
报表头和事实使用可空币种加必配 `currencyNullReason`；数值事实使用可空 `value` 加必配
`nullReason`，两组字段都强制二选一。非货币项目标记 `NOT_APPLICABLE`，来源空值保留明确原因，
不会被写成 0 或直接消失。单位换算只能由版本化映射完成，并保留原单位与转换规则。

### 4. 字段治理

报表行项目和指标只接受已审核的 metric dictionary。未知上游字段进入 quarantine，保存字段名、
schema fingerprint、样例摘要和批次引用；不会被丢弃，也不会写入未治理 JSON/EAV 列。字典变更需要
fixture、单位、符号和适用报表类型评审后，以 migration 或受控种子版本发布。

### 5. 来源准入

继承 ADR-0009：不采用 Tushare。AKShare 1.18.78 候选只允许 local/research shadow：

- 东方财富：
  `stock_balance_sheet_by_report_em`、`stock_profit_sheet_by_report_em`、
  `stock_cash_flow_sheet_by_report_em` 及三个 `*_by_report_delisted_em`；
  `stock_financial_analysis_indicator_em(..., "按报告期"|"按单季度")`。
- 百度股市通：`stock_zh_valuation_baidu(symbol, indicator, period)`。
- 新浪：`stock_financial_report_sina` 只保留研究候选，不是获批主源。

任一 capability 进入生产前必须分别通过：

1. 上游许可、商业使用、请求频率、缓存与留存批准。
2. 至少 5 个连续交易日的稳定性门禁；正式评审目标为 30 个交易日，并覆盖沪深北、正常与退市样本。
3. 字段、单位、累计/单季、合并范围、公告/更新时间、修订和空值语义 fixture。
4. 点时性门禁：能证明公告时间，或明确接受保守 `observedAt`、禁止历史可见性声明。
5. 独立 source policy 评审；不因同名字段自动 fallback 或跨源拼接。

未通过时可以实现 schema、任务和 shadow 质量报告，但不得创建生产 publication。

### 6. 服务边界

`service-data-sync` 通过内部只读接口暴露已发布报表、指标与估值。`service-api` 新增财务读取模块和防腐
client，沿用现有 JWT、角色和限流，只投影公开字段。首期所有 ACTIVE `USER`、`ADMIN`、
`SUPER_ADMIN` 可读；细粒度权限仍需鉴权评审。Redis 不保存财务权威数据或未标记缓存。

报表列表、财务指标和估值序列都必须显式传入 `methodologyCode + methodologyVersion`；
报表详情因 `reportRef` 已唯一绑定方法学而例外。每次查询只选择一个
`financial_publication(methodology_id, data_version)`，cursor 绑定同一 code、version 和 dataVersion。
不同方法学或版本绝不在一个页面、序列或默认“当前版本”中静默合并。

契约 0013 与 0014 分别替代契约 0003 与 0004 中基于 `instrumentId` 的财务报表、财务指标路径，
改用 `exchange + symbol`，并新增估值路径；证券目录、K 线、复权因子和公司行动路径不受影响。

### 7. 观察批次与证券身份依赖

本能力不另建共享批次语义。它依赖 0014 的 `source_batch` expand：
`UNIQUE(run_id, partition_key, observation_seq)`；`attempt` 只属于 `sync_partition`，
payload hash 非唯一，只用于查询重复内容和复用内容寻址 raw 对象。

历史报表不能按当前 `(exchange, symbol)` 直接 `get_or_create`。任务必须按报告/观测适用日期调用
0014 的 date-aware identity resolver；无法唯一解析或出现身份区间歧义时进入 quarantine，不写临时公开身份。

## 后果

- 能可靠支持无前视偏差的 `asOf + knownAt` 查询，并保留供应商迟到修订。
- 表和任务数量增加，但每类方法学可以独立准入、回滚、重算与退役。
- 完整历史回补成本高，必须先摘要变更检测，再按证券与报表拉取全量。
- 无可信公告日期的历史数据可查询，但只从平台实际观测时点起可见；这会降低历史覆盖，却避免虚假回测。
- 未知字段会阻断对应分区发布，短期可用率低于“全部塞 JSON”的方案。

## 回滚

本 ADR 在无生产 publication 前可通过禁用 source policy、停止任务和回滚新增 API 路由撤销。已有发布后，
不删除修订或回写 `knownFrom`；回滚只把当前 publication 指针切回上一已验证 `dataVersion`，暂停新任务，
并保留表和原始证据供审计。破坏性删表必须经过独立数据保留评审。

## 待决

- 各候选来源的生产许可、请求预算、缓存和 raw retention。
- 东方财富公告/更新字段在正常、修订、退市和金融行业样本上的精确定义。
- 百度估值总市值单位、日期截点、复权/股本口径与修订行为。
- 首批平台派生指标清单、公式版本和会计准则差异处理。
- 30 个交易日验收是否作为正式生产硬门槛；当前最低门槛仍为 ADR-0009 的 5 个交易日。
