# 0006：Web 前端运行时与工程技术栈

- 状态：Accepted
- 日期：2026-07-25
- 决策者：项目维护者

## 背景

`service-web` 在本 ADR 创建时尚未初始化，需要确定运行时、包管理、构建工具、语言、UI、路由、状态、请求、
样式、可视化与质量门禁；基础工程已于 2026-07-26 按本决策完成。前端必须独立构建、测试、运行和部署，只能通过版本化契约访问
`service-api`，不得直连数据库或第三方数据源。

用户要求使用 Vite+、pnpm、React、React Router、Material UI、最新版 TypeScript 和 ECharts。
仍需判断 Vite+ Beta 与 TypeScript 原生实现的成熟度。可视化需要同时满足专业 K 线交互和
非 K 线分析数据展示；用户已确认前者使用 KLineChart，后者使用 ECharts。

详细设计见
[Web 前端工程基础与双图表实现方案](../service-web/0001-frontend-foundation/index.html)。

## 候选方案

1. Vite+ + pnpm + React 19 + TypeScript 7 + React Router Data Mode + MUI v7。
   - 优点：统一 Vite、Vitest、Oxlint、Oxfmt、类型检查和任务运行；TypeScript 7 原生工具链与
     Vite+ 的 tsgolint 路线一致；SPA 保留标准 Vite 生态。
   - 缺点：Vite+ 仍处 Beta；TypeScript 7.0 尚无稳定编程 API；需要对工具版本和升级流程加约束。
2. 标准 Vite + pnpm + React 19 + TypeScript 6 + ESLint + Prettier。
   - 优点：组件成熟、集成案例多，依赖旧 TypeScript API 的工具兼容性最好。
   - 缺点：违反 Vite+ 和最新 TypeScript 要求；配置与重复分析更多，之后迁移成本更高。
3. React Router Framework Mode + SSR。
   - 优点：内建类型安全路由、数据加载、代码拆分，并可扩展 SSR/SSG。
   - 缺点：当前没有 SEO 或首屏服务端渲染需求，会提前引入服务端运行时和部署复杂度。

可视化候选：

1. ECharts 同时承担 K 线和分析图。
   - 优点：单引擎、主题统一、依赖较少。
   - 缺点：专业指标、overlay、磁吸和交易终端手势需要自行建设。
2. KLineChart 同时承担全部图表。
   - 优点：K 线能力完整。
   - 缺点：不适合作为通用分析可视化平台，研究图表种类和生态不如 ECharts。
3. KLineChart + ECharts 按能力分工。
   - 优点：K 线和通用分析分别使用专用引擎，避免重复自研金融交互。
   - 缺点：增加包体、实例生命周期、主题、测试和升级治理成本。
4. TradingView Lightweight Charts + ECharts。
   - 优点：K 线轻量、实时更新性能好。
   - 缺点：指标和高级画线仍需插件或自建，并有 attribution 要求。

## 决策

采用方案 1，状态为 Accepted；2026-07-26 已完成工程与双图表 fixture 基线：

- Node.js 24 作为运行时基线，pnpm 11 作为包管理器；初始化时固定精确版本并提交 lockfile。
- 使用 Vite+ Beta 初始化和管理开发、检查、测试、构建与任务。固定已验证版本，升级使用独立 PR。
- 使用 React 19 最新稳定小版本、React Router Data Mode 和 MUI v7。
- 使用 TypeScript 7.0.x 作为应用语言和类型检查器，开启严格检查和
  `noUncheckedIndexedAccess`，不使用 TypeScript 7 已移除的 `baseUrl`。
- 使用 MUI Theme、Emotion、`sx` 与 CSS Modules；不同时引入 Tailwind。
- 使用 TanStack Query 管服务端状态；存在真实跨路由客户端 UI 状态时使用 Zustand，
  禁止把服务端缓存或认证 token 放入 Zustand。
- API 契约落地后使用 openapi-typescript、openapi-fetch 与 openapi-react-query 生成并消费类型；
  表单使用 React Hook Form，运行时边界使用 Zod。
- 采用可视化候选 3：KLineChart 10.0.x 专职 K 线、成交量、技术指标、十字光标、缩放、
  overlay、磁吸和实时 Candle；ECharts 6.1.x 专职行情宽度、资金流、因子、相关性、
  收益分布和回测等非 K 线分析图。
- ECharts 禁止实现 candlestick series，KLineChart 禁止承载通用分析卡片。两引擎只共享
  canonical 数据模型、视觉令牌和错误语义，不建立覆盖两者的万能 chart adapter。
- 两引擎固定初始化当天验证过的精确版本，按 feature/route 动态加载；不引入 React 包装库。
- 使用 Vite+ 内置 Vitest，配合 Testing Library、MSW 和 Playwright 建立单元、组件、契约模拟和
  端到端测试。

已验证的基础版本为 Node.js 24.18.0、pnpm 11.17.0、Vite+ 0.2.6、TypeScript 7.0.2、
React 19.2.8、React Router 7.18.1、MUI 7.3.11、KLineChart 10.0.0 与 ECharts 6.1.0。
真实 OpenAPI client、WebSocket 与领域页面不在本次基础实现范围内。

## 后果

- 团队只需维护一套 `vp` 工作流，本地与 CI 的检查入口一致。
- Vite+ 尚未到 1.0，需要固定版本并验证每次升级。若回归阻塞发布，可恢复独立
  Vite/Vitest/Oxlint/Oxfmt 命令；标准 React/Vite 业务代码无需迁移。
- TypeScript 7.0 没有稳定编程 API。若单个开发工具仍依赖该 API，按官方建议只为工具并装
  `@typescript/typescript6`，应用类型检查继续使用 TypeScript 7。
- SPA 不提供 SSR/SEO；若以后出现公开、可索引页面，需新 ADR 评估 Framework Mode 或独立站点。
- 双图表引擎会增加异步 chunk、主题映射、生命周期、专项回归和版本升级成本；应用外壳不得
  静态导入引擎，同页双图按面板可见性加载。
- KLineChart 使用 DataLoader 接入历史与实时 Candle；ECharts 使用按需模块、dataset 和纯
  option builder。高频实例状态留在引擎内，不逐帧写 React 或 Zustand。
- 图表数据必须经 canonical DTO 规范化，decimal string 只在渲染边界转 number；KLineChart
  指标/overlay 和 ECharts formatter 均由本地白名单代码创建，不执行远端配置。
- OpenAPI、错误模型、decimal、市场时区、分页和实时消息 schema 仍需进入 `docs/contracts/`。

## 替代关系

无。
