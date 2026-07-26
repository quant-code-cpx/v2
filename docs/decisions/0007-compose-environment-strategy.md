# 0007：Docker Compose 开发与生产环境分层

- 状态：Accepted
- 日期：2026-07-26
- 决策者：项目维护者
- 关联方案：[Docker 开发与生产环境方案](../architecture/0001-docker-environments/index.html)

## 背景

三个服务已具备初始 Dockerfile，根目录已有一个 Compose 文件同时承载本地端口、测试服务、生产镜像阶段和
长期运行配置。单文件无法清晰表达以下差异：

- 开发环境需要本机构建、源码挂载、热更新和仅回环可见的依赖端口。
- 生产环境需要不可变镜像、隔离网络、无基础设施宿主机端口和容器最小权限。
- PostgreSQL、Redis、JWT、MinIO/S3 等凭据必须离开 Compose 文件，由不提交的环境文件或部署 secret 注入。
- 三个服务仍须独立构建、测试、运行和部署，不能因根编排而产生源码依赖。

参考项目 `quant-code/server-code` 已采用独立开发/生产 Compose 与 `.env` 注入，但直接复制两份完整拓扑会让
服务、健康检查和依赖关系逐渐漂移。本仓库需要保留该环境分离思路，同时减少重复。

## 约束

- Docker Compose v2 是当前本地和单机部署编排入口。
- `service-api` 的 migration 必须独立于应用启动。
- `service-data-sync` 必须保留访问外部数据源的出口，但 PostgreSQL、Redis 和 MinIO 只在内部网络可见。
- `service-web` 的 `VITE_*` 值在镜像构建时进入浏览器产物，不是 secret，也不能靠运行时 `.env` 修改。
- 生产部署平台、TLS 终止、反向代理、集中式 secret manager 和高可用拓扑尚未决定。

## 候选方案

1. 开发和生产各维护一份完整 Compose 文件。
   - 优点：入口直观，每份文件可单独阅读。
   - 缺点：服务依赖、健康检查、volume 和网络定义重复，容易漂移。
2. 一个公共 `compose.yaml`，叠加 `compose.dev.yaml` 或 `compose.prod.yaml`。
   - 优点：公共拓扑只有一份；环境差异显式；服务仍可通过 profile 独立启动。
   - 缺点：操作命令必须始终指定两个文件；排查时需要查看合并结果。
3. 一个 Compose 文件，仅用 profile 和大量环境变量切换。
   - 优点：文件数量少。
   - 缺点：profile 只控制服务启用，不能清楚表达 build、bind mount、端口暴露和安全策略差异。

## 决策

采用方案 2。

- `compose.yaml` 只定义公共服务拓扑、依赖、健康检查、内部端口、网络和 named volume。
- `compose.dev.yaml` 只定义本地 build target、源码挂载、热更新、测试服务和
  `127.0.0.1` 端口映射。
- `compose.prod.yaml` 禁止 `build`，只接受发布流水线生成的不可变镜像引用；数据库、Redis 和 MinIO
  不发布宿主机端口；业务容器启用只读文件系统、`no-new-privileges` 与 capability drop。
- 开发和生产必须使用不同的 `COMPOSE_PROJECT_NAME`，让 named volume 与网络自然隔离。
- 根 `.env` 是本地敏感配置文件并由 Git 和 Docker build context 忽略；根 `.env.example`
  维护相同配置契约和可运行的假值。生产环境由受保护的 env 文件或部署 secret 注入真实值。
- 生产镜像引用通过 `*_IMAGE_REF` 注入并要求 digest；示例只提交保留域名和零 digest。
- `service-api-migrate` 保持一次性进程，成功后 API 才启动。生产应用不得自动迁移。
- `service-api-internal` 与 `data-sync-internal` 在生产设为 internal network。
  同步 worker 额外挂载非 internal 出口网络，基础设施不加入该网络。

## 后果

- 修改公共拓扑只需更新一处；开发和生产差异可通过 `docker compose config` 审查。
- 开发环境支持 API 与 Web 热更新；数据同步服务使用包含开发依赖的镜像和只读源码挂载。
- 生产 Compose 适合单机或单节点部署基线，不承诺高可用、滚动发布、TLS 或 secret manager 能力。
- 生产发布前必须先构建并推送 Web、API、API migration 和 data-sync 镜像，再替换所有假 digest。
- `.env.example` 可安全提交，但其值不具备任何安全性，不能进入 staging 或 production。
- Vite 浏览器配置仍由 Web 发布镜像的构建流水线固定；运行时修改 Compose env 不会重写静态资源。

## 回滚

保留原 named volume，不执行 `down -v`。若新入口失败，可停止当前项目，使用上一个已验证镜像 digest
重新渲染并启动生产 Compose。Compose 文件回滚不修改数据库；数据库 schema 回滚仍按各服务 migration
策略单独执行。

## 待决问题

- TLS、域名和统一反向代理由后续部署平台 ADR 决定。
- 生产是否使用托管 PostgreSQL、Redis 和对象存储，由容量、可用性目标和部署平台决定。
- 集中式 secret manager 与镜像签名/验证机制由发布流水线方案决定。

## 替代关系

无。
