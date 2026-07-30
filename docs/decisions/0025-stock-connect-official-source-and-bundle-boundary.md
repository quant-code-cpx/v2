# ADR-0025：沪深港通官方来源与原子 bundle 边界

- 状态：Accepted
- 日期：2026-07-30
- 决策人：quant-v2 maintainers
- 关联方案：
  - `docs/service-web/0010-stock-connect-center/index.html`
  - `docs/service-web/0010-stock-connect-center/service-api.html`
  - `docs/service-web/0010-stock-connect-center/service-data-sync.html`

## 背景

现有 `market.stock_connect.market_stat.reported` 与
`market.stock_connect.active_security.snapshot` 已具备部分 canonical 模型，但当前 AKShare
来源不能提供完整真实市场统计和官方活跃榜，typed reader 又丢失买入、卖出、额度、币种与字段可用性。
2024-08-19 起北向盘后披露不再提供买入、卖出与可据此计算的净额；南向成交金额使用 HKD，额度仍使用
CNY。将不同日期、币种、披露制度或来源临时拼接会制造错误市场事实。

模块需要由 `service-data-sync`、`service-api`、`service-web` 一次性打通，同时保持三个服务独立部署和
版本化边界。

## 决策

### 1. 生产来源

- 沪线、深线日终市场统计与官方活跃证券使用 HKEX Data Marketplace 获许可 Daily Statistics
  交付。
- 互联互通交易日使用 HKEX 官方年度 Calendar CSV。
- 南向证券身份使用最终可交易的 HKEX Securities Master Fixed-length Format；只投影互联互通事实引用的
  证券，不扩展为完整港股主数据。JSON 仅属于 T+2 to-be-listed 产品，不能替代正式主档。获许可布局通过
  带 SHA-256 的版本化 fixed-length profile 注入；记录长度、编码、交付日与 T+1 生效交易日任一不匹配即
  fail-closed。
- 北向日终额度/状态使用 HKEX OMD-C DQB 最终快照。
- 港股通（沪）日终额度/状态使用 SSE MDGW 最终交付。
- 港股通（深）日终额度/状态使用 SZSE STEP 最终交付。
- AKShare、东方财富、网页抓取、本地样本和随机值不得进入上述 capability 的生产来源路由，也不得作为
  缺失 publication 的 fallback。

商业账号、凭证、endpoint 和 entitlement 由部署 Secret 注入。缺失时 capability fail-closed，
保持上一批准 publication 并报警，不返回空成功或伪造数据。

### 2. 事实与派生

- reported 与 derived 使用独立 dataset/release。
- 只有同一官方 release 同时提供 buy 与 sell 时，才允许用
  `buy-minus-sell-v1` 计算市场级或来源活跃榜行级净额。
- 2024-08-19 后北向缺失买卖拆分写为 `NOT_DISCLOSED_BY_REGIME`；不填零，不从 turnover 反推。
- 活跃证券与净额排序范围固定为 `SOURCE_ACTIVE_SECURITIES_ONLY`，不得描述为全市场排行。
- 金额使用十进制字符串和显式 `currency`/`unit=BASE`。CNY、HKD 不直接求和。

### 3. 原子 publication

`service-data-sync` 创建不可变 `market.stock_connect.overview.bundle`，引用同一交易日的日历、市场统计、
活跃证券、派生净额、通道状态和身份覆盖 release。总览 latest 只解析为所选通道最后一个共同完成的 bundle；
通道 latest 可解析为该通道最后 publication；exact 不静默回退。

原子边界是“单通道 × 交易日”组件包以及“所选 channel set 已齐套”的 overview publication，
不是整个 `MARKET+ALL` run 的跨日期、跨通道数据库事务。运行中途失败时，已完成通道的不可变
publication 可继续被单通道查询读取，但四通道 latest-common 指针保持旧版；同一 fenced run
幂等重跑补齐剩余通道后，四通道 overview 才一次前移。任何 reader 都不得把不同日期或不同
component version 临时拼成一个总览。

当日 bundle 的必需来源或质量门失败时不发布。历史官方状态交付超出可回取范围时，可发布
`APPROVED_WITH_WARNINGS`，但对应状态/额度必须无数值并带明确 quality issue。

### 4. 服务边界

- `service-data-sync` 拥有来源、canonical 存储、质量、publication 和内部 POST 读取 API。
- `service-api` 只通过版本化内部 API 读取，提供认证后的公开 POST-only API；不直连同步库或 Provider。
- `service-web` 只通过共享 POST transport 调用 `service-api`；remote state 进入 TanStack Query，
  分享状态进入 URL。
- data-sync 正式源 capability 与 service-api 公开 API 的运行时开关默认关闭。service-web
  路由和导航始终可部署，但只能显示真实 API 数据或明确 fail-closed 状态；不设置需要重建镜像的
  Vite build-time 开关。真实文件到浏览器 DOM 的端到端对账通过后才开启公开 API。

### 5. 控制面全量入口

`market.stock_connect.overview.bundle` 只接受
`kind=STOCK_CONNECT, operation=MARKET`。`channel=ALL, direction=null` 在同一 fenced run 内展开四通道；
`ALL+NORTHBOUND|SOUTHBOUND` 展开两条同向通道，`SH|SZ+null` 展开该市场双向。控制面不暴露
`ACTIVE_SECURITY` 或 `HOLDING`：活跃榜是 bundle 的必需组件，持仓不属于本模块；这样也避免同批
`datasetCode` 去重规则让 SH 与 SZ 无法一次全量提交。同一 run 负责 fencing、幂等和可审计编排，
但不把全部历史分区包装成一个长事务。

## 被放弃方案

### 继续使用通用 typed market-data query

拒绝。它不能稳定表达共同 bundle、字段级披露状态、通道状态、额度、榜单范围和证券上下文。

### AKShare 或网页抓取作为生产 fallback

拒绝。当前能力为空或字段不完整，来源与再分发边界也不满足本合同。静默切换会污染方法学。

### 各通道独立 latest 后由 API/Web 拼装

拒绝。会把不同交易日和 dataVersion 组合成不存在的“今日总览”。

### 使用汇率把 CNY/HKD 合成一个总额

拒绝。本模块没有版本化汇率 publication；折算会改变原始事实。

## 后果

### 正面

- 每个数值可追溯到官方交付、source publication、SHA-256、解析器和 bundle。
- 制度未披露、来源缺失、休市、身份缺口与真实零值不再混淆。
- 三服务可独立发布代码，同时用 dataVersion 保持消费者一致性。

### 成本

- 需要维护 HKEX/SSE/SZSE entitlement、Secret、egress 和正式交付解析器。
- 全量回补需要先完成 HKEX historical order，并按源许可执行保留策略。
- 新增 canonical migration、专用内部/公开合同和三页面实现。

## 验证

- OpenAPI 只能声明约定的 POST operation，所有引用可机器解析。
- 使用真实获许可文件核对 source → canonical → bundle → internal API → public API → DOM。
- 北向新制度交易日无 buy/sell/net 数值；南向派生净额精确等于同源 buy − sell。
- 四通道 trade date/dataVersion 一致；CNY/HKD 不混加；活跃榜范围文案准确。
- 上游缺失、schema drift、重复文件、迟到修订和身份未解析不会污染上一批准 publication。
