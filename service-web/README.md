# service-web

量化数据与分析 Web SPA。独立构建、测试、运行与部署；仅通过版本化 `service-api` 契约获取数据。

当前基础：Vite+ 0.2.6、pnpm 11.17.0、Node.js 24.18.0、React 19、React Router 7、MUI 7、
TypeScript 7、TanStack Query、KLineChart 10 和 ECharts 6。页面暂用受控 fixture，未直连 API、
数据库或供应商。

源码按经典前端职责分层：<code>api</code>、<code>components</code>、<code>config</code>、<code>hooks</code>、<code>libs</code>、<code>mocks</code>、<code>router</code>、<code>styles</code>、<code>types</code>、<code>utils</code> 和 <code>views</code>。未冻结契约只能放入 <code>mocks</code>，不能伪造真实接口。

## 命令

在本目录执行：

```bash
vp install --frozen-lockfile
vp dev
vp check
vp test
vp build
```

E2E：

```bash
vp exec playwright install chromium
vp build
vp run e2e
```

容器：

```bash
docker build --tag quant-v2/service-web:local .
docker run --rm -p 15173:8080 quant-v2/service-web:local
curl --fail http://127.0.0.1:15173/healthz
```

开发容器与热更新从仓库根目录启动：

```bash
docker compose -f compose.yaml -f compose.dev.yaml --env-file .env \
  --profile web up --build
```

公开浏览器配置写在 `.env`，变量必须以 `VITE_` 开头；参见 [.env.example](.env.example)。
`VITE_*` 会编译进静态资源，不能保存 secret；生产镜像必须在发布构建阶段注入正确值，
运行时 Compose 环境变量不会重写已构建资源。生产 Compose 只接受 `SERVICE_WEB_IMAGE_REF`
immutable digest。

```bash
docker build --build-arg VITE_API_BASE_URL=https://api.example.invalid \
  --tag quant-v2/service-web:release .
```

真实 API/实时协议落地前，不添加猜测性 endpoint 或 token 配置。

技术方案见 [docs/service-web/](../docs/service-web/)。
