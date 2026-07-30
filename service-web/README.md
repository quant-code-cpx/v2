# service-web

量化数据与分析 Web SPA。独立构建、测试、运行与部署；仅通过版本化 `service-api` 契约获取数据。

当前基础：Vite+ 0.2.6、pnpm 11.17.0、Node.js 24.18.0、React 19、React Router 7、MUI 7、
TypeScript 7、TanStack Query、KLineChart 10 和 ECharts 6。鉴权与用户管理已接入真实
`service-api`；个人中心、活动会话、安全审计、角色工作台与用户统计已按 Contract 0017 接入，
市场概览、东财板块和申万行业页面已接入真实 publication API。前端不会直连数据库或供应商。

所有 `service-api` 请求由共享传输层固定为 `POST`，业务调用方不能覆盖 method。浏览器 CORS
preflight 由浏览器和服务端框架处理；`service-web` 自身的 `/healthz` 仍使用普通 `GET` 健康检查。

## 支持范围

产品仅支持 PC 桌面浏览器，最低宽度为 `1200px`，默认设计与视觉验收视口为 `1440×900`。
页面使用固定桌面信息架构；验证覆盖鼠标、键盘、语义化、焦点、对比度和 reduced-motion。

源码按经典前端职责分层：<code>api</code>、<code>components</code>、<code>config</code>、<code>hooks</code>、<code>libs</code>、<code>mocks</code>、<code>router</code>、<code>styles</code>、<code>types</code>、<code>utils</code> 和 <code>views</code>。未冻结契约只能放入 <code>mocks</code>，不能伪造真实接口。

## 市场概览与行业板块

已落地的受保护桌面路由：

- `/market`：原子市场完整包、四个主要指数、成交/宽度/涨跌停、资金流、股票与板块排行。
- `/market/sectors`：东财行业/概念目录、EOD 横截面、强弱持续性和板块资金流。
- `/market/sectors/:scheme/:sectorCode`：板块 EOD、原生日/周/月 K 线和观察成分。
- `/market/industries/sw`：申万一级、二级、三级 taxonomy 与逐字段估值。
- `/market/industries/sw/:code`：申万节点估值、已物化日/周/月 K 线和最新修订有效成员。

市场客户端只调用 `service-api` POST。200 必须同时通过 strict Zod、强 `ETag` 和
`X-Data-Version`/body 版本一致性校验；204 只能复用同一 query key 已校验且两个响应头完全匹配的
缓存实体。首页不从多个 latest publication 读时拼接；详情 publication 独立失败和局部重试。
生产路径禁止 market fixture、mock、demo、随机数、补零或静默换源。指数成交量单位固定显示“手”；
申万成交量保持“供应商原生单位、跨来源不可比”。K 线使用 KLineChart，资金流/强弱使用 ECharts，
股票排行只深链 `/market/equities/:exchange/:symbol`。

市场定向验证入口：

```bash
vp check src/api/market.ts src/api/test/market.test.ts src/types/market.ts \
  src/views/MarketOverviewView src/views/MarketSectorsView \
  src/views/MarketSectorDetailView src/views/SwIndustriesView \
  src/views/SwIndustryDetailView
vp test src/api/test/market.test.ts
vp build
```

完整方法学、数据许可/HTTPS 门禁、三服务路由、降级和验证状态见
[0008 市场概览与行业板块](../docs/service-web/0008-market-overview-and-sectors/index.html)；可编辑原型见
[prototype.html](../docs/service-web/0008-market-overview-and-sectors/prototype.html)。

## 沪深港通与跨境互联互通

已落地的受保护桌面路由：

- `/market/stock-connect`：四通道共同已发布日总览、分币种趋势和来源活跃证券榜。
- `/market/stock-connect/:channel`：单通道日终统计、状态、额度和来源榜内可用净额排序。
- `/market/stock-connect/securities/:instrumentEntityRef`：稳定证券在互联互通范围内的最小上下文。

页面只调用 service-api 的五个 POST：readiness、overview、channel、active-securities 与
security-context。readiness 独立显示持久化官方日历、交付、执行和 publication 证据；
浏览器会重算其规范正文 SHA-256。业务 publication 与 readiness 任一失败都不会互相伪装成功。
活跃证券恒为官方来源活跃榜范围，不描述为全市场排行；成交额不标为净流入，CNY/HKD 不合计，
北向制度未披露日不推导买卖额或净额。完整方案和可编辑原型见
[0010 沪深港通中心](../docs/service-web/0010-stock-connect-center/index.html)。

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

市场 EOD publication API 已落地；尚未冻结实时/分时协议，不添加猜测性 endpoint 或 token 配置。

技术方案见 [docs/service-web/](../docs/service-web/)。
