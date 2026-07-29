# service-data-sync

财经与股票基础数据同步服务。当前包含工程基础设施、个股日/周/月独立行情、复权因子、公司行动、公司概况、
行业/概念板块三周期行情、申万三级行业与估值、A 股证券主数据和显式上市生命周期、财务与估值、日频资金流。
各链路经 provider-neutral port、独立 adapter、S3 失败排障证据、PostgreSQL canonical revision、
publication、CLI 与 Celery 任务闭环。

技术方案见 [0001：同步服务工程基础设施](../docs/service-data-sync/0001-data-sync-foundation/index.html)。
板块跨服务读取与 API 路径见
[0003：板块行情 API 访问技术方案](../docs/service-api/0003-sector-market-data-access/index.html)（已实现内部与公开读取路由）。

## 当前边界

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
- 已实现行业/概念板块 EOD 完整横截面 adapter、失败证据、revision、质量门、内部读取、受控手工 CLI、shadow scheduler、租约 reaper、publication rollback、结构化任务日志与 2026 沪深公告交易日历。它只保存 `post_close_observation`，不宣称官方终态；全部 EOD 开关默认关闭。2026 年以外日期安全阻断，且单位对账、连续探针与生产观测验收仍未完成；仓内未选择或引入监控平台。
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

先启动本地数据同步基础设施并完成迁移，再显式开启研究来源：

~~~sh
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env.example \
  --profile data-sync-infra up -d
docker build --target test --tag quant-v2/service-data-sync:test service-data-sync
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env.example \
  --profile data-sync-infra --profile data-sync-test run --rm data-sync-test \
  /bin/sh -ec 'alembic upgrade head && DATA_SYNC_AKSHARE_ENABLED=true \
  DATA_SYNC_EQUITY_MARKET_ENABLED=true \
  data-sync-equity-bars --instrument SSE.600519 --period 1w --full-history'
~~~

未传 `--start` 与 `--end` 时，CLI 从当日向前取 31 个自然日。`--start`、`--end` 均为包含端 ISO 日期；
重复同一窗口只记录新的 raw 观测，不会创建重复 canonical revision 或新的 publication version。参考数据使用：

~~~sh
DATA_SYNC_AKSHARE_ENABLED=true DATA_SYNC_EQUITY_MARKET_ENABLED=true \
data-sync-equity-reference --instrument SSE.600519 --dataset all --full-history
~~~

`--dataset` 可选 `factor`、`action`、`profile` 或 `all`。内部机器契约见
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

目录 CLI 可按交易所或按三所协调运行。东财目录来源仅支持当日 Shanghai 市场日，且只能用于已明确开启的
research/pilot 环境；它不提供退市、暂停或恢复的证据。

~~~sh
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env.example \
  --profile data-sync-infra up -d
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env.example \
  --profile data-sync-infra --profile data-sync-test run --rm data-sync-test \
  /bin/sh -ec 'alembic upgrade head && DATA_SYNC_AKSHARE_ENABLED=true \
  data-sync-equity-catalog --all-exchanges'
~~~

`data-sync-equity-lifecycle --exchange SSE --target-date YYYY-MM-DD` 使用固定版本交易所显式事实 adapter；
可通过 replay 模式从最后成功 checkpoint 恢复而不再次访问上游。退市后状态反转只能使用
`OFFICIAL_CORRECTION` 并携带可追溯来源证据引用；代码复用候选会被隔离，不能绑定到已退市证券。

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

诊断成功后启动空载 worker：

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

## ETF、两融、港通、公告与交易公开信息 P0

这些 P0 CLI 默认使用个人内部研究 AKShare 元数据和禁止再分发范围；可按参数覆盖真实上游。设置
`DATA_SYNC_AKSHARE_ENABLED=true` 后，组合根注册唯一 `provider_id=akshare` 的统一 P0 adapter，目录、状态、
日线、净值、两融、港通、公告、龙虎榜、大宗交易和真实合约日线均按独立 capability 发布；一个任务不会用另一个
dataset 补齐字段。未开启 AKShare 时命令仍写入 <code>source_unavailable</code> 观测并成功返回空结果，不会阻断 API 消费。

AKShare 没有“官方成交活跃前十”接口，不能把持股或估算增持排行混入该 dataset；该 capability 当前返回合法空数组。
AKShare 也没有上交所两融标的名单接口，上交所资格 capability 返回合法空数组。ETF 目录只支持当日快照；请求历史
目录日期同样返回空数组。上述空值均会由 API 投影为空列表或空字段，不构成同步失败。

```bash
data-sync-etf --operation master --venue SSE --observation-date 2026-07-29

data-sync-margin --operation market --venue SSE --start 2026-07-28 --end 2026-07-28

data-sync-stock-connect --operation market --channel SH --direction NORTHBOUND \
  --start 2026-07-28 --end 2026-07-28
```

`data-sync-corporate-events`、`data-sync-trading-events` 也可无来源参数运行；前者只接收
`--start/--end`，后者另需 `--operation dragon-tiger|block-trade`。这些命令默认仅供受控手工运行，未配置自动调度。

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
