# service-data-sync

财经与股票基础数据同步服务。当前包含工程基础设施、P0 个股未复权日线、行业/概念板块三周期行情，以及
A 股证券主数据和显式上市生命周期的受控同步闭环：provider-neutral port、独立 adapter、S3 raw evidence、
PostgreSQL canonical revision 和人工 CLI。

技术方案见 [0001：同步服务工程基础设施](../docs/service-data-sync/0001-data-sync-foundation/index.html)。
板块跨服务读取与 API 路径见
[0003：板块行情 API 访问技术方案](../docs/service-api/0003-sector-market-data-access/index.html)（已实现内部与公开读取路由）。

## 当前边界

- 已实现 FastAPI 内部只读路由 `GET /internal/v1/sectors`、`GET /internal/v1/sectors/{scheme}/{sectorCode}`
  和 `GET /internal/v1/sectors/{scheme}/{sectorCode}/bars`，以及证券主数据
  `GET /internal/v1/equities`、`GET /internal/v1/equities/{exchange}/{symbol}` 和
  `GET /internal/v1/equities/{exchange}/{symbol}/listing-status-history`。路由仅接受
  `DATA_SYNC_INTERNAL_API_BEARER_TOKEN`，不暴露 raw、供应商字段、数据库键或 `PENDING` 身份。
- 已有 P0 Alembic revision：证券占位身份、source batch、按年分区的日线修订和 publication 元数据。
- 已实现 AKShare 腾讯未复权日线 adapter；默认关闭，只有设置 `DATA_SYNC_AKSHARE_ENABLED=true` 才会注册。
- 已实现东财行业/概念板块的日线、周线、月线 adapter；三个周期分别调用上游参数、分别存入
  `sector_daily_bar`、`sector_weekly_bar`、`sector_monthly_bar`，绝不由日线计算。除 `DATA_SYNC_AKSHARE_ENABLED=true`
  外，还必须显式设置 `DATA_SYNC_SECTOR_ENABLED=true`。
- 已实现行业/概念目录 CLI；目录快照写入 raw evidence 后会激活带名称的 `ACTIVE` 身份。行情先创建的 `PENDING` 身份会保留 UUID 后升级；已实现板块成分当前快照、观测区间、固定 release、双向 internal 读取与手动 CLI。
- 已实现行业/概念板块 EOD 完整横截面 adapter、raw evidence、revision、质量门、内部读取、受控手工 CLI、shadow scheduler、租约 reaper、publication rollback、结构化任务日志与 2026 沪深公告交易日历。它只保存 `post_close_observation`，不宣称官方终态；全部 EOD 开关默认关闭。2026 年以外日期安全阻断，且许可、单位对账、连续探针与生产观测验收仍未完成；仓内未选择或引入监控平台。
- 默认日线 CLI 只拉最近 31 个自然日；首次回填、复权、财务和定时调度尚未实现。
- 已实现证券目录、双时间身份/名称/上市状态、交易所独立 publication、全市场稳定聚合、内部读取和公开 API
  代理。目录缺席、空响应和行情缺席不会改变生命周期；显式退市、暂停和恢复只能经专用生命周期 port 发布。
- 生命周期 CLI 已实现，但当前没有获准的生命周期来源 adapter；`data-sync-equity-lifecycle` 会拒绝空注册表。
  东财目录 adapter 仅限 research/pilot，不构成生产来源许可。
- 所有未来外部数据只能通过 provider-neutral port 与独立 adapter 获取。
- application、task、质量、持久化代码禁止直接调用数据源 SDK、HTTP 或具体 adapter。

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

## P0 最近一个月同步

先启动本地数据同步基础设施并完成迁移，再显式开启研究来源：

~~~sh
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env.example \
  --profile data-sync-infra up -d
docker build --target test --tag quant-v2/service-data-sync:test service-data-sync
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env.example \
  --profile data-sync-infra --profile data-sync-test run --rm data-sync-test \
  /bin/sh -ec 'alembic upgrade head && DATA_SYNC_AKSHARE_ENABLED=true \
  data-sync-equity-bars --instrument SSE.600519'
~~~

未传 `--start` 与 `--end` 时，CLI 从当日向前取 31 个自然日。`--start`、`--end` 均为包含端 ISO 日期；
重复同一窗口只记录新的 raw 观测，不会创建重复 canonical revision 或新的 publication version。

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

上例只适用于获准的 local/research 来源。生产开关继续保持关闭，直到许可、频率、留存和连续稳定性完成评审。

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

该命令默认只写 `candidate`，只能用于获准的 local/research/shadow。2026 年可在显式开启
`DATA_SYNC_TRADING_CALENDAR_ENABLED=true` 后使用沪深公告日历；其他年份、来源许可、单位与连续稳定性仍未获批准，
缺少或未知日历时命令会在访问 provider 前停止。只有 `DATA_SYNC_SECTOR_EOD_PUBLISH_ENABLED=true` 且显式传入
`--publish` 才会推进 `dataset_publication`，生产开关必须保持关闭。

发生归一化或质量规则变更时，可用 `--replay-raw` 按该分区已 checkpoint 的 raw evidence 重放；该模式不会调用上游，且
仅在已有来源观测后可用。

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

成分 CLI 只接受单一、已显式开启的来源；默认 `DATA_SYNC_SECTOR_MEMBERSHIP_ENABLED=false`，且 AKShare/东方财富
许可、频率、raw 留存与再分发边界尚未获生产批准。完整快照才会更新 observed 区间；PENDING、quarantine、空响应、
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

`data-sync-equity-lifecycle --exchange SSE --target-date YYYY-MM-DD` 仅在平台注册一个已经过来源、字段语义、
频率和留存审批的显式生命周期 adapter 后可运行。退市后状态反转只能使用 `OFFICIAL_CORRECTION`，并且标准
证据必须携带人工审批引用；代码复用候选会被隔离，不能绑定到已退市证券。

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
