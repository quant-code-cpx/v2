# service-data-sync

财经与股票基础数据同步服务。当前实现仅包含工程基础设施：配置、日志、PostgreSQL/Redis/S3 连通性诊断、
空载 Celery worker、provider-neutral port 和架构测试。

技术方案见 [0001：同步服务工程基础设施](../docs/service-data-sync/0001-data-sync-foundation/index.html)。

## 当前边界

- 没有 FastAPI、HTTP route、OpenAPI、service-api 集成。
- 没有业务表、业务模型、Alembic revision、定时任务或真实同步任务。
- 未安装、未调用 AKShare、Tushare 或其他供应商 SDK。
- 所有未来外部数据只能通过 provider-neutral port 与独立 adapter 获取。
- application、task、质量、持久化代码禁止直接调用数据源 SDK、HTTP 或具体 adapter。

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
