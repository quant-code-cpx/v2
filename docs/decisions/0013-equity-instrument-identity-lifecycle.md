# 0013：A 股证券身份、上市生命周期与跨服务读取边界

- 状态：Proposed
- 日期：2026-07-27
- 关联方案：[A 股证券主数据与上市生命周期技术方案](../service-data-sync/0014-equity-instrument-master/index.html)
- 关联契约：
  [同步服务内部接口](../contracts/0009-data-sync-equity-instrument-internal.openapi.yaml)、
  [service-api 对外接口](../contracts/0010-service-api-equity-instrument.openapi.yaml)
- 继承决策：[ADR-0002](0002-data-sync-ownership-and-access.md)、
  [ADR-0009](0009-equity-data-source-and-serving-boundary.md)

## 背景

当前 P0 `equity_instrument` 同时保存内部 UUID、交易所、代码、当前名称与单值
`listing_status`。日线先到时会按 `(exchange, symbol)` 创建 `PENDING` 占位，后续主数据尚未实现。
该结构能承接行情，却不能回答“某日证券叫什么、当时处于什么上市状态、系统何时知道该事实”，也不能安全表达
名称修订、暂停上市、恢复上市和退市更正。

证券主数据是日线、周/月线、公司行动、财务和板块成份的身份基础。若把一次交易所目录的暂时缺失解释为退市，
会错误关闭证券、隔离合法行情并污染板块历史。若 `service-api` 直接读取同步库，又会绕过发布版本、质量门和迁移边界。

AKShare 1.18.78 只是候选采集库；版本固定与单次探针不保证接口稳定性或字段语义。2026-07-27 的 Docker 探针确认：

- `stock_info_bj_name_code()` 成功返回 330 行，列为证券代码、证券简称、总股本、流通股本、上市日期、
  所属行业、地区、报告日期。
- `stock_info_sh_name_code("主板A股")` 在 50 秒探针预算内未返回。
- `stock_info_sz_name_code("A股列表")` 出现 TLS `UNEXPECTED_EOF_WHILE_READING`。
- `stock_info_sh_delist("全部")` 成功返回 159 行；AKShare 源码把上游 `DELIST_DATE` 重命名为
  “暂停上市日期”。该中文列名与接口“终止上市公司”语义冲突，未经交易所字段验证不得映射为 canonical
  暂停日或退市日。
- `stock_info_sz_delist("暂停上市公司")` 返回空 DataFrame 且无列；
  `stock_info_sz_delist("终止上市公司")` 返回 208 行及证券代码、证券简称、上市日期、终止上市日期。
- `stock_zh_a_stop_em()` 与 `stock_staq_net_stop()` 本次均被远端断开。

这些结果只证明一次接口形状和失败模式。长期稳定性、交易所日历和字段语义仍需用连续技术验证确认。

## 候选方案

1. 保留单表当前值，目录缺失时直接把 `LISTED` 改为 `DELISTED`。
   - 优点：实现简单，查询快。
   - 缺点：无法回放名称和状态；网络故障会被误判为市场事实。
2. 保留现有稳定 `equity_instrument` 锚点，新增标识、名称和状态双时间历史表；
   全量目录与显式生命周期来源分开取证、质量校验和发布。
   - 优点：身份不随名称/状态变化；支持 `asOf` 与 `knownAt`；修订可审计；缺失和退市语义分离。
   - 缺点：增加历史表、排斥约束、迁移和版本化查询成本。
3. 由 `service-api` 复制完整证券主数据读模型。
   - 优点：读取与同步服务运行时解耦。
   - 缺点：需要事件、回放和第二份权威状态；当前消费者与规模不足以证明成本。

## 决策

采用方案 2；方案 1 禁止；方案 3 延后。

### 身份与生命周期

- 现有 `equity_instrument.security_id` 是内部大表关联键，`instrument_id` 是内部永久 UUID。
  当前没有已决跨资产统一身份需求，因此不拆分 `instrument_identity/equity_security`；两个现有标识值均不对公开 API 暴露。
- 公开证券身份使用 `(exchange, symbol)`；名称不是身份。历史代码变更通过有效期标识表解析，
  同一有效时间内一个交易所代码只能对应一只证券。历史代码被新证券复用时关闭旧标识有效期并追加新证券标识；
  永不按相同或相似名称合并，双时间标识历史排斥约束是最终身份唯一性。
- 名称、交易所代码和上市状态采用双时间记录：
  `effective_from/effective_to` 表示市场事实有效期，`known_from/known_to` 表示系统知识有效期，
  `observed_at` 表示来源观测时间。结束时间均为不包含端。
- 发布状态沿用现有 `LISTED`、`SUSPENDED`、`DELISTED`。
  `PENDING` 仅是未确认占位控制状态，不进入已发布目录。
- 本契约的 `SUSPENDED` 只表示交易所明确的“暂停上市”。普通盘中或单日停牌不是该状态，
  不得改变上市生命周期；交易状态应由后续独立数据集表达。当前没有可靠预上市来源，因此不新增
  `PRELISTED`。
- `DELISTED` 只能由明确的交易所退市/终止上市证据触发，并同时通过字段语义、身份、日期和批次完整性质量门。
  上市目录缺失、行情缺失、名称含“退”或发现源缺席均不得触发退市。
- SSE `stock_info_sh_delist` 的 `DELIST_DATE` 在字段语义核实前只保存 raw evidence，并产生待处置质量问题；
  不写 canonical 暂停或退市日期。

### 标识解析与迁移顺序

- 新增日期感知标识解析器，只从 `equity_identifier_version` 读取
  `(exchange, symbol, fact_date, known_at)`；返回 resolved、not_found 或 conflict。
  日线使用 `trade_date`，周/月线使用各自上游事实的 `period_end`，公司行动、财务、板块成员和所有其他写入者
  使用自身事实日期。禁止从 `equity_instrument.exchange/symbol` 直接 `get_or_create`。
- 首次未知行情只能在标识锁内建立 PENDING anchor 和 `identity_state=PENDING` 的占位标识版本；
  重复事实复用该 `security_id`。主数据确认时关闭占位知识版本并以主数据证据追加 CONFIRMED 版本，
  publication 永远过滤 PENDING。若事实日期解析到已明确退市的旧证券且行情晚于退市日，则返回
  `possible_code_reuse` 并隔离；在主数据关闭旧标识、建立新身份前不得误绑或自行推断复用。
- Expand 阶段暂时保留旧 `UNIQUE(exchange,symbol)`，先部署解析器并切换全部消费者和写入者。
  在此窗口发现的代码复用候选必须隔离，不得为绕过旧唯一键而合并证券。
- Contract 阶段只有在历史回填完整、开放标识约束已验证、所有调用点和线上遥测均证明不再依赖当前列后，
  才删除 `equity_instrument` 的绝对 `UNIQUE(exchange,symbol)` 并允许发布代码复用。
  当前 `exchange/symbol/name/listing_status` 列可继续作为展示兼容投影，但不再具有身份权威。
- 当前开放代码的快速唯一性由 `equity_identifier_version` 上
  `effective_to IS NULL AND known_to IS NULL` 的 `(exchange,symbol)` 唯一索引保证；全历史仍由双时间排斥约束保证。
  禁止按 `listing_status` 建 partial unique，因为生命周期状态与标识有效期不能混用。
- 已发布代码复用后，不允许回滚到旧绝对唯一键或旧 current-column 解析器；故障时关闭新写入并 roll forward。

### 发布与部分失败

- 目录 capability 按 `SSE`、`SZSE`、`BSE` 独立抓取、校验和发布。单所失败保留该所上一成功版本，
  不阻止另两所形成各自的新版本。
- 面向全市场的 `equity.master.cn-a` 聚合 publication 只有在三所对同一目标批次均成功、质量通过且
  版本集合冻结后才推进。任一所失败时保持上一稳定聚合版本；首次无稳定版本时，全市场查询返回 503，
  不返回混合新旧的“看似完整”目录。
- 显式退市/暂停来源作为独立 lifecycle batch 进入同一证券分区事务；来源暂时不可用时保持现状，
  不从目录差集补推。
- 相同业务内容的重放不新增历史修订或 `dataVersion`；任何事实更正追加新的知识版本，不覆盖旧证据。
- 每次外部获取都是独立观测并创建新的 `source_batch`，即使 payload hash 相同也不折叠；
  payload hash 只用于 raw object 字节去重和普通查重索引。

### 数据所有权与接口

- `service-data-sync` 独占身份、证券、历史、raw evidence、质量问题和 publication。
  adapter 只通过 provider-neutral port 返回标准候选批次，不能写 canonical PostgreSQL。
- 行情任务可创建 `PENDING` 占位；主数据只能在无身份冲突且有合格证据时原地确认，必须保留现有
  `security_id` 与内部 UUID。PENDING 创建也必须经过日期感知解析/保留流程，不得直接按当前列
  `get_or_create`。板块成份只可发布已确认证券；未解析代码进入隔离区。
- `service-api` 只通过版本化 internal HTTP 读取，不获得同步库、对象存储或供应商凭据，
  不在 Prisma 或 Redis 复制权威证券主数据。
- 公开接口要求现有有效 Bearer Session；是否细分市场数据权限仍由 API 权限评审决定，未决前不得公开匿名读取。
- `docs/contracts/0009-data-sync-equity-instrument-internal.openapi.yaml` 与
  `docs/contracts/0010-service-api-equity-instrument.openapi.yaml` 是证券目录、详情和上市状态历史的后续实施唯一权威。
  它们按重叠路径局部取代 Proposed 契约 0003/0004 的主数据 schema；0003/0004 的行情、复权、
  公司行动、财务等非主数据部分不受影响。被替代的 UUID 主数据路径尚未实现，因此不建立兼容双路由。
- 公开响应和路径使用 `exchange + symbol`，`service-api` 必须剥离 internal `instrumentId`、`securityId`、
  来源批次和质量细节。
- 键式详情和状态历史未传 `asOf` 时，只解析当前知识下唯一的开放标识；没有开放标识返回 404。
  代码已复用时默认返回当前证券，旧证券必须显式传 `asOf`。非法时间输入返回 400；选择切片无匹配返回 404；
  若历史异常地解析出多个身份则返回 409 并阻断发布，不任意选取一只。

### 技术启用条件

- AKShare adapter 版本固定为研究时验证的 1.18.78；升级须重跑接口形状 fixture、失败注入和真实样本探针。
- 本 ADR 覆盖的首批 A 股 equity 链路中，来源、adapter、方法学、`approvalStatus` 与 rights/license 引用保留为
  source batch 和 publication 的审计元数据；它们不得阻断 command、checkpoint、publication 或技术验收。技术启用
  只要求可复现的接口形状、字段语义、调用预算、失败恢复与连续稳定性证据；详见
  [ADR-0028](0028-source-metadata-nonblocking-data-operations.md)。
- 权威交易日历尚未冻结。调度可按 `Asia/Shanghai` 运行，但在日历未通过准入前不能根据“非交易日”
  自动关闭状态、判断缺失或终结批次。

## 后果

- 查询可同时支持市场有效时间和系统知识时间，适用于无未来信息回测和修订审计。
- 每次目录同步需要保存完整 raw、快照元数据、差异、质量结果和独立 exchange publication；
  存储与运维成本高于单表 upsert。
- 全市场目录可能有意保持旧稳定版本，即使部分交易所已有新版本；运维接口和指标必须暴露各分区新鲜度，
  公开响应不能静默混版。
- 现有 `equity_instrument` 继续作为身份锚；通过 expand/backfill 增加标识、名称、状态双时间历史与快照表，
  保持 `security_id`、内部 UUID 和行情外键。现有当前列在兼容窗口内作为事务内维护的投影；
  全部读写切换后删除其绝对 exchange+symbol 唯一键，代码复用后的当前列只作展示兼容。
- `service-api` 增加只读下游依赖和 503 失败面，但其 Prisma schema 与 Redis 业务状态不增加。

## 替代关系

本 ADR 细化 ADR-0009 的 `equity.master` 来源和服务边界，不改变其行情、复权、公司行动或财务决策。
本 ADR 局部取代 Proposed OpenAPI 0003/0004 中证券主数据、详情和上市状态 schema；其余能力继续由原契约管理。
