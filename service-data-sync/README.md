# service-data-sync

财经与股票基础数据同步服务。当前包含工程基础设施、P0 个股未复权日线，以及行业/概念板块三周期行情的受控同步闭环：
provider-neutral port、AKShare adapter、S3 raw evidence、PostgreSQL canonical revision 和人工 CLI。

技术方案见 [0001：同步服务工程基础设施](../docs/service-data-sync/0001-data-sync-foundation/index.html)。
板块跨服务读取与 API 路径见
[0003：板块行情 API 访问技术方案](../docs/service-api/0003-sector-market-data-access/index.html)（已实现内部与公开读取路由）。

## 当前边界

- 已实现 FastAPI 内部只读路由 `GET /internal/v1/sectors`、`GET /internal/v1/sectors/{scheme}/{sectorCode}`
  和 `GET /internal/v1/sectors/{scheme}/{sectorCode}/bars`；仅接受 `DATA_SYNC_INTERNAL_API_BEARER_TOKEN`，不暴露 raw、供应商字段或 `PENDING` 身份。
- 已有 P0 Alembic revision：证券占位身份、source batch、按年分区的日线修订和 publication 元数据。
- 已实现 AKShare 腾讯未复权日线 adapter；默认关闭，只有设置 `DATA_SYNC_AKSHARE_ENABLED=true` 才会注册。
- 已实现东财行业/概念板块的日线、周线、月线 adapter；三个周期分别调用上游参数、分别存入
  `sector_daily_bar`、`sector_weekly_bar`、`sector_monthly_bar`，绝不由日线计算。除 `DATA_SYNC_AKSHARE_ENABLED=true`
  外，还必须显式设置 `DATA_SYNC_SECTOR_ENABLED=true`。
- 已实现行业/概念目录 CLI；目录快照写入 raw evidence 后会激活带名称的 `ACTIVE` 身份。行情先创建的 `PENDING` 身份会保留 UUID 后升级；成份、申万体系、EOD 快照、资金流和调度仍未实现。
- 默认 CLI 只拉最近 31 个自然日；首次回填、交易所主数据、复权、财务和定时调度尚未实现。
- 所有未来外部数据只能通过 provider-neutral port 与独立 adapter 获取。
- application、task、质量、持久化代码禁止直接调用数据源 SDK、HTTP 或具体 adapter。

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
