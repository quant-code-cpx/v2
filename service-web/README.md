# service-web

量化数据与分析 Web SPA。独立构建、测试、运行与部署；仅通过版本化 `service-api` 契约获取数据。

当前基础：Vite+ 0.2.6、pnpm 11.17.0、Node.js 24.18.0、React 19、React Router 7、MUI 7、
TypeScript 7、TanStack Query、KLineChart 10 和 ECharts 6。页面暂用受控 fixture，未直连 API、
数据库或供应商。

## 支持范围

产品仅支持 PC 桌面浏览器，最低宽度为 `1200px`，默认设计与视觉验收视口为 `1440×900`。
页面使用固定桌面信息架构；验证覆盖鼠标、键盘、语义化、焦点、对比度和 reduced-motion。

源码按经典前端职责分层：<code>api</code>、<code>components</code>、<code>config</code>、<code>hooks</code>、<code>libs</code>、<code>mocks</code>、<code>router</code>、<code>styles</code>、<code>types</code>、<code>utils</code> 和 <code>views</code>。未冻结契约只能放入 <code>mocks</code>，不能伪造真实接口。

## 页面与组件组织

路由页、页面私有能力和共享组件按真实依赖归档。路由页必须独立目录；单文件组件保持扁平：

```text
src/
├── views/
│   └── UserManagementView/
│       ├── UserManagementView.tsx
│       ├── components/
│       │   ├── UserFilters.tsx
│       │   └── UserTable.tsx
│       ├── hooks/
│       │   └── useUserManagement.ts
│       └── utils/
│           └── user-date-formatters.ts
└── components/
    ├── AuthProvider.tsx
    └── AppShell/
        ├── AppShell.tsx
        ├── components/
        │   ├── AppHeader.tsx
        │   └── AppSidebar.tsx
        └── hooks/
```

- 路由页只组合信息层级与页面状态；远程查询、表单副作用、复杂交互进入页面 Hook。
- 页面专用组件与函数就近放置；确认被多个页面使用后，才提升到共享 `components/`、`hooks/` 或
  `utils/`。
- 单文件组件直接放在所属 `components/`；只有存在多个强关联文件时才建立同名功能目录。不创建
  `index.ts` barrel，import 与路由懒加载直达具体文件。
- 组件拆分依据复用、状态、副作用、业务语义、渲染隔离或独立测试边界。单次使用、无状态、少量 JSX
  的片段保留在父组件或同一文件，不为缩短行数制造微型转发组件。
- MUI 样式按 `sx`、`styled()`、`theme.components`、全局基线的最小适用范围选择；视觉常量只来自
  `styles/design-tokens.*` 与 `styles/theme.tsx`。
- 生产 UI 组件或 Hook 接近 200 行、页面编排接近 150 行时，评审其是否混合了状态、数据、动作、
  纯函数或多个视觉任务；按职责拆分，不按行数制造无意义碎片。

## 测试归属

测试文件不得与源码同层：

```text
src/
├── views/
│   └── UserManagementView/
│       ├── UserManagementView.tsx
│       └── test/
│           └── UserManagementView.test.tsx
├── api/
│   ├── auth-session.ts
│   └── test/
│       └── auth-session.test.ts
└── utils/
    ├── return-to.ts
    └── test/
        └── return-to.test.ts
```

页面目录的 `test/` 统一承载该页面、私有组件、Hook 与工具函数测试。共享功能使用自身目录的 `test/`；
跨页面 E2E 保留在服务级 `e2e/`。禁止重新引入 `source.ts` 与 `source.test.ts` 同层结构。

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
