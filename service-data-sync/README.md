# service-data-sync

财经与股票基础数据同步服务。当前包含工程基础设施、个股日/周/月独立行情、复权因子、公司行动、公司概况、
行业/概念板块三周期行情、申万三级行业与估值、A 股证券主数据和显式上市生命周期、财务与估值、日频资金流。
各链路经 provider-neutral port、独立 adapter、S3 失败排障证据、PostgreSQL canonical revision、
publication 与统一 command 控制面闭环。

技术方案见 [0001：同步服务工程基础设施](../docs/service-data-sync/0001-data-sync-foundation/index.html)。
板块跨服务读取与 API 路径见
[0003：板块行情 API 访问技术方案](../docs/service-api/0003-sector-market-data-access/index.html)（已实现内部与公开读取路由）。

## 当前边界

### 数据运维控制面

所有新同步、取消、重试、主动健康检查和自动计划都必须通过数据运维 `command` 账本。标准运行路径只有
内部 0022 HTTP 路由提交的 command，以及 `service_data_sync.data_operations.dispatch`、
`service_data_sync.data_operations.reap`、`service_data_sync.data_operations.health_dispatch` 和
`service_data_sync.data_operations.scheduler_tick` 四个 worker 任务；全局 `ExecutionSlot`、lease 与
fencing token 由 PostgreSQL 权威维护。任何 CLI、beat、恢复或重试均不得直接调用同步 use case、发布
canonical 数据或推进 checkpoint。

兼容清单中的 18 个历史 CLI 和 5 个历史 Celery task 已安全拒绝执行，错误码为
`data-operations-legacy-entrypoint-unavailable`，以避免在未注册 fenced executor 的数据集上绕过全局槽。
仍保留的个股行情、参考数据和财务兼容入口只会转换受限业务参数并提交 `SYSTEM` command；它们不会在本进程
访问 Provider、写入 canonical 数据或取得执行槽。

既有内部读取 API 继续只接受 `DATA_SYNC_INTERNAL_API_BEARER_TOKEN`。0022 数据运维读取路由使用
`DATA_SYNC_INTERNAL_READ_API_BEARER_TOKEN`，有副作用的提交、取消、重试、健康检查与计划路由使用
`DATA_SYNC_INTERNAL_OPERATIONS_API_BEARER_TOKEN`。LOCAL/TEST 环境在两枚数据运维 token 都未配置时，才会
回退到既有 token；STAGING/PRODUCTION 必须同时配置既有 token 和读、写两枚数据运维 token，且既有 token
不能访问已拆分的数据运维路由。

### 首批 A 股 equity 来源审计元数据

本轮 A 股目录、生命周期、公司概况、日/周/月线、复权因子与公司行动八条 `equity.*` 链路的
`providerId`、`upstreamSource`、adapter、方法学、`approvalStatus` 与 rights/license 引用会冻结到 source
batch、run、publication 和 API lineage，用于追溯实际来源。仅对这些 equity targets，它们不参与
`preflight`、command 受理、dispatcher、fenced executor、checkpoint、publication 或技术验收；这些路径只依据
请求合同、adapter 可用性、provider 响应、schema、质量、fence、事务、coverage 与版本一致性判定。上游实际认证
协议失败仍是可观察的 provider 获取失败，但静态来源分类不是其替代品。详见
[ADR-0028](../docs/decisions/0028-source-metadata-nonblocking-data-operations.md)。

- **仅失败留证：** 成功 AKShare 同步不再写入 raw 或 normalized S3 对象，只保存 canonical 数据、来源摘要和
  不可回放标记；适配器返回后的字节只在内存中暂存，发生获取后解析、质量或发布失败时才写入私有
  `failures/` 目录及最小 manifest。新成功批次不支持 `--replay-raw`；已有历史归档在清理前仍可回放。
  表中的 `raw_uri`、`normalized_uri`、source manifest 和 lineage 均为引用或摘要元数据，不保存 AKShare 大字段。
  完整取舍见 [ADR-0021](../docs/decisions/0021-failure-only-source-payload-retention.md) 与
  [方案 0030](../docs/service-data-sync/0030-failure-only-source-payload-retention/index.html)。
- 已实现 FastAPI 内部只读路由 `GET /internal/v1/sectors`、`GET /internal/v1/sectors/{scheme}/{sectorCode}`
  和 `GET /internal/v1/sectors/{scheme}/{sectorCode}/bars`，以及证券主数据
  `GET /internal/v1/equities`、`GET /internal/v1/equities/{exchange}/{symbol}` 和
  `GET /internal/v1/equities/{exchange}/{symbol}/listing-status-history`。路由仅接受
  `DATA_SYNC_INTERNAL_API_BEARER_TOKEN`，不暴露 raw、供应商字段、数据库键或 `PENDING` 身份。
- 方案 0011 已实现个股市场扩展迁移：`equity_weekly_bar`、`equity_monthly_bar`、
  `equity_adjustment_factor`、`equity_corporate_action_version`、`equity_profile_version` 和
  `equity_sync_checkpoint`。周/月线各自按年分区并独立发布。
- 已实现 AKShare 腾讯未复权日线 adapter；默认关闭，只有设置 `DATA_SYNC_AKSHARE_ENABLED=true` 才会注册。
- 已实现 AKShare 东财周/月线直采 adapter、新浪累计后复权因子、东财公司行动和巨潮公司概况 adapter。
  周/月分别调用 `stock_zh_a_hist(period="weekly"|"monthly")`，只写各自物理表，不读取日线生成。
  设置 `DATA_SYNC_AKSHARE_ENABLED=true` 与 `DATA_SYNC_EQUITY_MARKET_ENABLED=true` 后注册这些能力；
  `DATA_SYNC_EQUITY_SCHEDULER_ENABLED=true` 再开启六类独立调度。单证券任务按每 worker 30 次/分钟
  限速；adapter 标记的瞬时失败最多重试 3 次，失败不推进 checkpoint。
- 已实现方案 0011 内部读取路由：`GET /internal/v1/equities/{exchange}/{symbol}/bars`、
  `adjustment-factors`、`corporate-actions` 和 `company-profile`。列表使用与 publication 和查询范围绑定的
  HMAC 游标，支持 ETag 304 与 `X-Data-Version`。
- 已实现东财行业/概念板块的日线、周线、月线 adapter；三个周期分别调用上游参数、分别存入
  `sector_daily_bar`、`sector_weekly_bar`、`sector_monthly_bar`，绝不由日线计算。除 `DATA_SYNC_AKSHARE_ENABLED=true`
  外，还必须显式设置 `DATA_SYNC_SECTOR_ENABLED=true`。
- 已实现行业/概念目录 CLI；目录快照的成功路径只保留来源摘要后会激活带名称的 `ACTIVE` 身份。行情先创建的 `PENDING` 身份会保留 UUID 后升级；已实现板块成分当前快照、观测区间、固定 release、双向 internal 读取与手动 CLI。
- 已实现行业/概念板块 EOD 完整横截面 adapter、失败证据、revision、质量门、内部读取、受控手工 CLI、shadow scheduler、租约 reaper、旧 rollback 安全停用、结构化任务日志与 2026 沪深公告交易日历。它只保存 `post_close_observation`，不宣称官方终态；全部 EOD 开关默认关闭。2026 年以外日期安全阻断，且单位对账、连续探针与生产观测验收仍未完成；仓内未选择或引入监控平台。
- 个股行情 CLI 默认拉最近 31 个自然日；传 `--full-history` 从 1990-12-19 回填。`--period 1w` 和
  `--period 1mo` 始终走上游独立周期接口。
- 已实现证券目录、双时间身份/名称/上市状态、交易所独立 publication、全市场稳定聚合、内部读取和公开 API
  代理。目录缺席、空响应和行情缺席不会改变生命周期；显式退市、暂停和恢复只能经专用生命周期 port 发布。
- 已实现固定 AKShare 版本的交易所生命周期 adapter，直接读取沪深北交易所显式上市或退市事实；成功批次保留
  摘要、schema fingerprint、双时间 revision、checkpoint 与 publication，失败批次才保留 raw/normalized evidence。
  所有行情、资金流、财务及板块成分证券写入均按业务日期解析不可变 `security_id`，代码复用冲突返回 409。
- 已实现申万一、二、三级 taxonomy、父级闭包和估值观察：固定版本 adapter、方法学血缘、raw/replay、
  canonical revision、质量门、checkpoint、publication、受控 backfill、CLI/Celery 与三条内部读取路径。
  资金流不属于该链路，完整归属方案 0017。
- 0016 财务与估值已实现 AKShare 东财三表、报告期指标和历史估值 adapter，失败排障证据、canonical
  双时态 revision、独立 publication、平台派生指标、受控单证券 CLI 与无 beat schedule 的 Celery 任务。仅
  `DATA_SYNC_FINANCIAL_SOURCE_POLICY=akshare-eastmoney` 会注册该 adapter；其他策略保持未注册。
- 0016 的四条财务内部读取路径均在存在精确 production publication 时返回冻结双时态视图、HMAC 续页游标、
  ETag 200/304 与 `X-Data-Version`。缺失、已替代或不可读时统一返回
  `financial-publication-unavailable`（503）；任何路径均不暴露 research、raw、quarantine、内部数据库键或
  半成品 revision。
- 已实现方案 0017 的个股、板块、市场日频资金流及供应商排行 adapter、失败排障证据、显式方法学、
  canonical revision、质量门、checkpoint、publication、恢复、CLI/Celery 和五条内部读取路径。
  固定版本在线探针未通过完整性门禁的方法学保持 research，production 读取统一 fail-closed。
- 所有未来外部数据只能通过 provider-neutral port 与独立 adapter 获取。
- application、task、质量、持久化代码禁止直接调用数据源 SDK、HTTP 或具体 adapter。

## 市场概览与行业板块完整包

`market.overview-and-sectors.bundle` 是市场首页、东财行业/概念和申万详情共同依赖的原子 EOD
publication。生产启用前必须同时提供：

- `DATA_SYNC_MARKET_OVERVIEW_ENABLED=true`
- `DATA_SYNC_TUSHARE_ENABLED=true`
- `DATA_SYNC_TUSHARE_TOKEN`
- `DATA_SYNC_MARKET_DATA_LICENSE_SCOPE=commercial-redistribution-approved`
- `DATA_SYNC_MARKET_DATA_LICENSE_REFERENCE`

启动与每次人工提交前的 `market.source.preflight` 会真实探测日历、A 股日行情、上市证券目录、
换手与市值、停牌、涨跌停核验、四个指数、个股与市场资金流、东财目录/行情/成分/资金流和
申万 taxonomy/成分/行情权限及 schema。全量端点恰达供应商行上限时按潜在截断失败，不能用
部分结果推进 bundle 指针；`stock_basic` 的 `L`、`D`、`P` 三个状态在实际同步中分别检查，
preflight 至少在线验证必需的 `L` 分区。

17:20 只表示当天满足 EOD eligibility，不表示所有来源已更新。Tushare 个股 `moneyflow`
在交易日 19:00 后更新，因此数据运维控制面只接受以下唯一计划：`INCREMENTAL`、沪深共同
交易日历、`Asia/Shanghai` 19:20。其他 mode、policy、日历、时区或时间均返回
`market-overview-schedule-invalid`（400），避免把 17:20 误配为完整包抓取时刻。

首次创建计划时，通过 0022 内部运维写路由提交以下模板；`${...}` 必须在调用前替换为本次真实
UUID、内部主体引用和运维凭据。同一数据集只允许一个持久化计划，后续修改必须先读取当前
`scheduleId` 与 `version`，再以 `expectedVersion` 乐观锁更新。

```json
{
  "submissionId": "${SUBMISSION_ID}",
  "scheduleId": null,
  "datasetCode": "market.overview-and-sectors.bundle",
  "mode": "INCREMENTAL",
  "selector": {"kind": "GLOBAL"},
  "targetPolicy": {"policyVersion": 1, "dateResolution": "NONE"},
  "frequency": {
    "kind": "TRADING_DAY",
    "timezone": "Asia/Shanghai",
    "localTime": "19:20",
    "dayOfWeek": null,
    "dayOfMonth": null,
    "intervalMinutes": null,
    "calendarCode": "SSE-SZSE"
  },
  "misfirePolicy": "RUN_ONCE",
  "coalesce": true,
  "enabled": true,
  "expectedVersion": null,
  "actor": {
    "actorRef": "${ACTOR_REF}",
    "role": "SYSTEM",
    "reason": "启用市场概览交易日完整包计划"
  }
}
```

请求使用 `POST /internal/v1/data-operations/schedules/upsert`，携带
`Authorization: Bearer ${DATA_SYNC_INTERNAL_OPERATIONS_API_BEARER_TOKEN}`、
每次操作唯一且网络重试时稳定复用的 `Idempotency-Key` 和 `X-Request-Id`。scheduler tick 只把
19:20 fire 写入同一 command 队列，dispatcher 取得全局 slot 与 fencing token 后才执行同步；
不存在绕过 command 账本的 beat 直写。

同一运行内仅对可重试网络失败最多做三次有界 adapter 重试，不存在 19:35、19:50 或每 15 分钟的
自动补跑。交易日 20:00 前必须核对：

1. 对应 command/run 已到 `SUCCEEDED`，且没有 `PARTIAL`、`FAILED` 或死信投递；
2. `GET /internal/v1/market/overview-bundles/latest` 的 `tradeDate` 等于当日共同交易日；
3. `status.freshness=current`、`lagTradingDays=0`，并存在强 `ETag` 与相同
   `X-Data-Version`。

20:00 仍未满足时立即告警，并对失败 command 或 run 调用
`POST /internal/v1/data-operations/commands/retry`。重试请求必须引用真实失败资源，不重新猜日期：

```json
{
  "submissionId": "${RETRY_SUBMISSION_ID}",
  "target": {
    "resourceType": "RUN",
    "resourceId": "${FAILED_RUN_ID}"
  },
  "actor": {
    "actorRef": "${ACTOR_REF}",
    "role": "SYSTEM",
    "reason": "市场概览完整包在 20:00 前未成功，按失败运行重试"
  }
}
```

该请求同样使用运维写 bearer、独立 `Idempotency-Key` 与 `X-Request-Id`。失败不会推进 active
bundle；旧完整版本继续可读并明确标为 stale。次日正常 `INCREMENTAL` 会检查最近 25 个共同
交易日并升序补洞；若发现 current pointer 之后已有发布而中间仍有历史 active 缺口，则安全停止并
要求受控 chain replay，不会把历史修补伪装成当天成功。

## 数据模型导览

同步服务当前数据库定义以
[`models/registry.py`](src/service_data_sync/infrastructure/database/models/registry.py) 为唯一入口：它显式登记全部
已登记逻辑表。每张表各有一个模型文件，字段、PostgreSQL 类型、可空性、中文含义、主外键、约束和索引都写在
该文件中；模型按 `execution`、`provenance`、`publication`、`equity`、`sector/catalog`、`sector/market_data`、
`sector/membership` 和 `sector/eod` 分组。物理分区不是独立业务表，规则见
[`partition_manager.py`](src/service_data_sync/infrastructure/database/partition_manager.py)。

完整取舍、边界和验证结果见
[0018：SQLAlchemy ORM 全量迁移](../docs/service-data-sync/0018-sqlalchemy-orm-persistence-models/index.html)。

## 测试归属

`service-data-sync` 作为独立 Python 功能模块，测试与生产包隔离在服务级 `tests/`：

```text
service-data-sync/
├── src/service_data_sync/
└── tests/
    ├── unit/
    ├── integration/
    └── architecture/
```

禁止在 `src/service_data_sync/` 生产源码旁平铺 `test_*.py`。新增细分功能域时，在对应测试分类下建立
同名功能子目录；只有跨功能模块的集成与架构测试保留在服务级分类根部。

## 个股日/周/月、复权与公司资料同步

这批链路只允许通过 Data Operations 控制面运行。不要直接调用同步 use case、仓储或旧 CLI；
`data-sync-equity-bars` 和 `data-sync-equity-reference` 仅保留为提交 `SYSTEM` command 的兼容入口，
目录和生命周期旧 CLI 已明确拒绝执行。正式验收统一使用内部 HTTP 路由、dispatcher 与 fenced executor。

先启动基础设施、内部 API 和 worker，并在同一受控环境显式开启来源：

~~~sh
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env \
  --profile data-sync-infra --profile data-sync-api --profile data-sync-worker up -d
~~~

按以下顺序提交；内部 bearer 由受控环境注入，调用、日志和验收记录不得打印其值。

1. 使用 read bearer 调用 `POST /internal/v1/data-operations/commands/preflight`，请求体为
   `{"targets":[target]}`。
2. 使用 operations bearer 调用 `POST /internal/v1/data-operations/commands/submit`，携带稳定的
   `Idempotency-Key`；请求体必须原样带回 `preflightId`、`requestHash`、`targets`，并提供
   `submissionId` 与 `actor`。
3. worker 的 `service_data_sync.data_operations.dispatch` 取得 PostgreSQL execution slot 和 fencing
   token 后才会执行。通过 `POST /internal/v1/data-operations/commands/detail` 和
   `POST /internal/v1/data-operations/runs/detail` 轮询到 `SUCCEEDED`；不能以 command 已受理代替发布成功。

目录必须先运行，再从已发布目录读取真实样本，不能把示例代码当作仍有效证券：

~~~json
{
  "datasetCode": "equity.master.cn-a",
  "mode": "FULL",
  "selector": {"kind": "GLOBAL"},
  "dateFrom": null,
  "dateTo": null,
  "observationDate": null
}
~~~

成功后用 `GET /internal/v1/equities?exchange=SSE&status=LISTED&limit=1` 取得当前发布的 `symbol`，
再以该值提交以下目标：

- 生命周期：`equity.lifecycle.explicit`、`FULL`、`{"kind":"GLOBAL"}`，三个日期字段均为 `null`；
- 日、周、月线：`equity.bar.1d.raw`、`equity.bar.1w.raw`、`equity.bar.1mo.raw`，`DATE_RANGE`，
  `{"kind":"INSTRUMENT","exchange":"SSE","symbol":"<目录返回值>"}`；
- 复权因子与公司行动：`equity.adjustment_factor`、`equity.corporate_action`，使用同一受控
  `INSTRUMENT` selector 和包含端 `DATE_RANGE`；
- 公司概况：`equity.profile`、`INCREMENTAL`、同一 `INSTRUMENT` selector，三个日期字段均为 `null`。

每个 target 都要连续提交两次。第二次必须证明业务行没有重复、checkpoint 没有错误推进、
`dataset_publication` 的 `dataVersion` 稳定；使用 `POST /internal/v1/data-operations/datasets/detail`
读取 `latestPublication`，并通过证券、行情、因子、公司行动和概况内部读取路由核对 ETag、
`X-Data-Version`、日期、来源批次和质量状态。内部机器契约见
[`0018-data-sync-equity-market-data-internal.openapi.yaml`](../docs/contracts/0018-data-sync-equity-market-data-internal.openapi.yaml)；
service-api 公开 POST 契约见
[`0019-service-api-equity-market-data.openapi.yaml`](../docs/contracts/0019-service-api-equity-market-data.openapi.yaml)。

## P0 板块三周期同步

板块 CLI 不提供默认日期，必须显式给出有界窗口。`--period` 为 `1d`、`1w` 或 `1mo`，它们映射到三个
独立 upstream 请求和 canonical 表；不能传入分钟周期，也不会读取日线来计算周/月线。

~~~sh
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env.example \
  --profile data-sync-infra up -d
docker build --target test --tag quant-v2/service-data-sync:test service-data-sync
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env.example \
  --profile data-sync-infra --profile data-sync-test run --rm data-sync-test \
  /bin/sh -ec 'alembic upgrade head && DATA_SYNC_AKSHARE_ENABLED=true \
  DATA_SYNC_SECTOR_ENABLED=true data-sync-sector-bars \
  --scheme eastmoney.industry --sector BK0475 --period 1w \
  --start 2026-06-01 --end 2026-06-30'
~~~

环境开关默认关闭；在目标环境完成单位、schema、频率与连续稳定性验证后按 capability 开启。

## 板块 EOD 横截面同步

EOD 只接受显式的 `scheme` 和 `trade-date`，一次调用对应分类体系的批量 name 接口；它不会用逐板块
spot、K 线或其他来源补齐缺失行。运行前目录必须已经发布为完整 `ACTIVE` 集合，候选覆盖率必须为 100%；
同内容重跑只新增来源观测，不创建新的 canonical revision 或 `dataVersion`。

~~~sh
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env.example \
  --profile data-sync-infra up -d
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env.example \
  --profile data-sync-infra --profile data-sync-test run --rm data-sync-test \
  /bin/sh -ec 'alembic upgrade head && DATA_SYNC_AKSHARE_ENABLED=true \
  DATA_SYNC_SECTOR_ENABLED=true DATA_SYNC_SECTOR_EOD_ENABLED=true \
  data-sync-sector-eod --scheme eastmoney.industry --trade-date 2026-07-27'
~~~

该命令默认只写 `candidate`。2026 年可在显式开启
`DATA_SYNC_TRADING_CALENDAR_ENABLED=true` 后使用沪深公告日历；其他年份、单位与连续稳定性尚未完成技术验证，
缺少或未知日历时命令会在访问 provider 前停止。只有 `DATA_SYNC_SECTOR_EOD_PUBLISH_ENABLED=true` 且显式传入
`--publish` 才会推进 `dataset_publication`，生产开关必须保持关闭。

发生归一化或质量规则变更时，只有清理前已有历史 raw archive 的分区可用 `--replay-raw` 重放；新成功批次不再
保留 raw，因此应重新抓取来源或创建显式回填方案。该模式不会调用上游，且仅在已有可读取来源观测后可用。

阻断质量失败会保存完整 `quarantined` snapshot、报价和规则证据，但不会替换已有 `published` version 或创建
`dataset_publication`；质量证据固定记录 `sector-eod-shadow-v1` policy；同一 scheme 的最近已发布快照用于市值稳定性
和全批 stale 检测。

运维可单独运行 `data-sync-sector-eod-reaper` 回收过期租约；它只将 checkpoint 置回 `queued`，不会访问 provider、
推断交易日或启用 EOD source policy。`DATA_SYNC_SECTOR_EOD_SCHEDULER_ENABLED=true` 时，独立
`data-sync-scheduler` 会在上海时间 16:20 投递两个 scheme，并每 5 分钟回收后重投 queued 分区；它仍会因未知日历停止。

若需恢复指定已通过 revision，可在受控变更窗口执行：

~~~sh
DATA_SYNC_SECTOR_EOD_PUBLISH_ENABLED=true data-sync-sector-eod-rollback \
  --scheme eastmoney.industry --trade-date 2026-07-27 --revision 1
~~~

rollback 只把 consumer publication 指回既有 `passed`/`warned` history，不删除 raw、candidate、quarantine 或较新 revision。

## P0 板块目录与内部读取

先同步分类目录，才会有可由内部 API 读取的 `ACTIVE` 板块；目录同步不包含成份关系：

~~~sh
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env.example \
  --profile data-sync-infra up -d
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env.example \
  --profile data-sync-infra --profile data-sync-test run --rm data-sync-test \
  /bin/sh -ec 'alembic upgrade head && DATA_SYNC_AKSHARE_ENABLED=true \
  DATA_SYNC_SECTOR_ENABLED=true data-sync-sector-catalog --scheme eastmoney.industry'
~~~

内部 HTTP 服务只在 Compose 内网监听 `8000`。`service-api` 通过 `data-sync-api` 服务名访问它；两侧必须使用同一
`DATA_SYNC_INTERNAL_API_BEARER_TOKEN`，生产环境由 secret 注入并进行轮换。

## 板块成分观测历史

成分 CLI 只接受单一、已显式开启的来源；默认 `DATA_SYNC_SECTOR_MEMBERSHIP_ENABLED=false`。完整快照才会更新
observed 区间；PENDING、quarantine、空响应、
结构异常或质量阻断不会关闭任何关系。公开读取只经固定 release，`observedFrom`/`observedTo` 不是实际调入或调出日期。

~~~sh
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env.example \
  --profile data-sync-infra --profile data-sync-test run --rm data-sync-test \
  /bin/sh -ec 'alembic upgrade head && DATA_SYNC_AKSHARE_ENABLED=true \
  DATA_SYNC_SECTOR_ENABLED=true DATA_SYNC_SECTOR_MEMBERSHIP_ENABLED=true \
  data-sync-sector-membership --scheme eastmoney.industry --observation-date 2026-07-27'
~~~

同一 `scheme/sector/observation_date` 重跑复用逻辑 snapshot，但会保留独立 source evidence；任务 run、分区 lease、
checkpoint、失败码与最终 `succeeded`/`partial`/`failed` 状态均写入 PostgreSQL。当前来源只支持当天 Shanghai 市场日，
历史修复必须从已归档 raw 重放，不能向上游伪造历史请求。

## A 股证券主数据与上市生命周期

证券目录和上市生命周期同样只能走上节的 Data Operations `preflight → command → dispatcher → fenced
executor` 路径。`data-sync-equity-catalog` 与 `data-sync-equity-lifecycle` 是已停用入口，会返回
`data-operations-legacy-entrypoint-unavailable`，不能用于同步、重放、checkpoint 或 publication 验收。

东财目录只描述当前观察到的证券集合，不提供退市、暂停或恢复证据；目录缺席绝不能改写生命周期。
生命周期 target 使用 `equity.lifecycle.explicit/FULL/GLOBAL`，由固定版本交易所显式事实 adapter 分别
覆盖 SSE、SZSE、BSE。退市后状态反转只能使用 `OFFICIAL_CORRECTION` 并携带可追溯来源证据引用；
代码复用候选会被隔离，不能绑定到已退市证券。

## 前置条件

- Docker Engine 与 Docker Compose v2。
- 本服务的构建、启动、检查、测试均在容器内执行；宿主机不需要安装 Python 或 uv。

## 本地启动

在仓库根目录：

~~~sh
cp .env.example .env
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env --profile data-sync-infra up -d
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env --profile data-sync-infra --profile data-sync-worker run --rm --no-deps data-sync-worker data-sync-diagnostics --format console
~~~

常驻 `worker` 与 `scheduler` 的 Docker `healthcheck` 仅检查 PID 1 存活（`kill -0 1`），不会周期性发起
PostgreSQL、Redis 或 S3 探测。基础设施连通性应在启动前以单次 `data-sync-diagnostics` 确认；实际同步仍必须
经 Data Operations `preflight → command → dispatcher → fenced executor`，liveness 不能替代该控制面。

诊断成功后启动真实 worker 与 scheduler；scheduler 会按固定 beat 唤醒 dispatcher，worker 取得全局
execution slot 与 fencing token 后执行已提交或已到期的同步 run：

~~~sh
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env --profile data-sync-infra --profile data-sync-worker up -d
~~~

停止本地依赖：

~~~sh
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env --profile '*' down
~~~

该命令保留 named volume。不要将 down -v 加入常规脚本。

## 开发命令

构建测试镜像后在容器内执行：

~~~sh
docker build --target test --tag quant-v2/service-data-sync:test service-data-sync
docker run --rm --read-only --tmpfs /tmp quant-v2/service-data-sync:test ruff format --check .
docker run --rm --read-only --tmpfs /tmp quant-v2/service-data-sync:test ruff check .
docker run --rm quant-v2/service-data-sync:test pyright
docker run --rm --read-only --tmpfs /tmp quant-v2/service-data-sync:test pytest -m "not integration"
docker run --rm --read-only --tmpfs /tmp quant-v2/service-data-sync:test pytest tests/architecture --no-cov
docker run --rm --read-only --tmpfs /tmp quant-v2/service-data-sync:test pip-audit
~~~

启动本地基础设施后执行集成检查：

~~~sh
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env --profile data-sync-infra up -d
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env --profile data-sync-infra --profile data-sync-test run --rm data-sync-test
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env --profile data-sync-infra --profile data-sync-worker run --rm --no-deps data-sync-worker alembic current
~~~

## 诊断退出码

- 0：全部通过。
- 1：未知内部错误。
- 2：配置无效。
- 3：PostgreSQL 不可用。
- 4：Redis 不可用。
- 5：S3/MinIO 不可用。
- 6：多个依赖不可用。

诊断只执行连通性检查，不创建表、不写入业务数据、不发起外部数据源请求。

## 配置与秘密

根目录 [.env.example](../.env.example) 是可提交的假值模板。复制为不提交的 `.env` 后再启动；
不得把模板值用于 staging/production，也不得提交真实账号、token、行情数据、数据库文件或 Docker volume。

容器内使用 Compose service DNS；宿主机诊断使用 .env 中的 127.0.0.1 endpoint。
生产环境只接受 `DATA_SYNC_IMAGE_REF` immutable digest，PostgreSQL、Redis 和 MinIO 不发布宿主机端口。

## 财务与估值 dark launch

`DATA_SYNC_FINANCIAL_ENABLED=false` 与 `DATA_SYNC_FINANCIAL_SOURCE_POLICY=disabled` 是默认状态。启用东财财务
同步时，设置 `DATA_SYNC_AKSHARE_ENABLED=true`、`DATA_SYNC_FINANCIAL_ENABLED=true`、
`DATA_SYNC_FINANCIAL_SOURCE_POLICY=akshare-eastmoney`，并同时提供
`DATA_SYNC_FINANCIAL_MAX_CONCURRENCY`、`DATA_SYNC_FINANCIAL_REQUESTS_PER_MINUTE` 与
`DATA_SYNC_FINANCIAL_REQUEST_TIMEOUT_SECONDS`；缺少任一项时服务拒绝启动。

worker 注册 `service_data_sync.financial.probe` 和受控的
`service_data_sync.financial.sync_security(exchange, symbol)`，没有 beat schedule。手工同步使用：

```bash
data-sync-financial --exchange SSE --symbol 600519
```

同步只在失败时归档三表、指标和估值的排障证据；三项标准载荷均成功后才分别推进各自的 canonical 与
publication。平台派生指标使用独立输入 publication 和公式版本，可执行：

```bash
data-sync-financial-derived --exchange SSE --symbol 600519
```

## 指数来源影子探测

`DATA_SYNC_INDEX_ENABLED=false` 与 `DATA_SYNC_INDEX_SOURCE_POLICY=disabled` 默认关闭。仅在明确启用
`DATA_SYNC_AKSHARE_ENABLED=true` 后，才可将来源策略设为 `akshare-csindex`、`akshare-cnindex` 或
`akshare-csindex-cnindex`；中证与国证 adapter 不会互相兜底。

当前实现只注册目录、成分和权重的来源观察能力。中证当前成分没有历史日期参数，国证详情没有可靠交易所
字段，均不会被提升为 PIT 有效事实。可受控运行单管理人、单能力、单指数（目录除外）的影子 CLI；它仅归档
raw/标准载荷并写入 research 观察、质量和血缘，不创建 publication、PIT 或业务读取接口：

```bash
DATA_SYNC_AKSHARE_ENABLED=true DATA_SYNC_INDEX_ENABLED=true \
DATA_SYNC_INDEX_SOURCE_POLICY=akshare-csindex \
data-sync-index-shadow --administrator CSI --capability index.weight.snapshot --index-code 000300
```

## 衍生品真实合约日线

衍生品 P0 默认尝试个人内部研究用 AKShare 来源。找不到唯一 adapter 时，命令记录
`source_unavailable` 并成功返回空结果；有 adapter 时仍要求合约目录和来源批准完整，才会访问网络或创建
publication。可用参数覆盖默认来源元数据：

```bash
data-sync-derivative-bars --contract CFFEX.IF2608 --start 2026-07-28 --end 2026-07-28
```

连续探测、正式质量门、定时任务、历史回放与受控消费仍须完成方案 0019 的后续门禁后另行启用。

## ETF、两融、公告与交易公开信息 P0

这些 P0 能力使用个人内部研究 AKShare 元数据和禁止再分发范围。设置
`DATA_SYNC_AKSHARE_ENABLED=true` 后，组合根注册 `provider_id=akshare` 的研究 adapter，ETF、两融、公告、
龙虎榜、大宗交易和真实合约日线仍按独立 capability 发布；一个任务不会用另一个 dataset 补齐字段。
沪深港通不属于这条 AKShare 链路，任何同名 capability 都不能替换已批准的官方来源。

AKShare 没有上交所两融标的名单接口，上交所资格 capability 返回合法空数组。ETF profile 实际读取
上交所 `commonQuery` 的 `F100` 当前 ETF 目录与官方类别树，以及深交所官方基金 XLSX 中
`基金类别=ETF` 的行；`akshare` 只是内部 adapter/权利审批标签，公开来源仍分别标识交易所。两端都只有
当前快照：跨日旧 fire 会在任何 Provider 请求前不可重试失败并留脱敏证据，目录空响应按来源不可用处理，
均不会解释为零产品或退市。上述研究能力的空值不会被解释为沪深港通官方数据。

```bash
data-sync-margin --operation market --venue SSE --start 2026-07-28 --end 2026-07-28
```

`data-sync-etf` 旧入口已故意拒绝执行，ETF 只能通过带 fencing 的 Data Operations 链路运行：

1. 启动 `data-sync-worker` 与 `data-sync-scheduler`；scheduler 每十秒唤醒 dispatcher，二者必须使用同一
   immutable service-data-sync image。
2. 对 `fund.etf.profile.reported` 以 `OBSERVATION_DATE` 和
   `{"kind":"ETF","operation":"MASTER","scope":"ALL_VENUES","venue":null,"etf":null}`
   调用 `POST /internal/v1/data-operations/commands/preflight`，再使用新的幂等键调用
   `POST /internal/v1/data-operations/commands/submit`。只有 SSE、SZSE 两个 publication 都成功后才能继续。
3. 对日线、NAV、状态分别选择 `BARS`、`NAV`、`STATUS` 之一；以下三个 selector 都可直接复制，一次预检只提交
   其中一个：

   ```json
   {"kind":"ETF","operation":"BARS","venue":null,"scope":"ALL_ETFS","etf":null,"profileDataVersions":null}
   {"kind":"ETF","operation":"NAV","venue":null,"scope":"ALL_ETFS","etf":null,"profileDataVersions":null}
   {"kind":"ETF","operation":"STATUS","venue":null,"scope":"ALL_ETFS","etf":null,"profileDataVersions":null}
   ```

   服务端会冻结两市 exact profile dataVersion、全集数量和摘要，submit 不接收调用方伪造版本。官方
   profile 类型为 `交易型货币基金` 或 `货币市场基金` 的成员会以
   `NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET` 记录 `SKIPPED` 和审计摘要，不请求 NAV Provider；若运行时
   仍观察到混合 `NAVTYPE`，同样返回 `CURRENTLY_UNSUPPORTED`，绝不把万份收益或七日年化冒充单位净值。
4. 自动计划先配置双市场 profile current-snapshot 计划，再配置下游 ALL_ETFS 计划。全集逐实体请求使用
   `DATA_SYNC_ETF_PROVIDER_MIN_INTERVAL_SECONDS` 节流；可重试熔断按
   `DATA_SYNC_ETF_AUTO_RETRY_BASE_SECONDS`/`MAX_SECONDS` 有界退避，并在
   `DATA_SYNC_ETF_AUTO_RETRY_MAX_ATTEMPTS` 内续跑同一冻结 run、继承成功分区。超过上限才进入终态失败。

`data-sync-corporate-events`、`data-sync-trading-events` 也可无来源参数运行；前者只接收
`--start/--end`，后者另需 `--operation dragon-tiger|block-trade`。这些命令默认仅供受控手工运行，未配置自动调度。

## 沪深港通官方完整包

`market.stock_connect.overview.bundle` 只接受 `provider_id=official-stock-connect`，原子串联 HKEX 官方日历、
Data Marketplace licensed 日统计与活跃证券、摘要钉住的 fixed-length Securities Master，以及 OMD-C、
SSE MDGW、SZSE STEP 日终状态 landing。缺 Daily Statistics 授权、运营边界内缺状态 sidecar、缺
`END_OF_DAY_FINAL`、已声明 profile 的 schema 漂移或部分通道失败均阻断对应 publication；单日
Securities Master 缺件或单证券无法映射只把该行标为 `SOURCE_UNRESOLVED`，保留来源代码和来源名称，
不生成 `instrumentEntityRef`，不会阻断真实成交与来源活跃榜。系统不会回退 AKShare、制造空榜或从成交额
推导净买入。OMD-C Msg80 不报告会话，北向 `CLOSED` 仅在官方开放日与最终性证据同时存在时标为
`DERIVED`，买卖委托接受标志保持空。

内部 Stock Connect API 另提供
`POST /internal/v1/stock-connect/readiness/query`。它只读取由官方日历、entitlement/delivery、
preflight、execution 和 publication 持久化事件形成的独立快照；候选日与全部所选通道就绪日分别返回。
正文删除顶层 `dataVersion` 后按合同规范序列化并计算 SHA-256，响应头必须一致；无快照返回
`READINESS_NOT_OBSERVED`，查询时间不参与状态或版本。

上线前应先在只读容器内离线校验四类清单；该命令不访问网络或数据库，成功返回 `0`，schema、摘要、
覆盖边界或分页 root 不合法返回 `2`：

```bash
data-sync-stock-connect-manifests validate-all \
  --calendar /run/quant-v2/stock-connect-config/hkex-calendar-manifest.json \
  --sftp /run/quant-v2/stock-connect-config/hkex-sftp-delivery-manifest.json \
  --status /run/quant-v2/stock-connect-config/stock-connect-status-manifest.json \
  --status-required-from 2020-01-01 \
  --master /run/quant-v2/stock-connect-config/hkex-securities-master-fixed-length-profile.json \
  --master-sha256 "${DATA_SYNC_HKEX_SECURITIES_MASTER_PROFILE_MANIFEST_SHA256}"
```

分页 SFTP 清单变更评审时可单独重算 canonical root：

```bash
data-sync-stock-connect-manifests calculate-sftp-root \
  --path /run/quant-v2/stock-connect-config/hkex-sftp-delivery-manifest.json
```

正式 preflight 持久化 immutable delivery manifest 后，使用其真实 UUID 与 root hash 做六阶段全量覆盖审计：

```bash
data-sync-stock-connect-coverage-audit \
  --manifest-id "${STOCK_CONNECT_DELIVERY_MANIFEST_ID}" \
  --root-hash "${STOCK_CONNECT_DELIVERY_ROOT_HASH}"
```

审计只读取 PostgreSQL，依次核对 entitlement、对象版本、状态、市场统计、来源活跃榜和原子 bundle；全部
通过返回 `0`，存在 coverage 缺口返回 `3`，输入或依赖失败返回 `2`。生产状态边界只读取已持久化
`stock_connect_status_coverage_boundary_lock`，不会采信临时环境变量。

回滚只接受精确通道、交易日和已经存在的历史完整包，不提供隐式 latest。首次执行前生成一个永久
operation UUID；命令输出丢失或进程恢复时必须复用同一 UUID 和完全相同的审计参数：

```bash
data-sync-stock-connect-rollback \
  --operation-id "${STOCK_CONNECT_ROLLBACK_OPERATION_ID}" \
  --channel SH_NORTHBOUND \
  --trade-date 2026-07-29 \
  --target-bundle-release-id "${STOCK_CONNECT_TARGET_BUNDLE_RELEASE_ID}" \
  --actor-ref "operator:stock-connect" \
  --reason "生产完整包出现数据质量回归，回滚到已验证历史版本" \
  --request-id "incident:stock-connect:20260729"
```

该入口使用权威 command/run、全局 execution slot 和单调 fencing token；bundle 指针、全部受影响 overview
子集、不可变回滚审计及成功终态在同一事务提交。旧 token、跨日/跨通道目标、残缺 bundle、残缺 overview
图或并发占槽全部 fail-closed。成功或同 operation 重放返回 `0`，业务拒绝返回 `3`，输入或依赖失败返回
`2`；换 operation UUID 不能把已回滚目标伪装成同一次幂等重放。

启用前至少配置：

- `DATA_SYNC_STOCK_CONNECT_ENABLED=true`
- `DATA_SYNC_STOCK_CONNECT_LICENSE_SCOPE`
- `DATA_SYNC_HKEX_SFTP_USERNAME`
- `DATA_SYNC_HKEX_SH_DAILY_PATH_TEMPLATE`、`DATA_SYNC_HKEX_SZ_DAILY_PATH_TEMPLATE`
- `DATA_SYNC_HKEX_SECURITIES_MASTER_PATH_TEMPLATE`，必须使用 `{issued_date}` 或 `{issued_iso_date}`
- `DATA_SYNC_HKEX_SECURITIES_MASTER_PROFILE_MANIFEST_SHA256`
- `DATA_SYNC_HKEX_CALENDAR_MANIFEST_PATH`
- `DATA_SYNC_HKEX_SFTP_DELIVERY_MANIFEST_PATH`
- `DATA_SYNC_STOCK_CONNECT_STATUS_MANIFEST_PATH`
- `DATA_SYNC_STOCK_CONNECT_STATUS_REQUIRED_FROM`
- `DATA_SYNC_STOCK_CONNECT_CURSOR_HMAC_SECRET`，至少 32 字节

默认连接 `sftp.data.hkex.com.hk:22`，可用 `DATA_SYNC_HKEX_SFTP_HOST` 和
`DATA_SYNC_HKEX_SFTP_PORT` 覆盖；公开日历只允许
`DATA_SYNC_HKEX_CALENDAR_URL_TEMPLATE` 指定的 HTTPS 地址。容器必须只读挂载：

- OpenSSH 私钥到 Compose 固定的
  `/run/quant-v2/stock-connect-config/hkex-sftp-private-key`
- 严格 known-hosts 到 Compose 固定的
  `/run/quant-v2/stock-connect-config/known_hosts`
- licensed fixed-length profile manifest 到 Compose 固定的
  `/run/quant-v2/stock-connect-config/hkex-securities-master-fixed-length-profile.json`
- 逐年度官方日历摘要清单到
  `/run/quant-v2/stock-connect-config/hkex-calendar-manifest.json`
- 分页 SFTP 历史订单/保留期清单到
  `/run/quant-v2/stock-connect-config/hkex-sftp-delivery-manifest.json`
- 状态覆盖清单到
  `/run/quant-v2/stock-connect-config/stock-connect-status-manifest.json`
- OMD-C、SSE MDGW、SZSE STEP 最终文件及同名 `.manifest.json` 到
  `/var/lib/quant-v2/stock-connect-status`

Compose 将 `data_sync_stock_connect_config` 与 `data_sync_stock_connect_status` 两个 named volume
分别只读挂载到上述配置根和状态根；生产部署必须在启动 worker/scheduler 前由受控交付流程填充它们，
不能依赖镜像内默认文件。

状态相对路径可分别由 `DATA_SYNC_HKEX_OMDC_STATUS_PATH_TEMPLATE`、
`DATA_SYNC_SSE_MDGW_STATUS_PATH_TEMPLATE` 和 `DATA_SYNC_SZSE_STEP_STATUS_PATH_TEMPLATE` 配置。
OMD-C 模板必须含 `{channel}`，默认值为
`hkex-omdc/{channel}/{year}/{trade_date}.bin`，确保沪、深北向文件及 sidecar 不会互相覆盖。每次人工
preflight 都会在总 deadline 内枚举请求窗口的全部官方开放日。日历必须逐年出现在摘要清单中；历史本地
官方归档使用 `sourceKind=LOCAL_ARCHIVE`，历史在线文件使用带精确 `url` 的
`sourceKind=HTTPS_OBJECT`；`HTTPS_TEMPLATE` 只允许清单中的当前公开年份，不能为 2014 等历史年份猜
`{year}` URL。所有对象都先校验 SHA-256，缺中间年份、404 或摘要漂移不会改用第三方日历。Securities
Master profile manifest 必须是互不重叠的生效区间集合，缺布局区间时拒绝解析，不能用当前列宽猜历史文件。
SH Daily Statistics 全量边界为官方 back issues 覆盖的 `2014-11-17`，SZ 边界为 `2016-12-05`；
DATE_RANGE 早于对应边界时直接拒绝，不把连接开通日、抓取时间或当前订阅日冒充产品交付边界。
Daily Statistics 在 2024-08-19 北向披露变更前后使用不同制度 profile：变更前买卖额缺失是 schema
错误，变更后保持空值和 `NOT_DISCLOSED_BY_REGIME`，绝不写零。

SFTP 每个 Daily Statistics 对象必须出现在分页 entitlement 清单，逐对象携带 `orderReference` 与
`availableUntil`；Securities Master 清单项存在时同样逐对象校验，缺项或单日文件不可用时仅进入身份降级。
preflight 会用
`DATA_SYNC_STOCK_CONNECT_MIN_PARTITIONS_PER_MINUTE`（默认每分钟 20 个日包）和
`DATA_SYNC_STOCK_CONNECT_DELIVERY_EXPIRY_SAFETY_SECONDS`（默认 3600 秒）估算安全完成时刻；无法在最早
保留截止前完成时 `eligible=false`、零 command/run。目录批量核验后，每个产品组首尾对象另做精确 stat。

状态只校验 coverage manifest 明确声明的 landing；`DATA_SYNC_STOCK_CONNECT_STATUS_REQUIRED_FROM`
及之后的官方开放日必须逐日声明、逐一解析并校验 sidecar 摘要与 `END_OF_DAY_FINAL`，缺件立即阻断。该运营
边界之前没有参与者历史归档的日期仍可发布真实成交事实，但状态固定为
`sessionAvailability=SOURCE_MISSING`、`quotaState=SOURCE_MISSING`、无 publication，并带
`STATUS_SOURCE_NOT_AVAILABLE_HISTORICAL` 警告，不制造额度或会话值。

通过后，控制面把全窗证据拆成每页最多 20 个交易日、256 个目标的 PostgreSQL 不可变页面，header 冻结
`manifestId`、root hash、目标/页计数和 `availableUntil`；preflight JSONB 与 run intent 只保存小型引用，
不复制万级对象清单。任一中间日必需交付缺件或按剩余保留期无法完成时 `eligible=false`，submit 返回
`preflight-rejected`，不会创建 command/run。页面和页内交易日均按日期降序冻结，FULL/DATE_RANGE
优先形成最近 publication；执行器按 header 水位只加载一个页面，每次只复核并消费最多五个最新待处理
完整业务日后自动让出全局 slot；同一 run 的成功日包分区可在 worker loss 后续跑，不重新下载成功
前缀，也不要求用户手工拆日期。正常 `YIELDED` 不消耗三次 worker-loss 恢复预算。

交付读取默认上限分别为 64 MiB、manifest 256 KiB、ZIP 压缩比 100，可通过
`DATA_SYNC_STOCK_CONNECT_MAX_DELIVERY_BYTES`、`DATA_SYNC_STOCK_CONNECT_MAX_MANIFEST_BYTES`、
`DATA_SYNC_STOCK_CONNECT_MAX_ZIP_COMPRESSION_RATIO` 调整。原始字节留存默认
`DATA_SYNC_STOCK_CONNECT_RAW_RETENTION_MODE=MANIFEST_ONLY`；只有显式设置
`LICENSED_RAW_ALLOWED`，并同时提供 `DATA_SYNC_STOCK_CONNECT_RAW_RETENTION_LICENSE_REFERENCE` 与
`DATA_SYNC_STOCK_CONNECT_RAW_KMS_KEY_ID` 时，来源字节才可进入许可暂存路径；成功与失败共用同一
许可引用和 KMS 门禁，任何路径都不能绕过。当前成功 publication 会主动丢弃暂存字节，只保存摘要和
不可回放引用；失败诊断才会把许可字节与 manifest 一起加密写入私有桶。许可引用同时写入失败 manifest
的 `rightsEvidenceRef` 和 canonical `DataSource.rights_evidence_ref`；`MANIFEST_ONLY` 在成功、失败两条
路径均不持久化来源字节。

## 申万行业与日频资金流

申万同步默认关闭；启用 `DATA_SYNC_AKSHARE_ENABLED=true`、`DATA_SYNC_SECTOR_ENABLED=true` 与
`DATA_SYNC_SW_SECTOR_ENABLED=true` 后，可运行：

```bash
data-sync-sw-sector --snapshot-date 2026-07-28
```

日频资金流默认关闭；启用 `DATA_SYNC_AKSHARE_ENABLED=true` 与 `DATA_SYNC_MONEY_FLOW_ENABLED=true`
后，可按 CLI 帮助选择个股、板块、市场或供应商排行分区：

```bash
data-sync-money-flow --help
```

申万机器契约见
[`0020-data-sync-sw-sector-internal.openapi.yaml`](../docs/contracts/0020-data-sync-sw-sector-internal.openapi.yaml)；
资金流机器契约见
[`0015-data-sync-daily-money-flow-internal.openapi.yaml`](../docs/contracts/0015-data-sync-daily-money-flow-internal.openapi.yaml)。
