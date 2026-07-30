# quant-v2

由三个可独立构建、测试、运行和部署的服务组成：

- `service-data-sync/`：Python 财经与股票基础数据同步服务。
- `service-api/`：NestJS 用户与鉴权 API。
- `service-web/`：React 数据与分析 SPA。

服务通过明确契约交互，不跨服务导入源码；根目录 Docker Compose 只负责本地编排和单节点生产基线。

## Docker 环境

公共拓扑位于 `compose.yaml`，必须叠加一个环境文件：

- `compose.dev.yaml`：本地构建、Web/API 热更新、回环端口和集成测试。
- `compose.prod.yaml`：不可变发布镜像、内部基础设施网络和业务容器最小权限。

首次本地启动：

```bash
cp .env.example .env
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env \
  --profile web --profile api --profile data-sync-infra --profile data-sync-worker \
  up --build
```

常用访问地址：

- Web：`http://127.0.0.1:15173`
- API：`http://127.0.0.1:13000`
- API OpenAPI：`http://127.0.0.1:13000/openapi`
- MinIO Console：`http://127.0.0.1:19001`

停止并保留数据：

```bash
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env --profile '*' down
```

不要把 `down -v` 作为日常命令。

## 配置与 secret

- 根 `.env` 保存本地凭据并已被 Git 和 Docker build context 忽略。
- 根 `.env.example` 维护相同配置契约，只含可提交假值；不得用于 staging 或 production。
- `service-api/.env.example` 供 API 宿主机运行，`service-web/.env.example` 只含浏览器公开变量。
- `VITE_*` 会进入浏览器产物，不能保存 secret；生产值必须在构建 Web 镜像时确定。
- Web 发布构建通过 `--build-arg VITE_API_BASE_URL=...` 注入公开 API origin。
- 生产镜像变量 `*_IMAGE_REF` 必须替换为发布 CI 生成的 immutable digest。

沪深港通正式链路默认失败关闭。启用前，部署 provisioner 必须把 HKEX SFTP
私钥、严格 `known_hosts` 和摘要固定的 Securities Master profile 写入
`data_sync_stock_connect_config` 卷，把 OMD-C、SSE MDGW、SZSE STEP
终态交付及 sidecar manifest 写入 `data_sync_stock_connect_status` 卷；API 与
worker 只读挂载这两个卷。随后设置 `.env.example` 所列
`DATA_SYNC_STOCK_CONNECT_*`/`DATA_SYNC_HKEX_*` 变量，通过数据运维
`market.stock_connect.overview.bundle` 的 `MARKET + ALL` 任务形成首个正式
publication，完成三服务对账后再设置 `STOCK_CONNECT_API_ENABLED=true`。缺少授权或
任一交付时，提交前 preflight 会分组件拒绝，不会排队后才失败，也不会使用网页抓取或
样本数据替代。

生产配置预检：

```bash
docker compose -f compose.yaml -f compose.prod.yaml --env-file /protected/quant-v2.env \
  --profile web --profile api --profile data-sync-infra --profile data-sync-worker \
  config --quiet
```

预检通过后使用同一组参数执行 `up -d`。生产 PostgreSQL、Redis 和 MinIO 不发布宿主机端口；
生产配置必须把 `COMPOSE_PROJECT_NAME` 改为独立名称（例如 `quant-v2-prod`），避免复用开发 volume。
TLS、反向代理、托管存储和高可用部署仍由后续方案决定。

详细设计：[Docker 开发与生产环境方案](docs/architecture/0001-docker-environments/index.html)。

## 文档

- [代理与仓库规则](AGENTS.md)
- [参与开发](CONTRIBUTING.md)
- [文档索引](docs/README.md)
- [架构边界](docs/architecture/README.md)
- [架构决策记录](docs/decisions/README.md)
