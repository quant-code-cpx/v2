# 0014：板块 EOD 横截面快照、修订与排行定义

- 状态：Proposed
- 日期：2026-07-27
- 决策者：项目维护者（待评审）
- 关联方案：[板块 EOD 横截面快照与排行技术方案](../service-data-sync/0015-sector-eod-snapshot-ranking/index.html)
- 关联契约：[同步服务内部增量接口](../contracts/0011-data-sync-sector-eod-internal.openapi.yaml)、
  [API 对外增量接口](../contracts/0012-service-api-sector-eod.openapi.yaml)
- 继承决策：[ADR-0002](0002-data-sync-ownership-and-access.md)、
  [ADR-0010](0010-sector-taxonomy-and-derived-data-boundary.md)、
  [ADR-0011](0011-sector-market-data-api-boundary.md)

## 背景

东方财富行业和概念板块的横截面可用于收盘后强弱比较、换手活跃度筛选和板块详情展示。
AKShare 1.18.78 中，`stock_board_industry_name_em()` 与
`stock_board_concept_name_em()` 各用一次批量请求返回当前板块清单及 12 个展示字段：
排名、名称、代码、最新价、涨跌额、涨跌幅、总市值、换手率、上涨家数、下跌家数、
领涨股票名称和领涨股票涨跌幅。

同版本的 `stock_board_industry_spot_em(symbol)` 与
`stock_board_concept_spot_em(symbol)` 每次只返回一个板块的 10 行 `item/value`：
最新、最高、最低、开盘、成交量、成交额、换手率、涨跌额、涨跌幅和振幅。
逐板块调用会形成 N+1 请求，且接口语义是实时行情，不符合“收盘后每个分类体系一次批量观测”的边界。

上述四个函数已从仓库测试镜像内固定的 AKShare 1.18.78 源码核对签名、字段映射和单位转换；
AKShare 官方 1.18.79 文档也给出相同接口角色与返回列。2026-07-27 的真实网络探针在第一个
`name` 请求发生 `RemoteDisconnected`，因此接口形态已验证，但连续可用性与生产准入尚未通过。

更重要的是，两个批量 `name` 响应都没有上游 `trade_date`、行情时间或 `is_final`。
不能因为任务在收盘后运行，就把结果描述成交易所或供应商确认的官方终值。响应内“排名”又是供应商
按当前页面默认字段生成的展示值，无法代表所有请求排序字段的通用排名。

## 约束

- `service-data-sync` 独占 raw evidence、canonical 快照、修订、质量状态和 publication。
- AKShare、东方财富 URL、中文字段与供应商错误只能存在于 adapter。
- adapter 只输出 provider-neutral batch，不能写 canonical PostgreSQL。
- `service-api` 只经版本化内部 HTTP 读取，不直连同步库、S3 或供应商。
- 本能力不是盘中、分时、分钟或实时推送；也不从日、周、月 K 线反推横截面。
- 上游许可、商业使用、保存、访问频率和再分发边界仍待评审。

## 候选方案

1. 收盘后对每个板块调用 `stock_board_*_spot_em`。
   - 优点：字段含开高低、成交量、成交额和振幅。
   - 缺点：N+1、实时接口、单点失败造成部分分区，且成交量单位转换仍需独立验证。
2. 每个 scheme 调用一次 `stock_board_*_name_em`，把整批响应作为一个不可拆分的候选快照。
   - 优点：固定两次批量调用、天然横截面、包含目录身份和主要排行字段。
   - 缺点：没有开高低、成交量、成交额、上游日期或官方最终态。
3. 从已同步日 K 线逐板块取最后一行，拼成横截面。
   - 优点：不新增外部调用。
   - 缺点：会把独立周期行情转换成另一数据集，缺少批量源的总市值、上涨/下跌家数和领涨股语义，
     也违反本能力“直接保存上游横截面”的目标。
4. 保存供应商“排名”并直接用于所有 API 排行。
   - 优点：读路径简单。
   - 缺点：只对应供应商默认排序，null、并列和稳定次序不受本系统控制。
5. 在读取时对一个已发布、不可变快照按请求字段重算排名。
   - 优点：排序语义可版本化；任意分页都能复验；不把供应商展示排名升级为市场事实。
   - 缺点：每次读取执行窗口函数，但单个快照规模小，可由索引与响应上限控制。

## 决策

采用方案 2 和方案 5。方案 1 只保留为受控诊断；方案 3、4 禁止用于 canonical 发布或公开排名。

### 来源与能力

- 新增 provider-neutral 原始能力 `sector.quote.eod.snapshot.raw`，canonical 数据集为
  `sector.quote.eod.snapshot`。
- `eastmoney.industry` 映射 `stock_board_industry_name_em()`；
  `eastmoney.concept` 映射 `stock_board_concept_name_em()`。每次成功任务对每个 scheme 只接受一个
  完整批量响应；失败重试不算日常轮询。
- `stock_board_*_spot_em` 不注册生产同步 capability，不参与补洞或字段拼接。它只能在人工诊断中
  对单个代码核验，结果不得覆盖批量快照。
- 不读取 `sector.bar.1d.raw`、`sector.bar.1w.raw` 或 `sector.bar.1mo.raw` 生成本数据集。
- 供应商“排名”只保存在 raw evidence。canonical 行不保存它，API 不返回它。

### 日期、截点与最终态

- `trade_date` 由已发布的中国 A 股权威交易日历解析。请求目标必须是明确开市日；未知日期或休市日
  不发起生产抓取。不得从 `observed_at` 的自然日猜测交易日。
- 初始 shadow 策略在 `Asia/Shanghai` 16:20 调度，发布截点为目标交易日 16:15。
  截点必须配置化、版本化，并在生产前用连续样本重新批准。`source_cutoff_at` 保存该目标日截点的
  UTC 时间；`observed_at` 保存 adapter 完成 SDK 调用的 UTC 时间。
- 只有 `observed_at >= source_cutoff_at` 的完整候选才可进入质量门。对外
  `finality=post_close_observation` 只表示“在内部收盘后策略截点之后观测”，不表示交易所、东方财富或
  AKShare 官方终态。
- 上游没有行情日期，系统无法仅凭本响应证明内容未滞后。跨日 payload 完全相同、覆盖突降或时间门失败
  必须 quarantine；不得用“任务按时运行”替代数据终态证据。

### 快照、修订与 source batch

- 逻辑分区键为 `scheme + trade_date`。一次 accepted batch 生成一个不可变 snapshot revision 和
  `data_version`；所有板块行必须在同一发布事务中可见。
- 同日或历史日期重新抓取若标准内容 hash 相同：新增一次 source observation 和 raw evidence，
  但不新增 snapshot revision 或 publication。内容变化且质量通过：追加 revision，原子替换该分区
  current publication；旧 revision 永久可审计。
- 当前 `source_batch` 的 `(provider_id, capability, payload_sha256)` 唯一约束会把不同时间的相同内容
  折叠。优先级更高的 `0014-equity-instrument-master` 方案拥有共享 expand migration：移除此唯一约束，
  使每次观测拥有独立 `source_batch_id`；payload hash 改为非唯一查重索引，并增加 `run_id`、
  `partition_key`、`observation_seq` 和 `schema_fingerprint`。本决策只依赖该最终契约，不重复实施或
  回滚共享迁移。
  `UNIQUE(run_id, partition_key, observation_seq)` 保证运行内观测编号唯一；`attempt` 只属于同步分区运行控制，
  不作为 source batch 身份。
- raw evidence 先于 canonical 发布落盘。数据库失败后从 raw 重放，不再次访问上游；数据库 downgrade
  不删除 raw。

### 确定性排行

- 允许排序字段：`changePercent`、`turnoverPercent`、`marketValue`、`latestValue`、
  `advancers`、`decliners`、`leaderChangePercent`、`code`；顺序为 `asc|desc`。
- API 读取时只在一个已发布 `data_version` 上重算。所有数值排序均使用 PostgreSQL `NUMERIC`，
  不转换为 binary float。
- null 无论升降序都排最后，`rank=null`；非 null 相同值使用 competition rank
  （`1, 1, 3`）。唯一 `position` 再以 `sector_code COLLATE "C" ASC`、
  `sector_id ASC` 打破并列。`code` 排序也使用 `C` collation，内部 UUID 只做不可见最终稳定键。
- cursor 绑定 scheme、解析后的 trade date、sort、order 和 `data_version`，保存唯一 position。
  revision 被替换后旧 cursor 返回 409，不允许跨快照续页。

### 字段语义

- `latest_value` 与 `change_value` 保存供应商原生板块数值；`latest_value_unit=provider_native`。
- `market_value` 保存上游“总市值”原值；在币种和缩放未独立确认前，
  `market_value_unit=provider_native`，禁止称为 CNY、加总或跨 scheme 比较绝对规模。
- `change_percent`、`turnover_percent` 和 `leader_change_percent` 明确为百分数，不是比例小数。
- 上游只可靠提供领涨股票展示名称，没有稳定证券代码；canonical 只保存 `leader_name`，
  不按名称或代码规则解析成 `equity_instrument` 外键。
- 快照行保存当次观测的板块名称，历史读取不受 `sector_entity.name` 后续修订影响。

### API 与兼容

- 内部增量契约 0011 新增排行和单板块快照读取；公开增量契约 0012 在
  `/api/v1/market/sectors` 下投影同一发布版本，并剥离 `sectorId`、raw URI、质量明细和 provider 字段。
- 两份契约只扩展 EOD，不重复或修改 0005/0006 已实现的目录与 K 线语义。现有客户端不受影响。
- `asOf` 缺省选择该 scheme 最新已发布交易日；传值时只接受精确交易日，不静默回退。
- ETag 绑定 `data_version` 和请求投影。内部服务 Bearer、公开用户 JWT、Problem Details、429 和
  503 行为继承 ADR-0011；`service-api` 不新增 Prisma 业务表或 Redis 权威缓存。

## 后果

- 每个交易日通常只有两个批量源请求，调用量与部分分区风险显著低于逐板块 spot。
- EOD 快照缺少开高低、成交量、成交额和振幅；消费者需要这些字段时读取独立 K 线，不得把两者伪装成
  同一个上游事实。
- `trade_date` 和 `post_close_observation` 是本系统策略语义，调用方能看到截点与实际观测时间，但不能
  把它解读成官方结算标志。
- 排行查询增加窗口函数成本；单 scheme 单日上限、500 行响应上限和 `(snapshot_id, sector_key)` 索引
  把成本限制在一个小横截面内。
- source batch expand 影响共享血缘表，实施时需先完成 `0014-equity-instrument-master` 拥有的 expand
  migration，再发布新 writer；本能力的回滚不得反向收紧共享表。旧 reader 不依赖已移除的唯一约束，
  可保持兼容。

## 生产前待决

- AKShare/东方财富商业使用、原始数据保存、访问频率和对外再分发许可。
- “最新价”“涨跌额”“总市值”的准确单位、缩放和跨 scheme 可比性。
- 16:15 截点、16:20 调度和异常阈值在连续至少 5 个交易日探针后的生产值。
- raw evidence、历史 revision 和运行记录保留期。

## 替代关系

本 ADR 细化 ADR-0010 的 `sector.quote.eod.snapshot`，不改变其 scheme、三周期独立存储和无分钟数据决策；
扩展 ADR-0011 的版本化 HTTP 边界，不修改已实现目录与 K 线合同。
