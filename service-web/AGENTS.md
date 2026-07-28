@../.agents/skills/react-best-practices/SKILL.md
@../.agents/skills/composition-patterns/SKILL.md
@../.agents/skills/material-ui-styling/SKILL.md
@../.agents/skills/material-ui-theming/SKILL.md

# AGENTS.md

## 适用范围

本文件适用于 `service-web/`。同时继承仓库根目录 `AGENTS.md`；冲突时以本文件为准。

## 测试目录与归属

- 路由页测试统一放在 `src/views/<PageName>/test/`，覆盖该页面私有组件、Hook、工具函数和页面编排。
- 共享组件、API、Hook、工具或样式测试放在对应功能目录的 `test/`。
- `service-web` 调用 `service-api` 时只能发送 `POST`；HTTP method 由共享传输层固定，业务 API
  调用方不得自行选择 method。`service-web` 自身的 `/healthz` 不属于该约束。

## 验证要求

以下命令在 `service-web/` 目录执行：

- 安装：`vp install --frozen-lockfile`
- 开发：`vp dev`
- 格式化、lint、类型检查：`vp check`
- 单元测试：`vp test`
- 生产构建：`vp build`
- E2E：先运行 `vp exec playwright install chromium`，再运行 `vp build && vp run e2e`
- 容器构建：`docker build --tag quant-v2/service-web:local .`
- 容器健康检查：运行镜像并请求 `GET /healthz`

## React 与 MUI 开发规范

- 当前运行时以 `package.json` 锁定的 React 19 与 MUI 7 为准。仓库内 MUI 官方 Skill 面向更新主版本时，
  只采用不依赖版本的分层与 Token 原则；组件 API、slot、类型和导入方式必须以当前 MUI 7 类型检查及
  官方文档为准。
- 每个路由页必须使用 `src/views/<PageName>/<PageName>.tsx` 独立目录。页面私有组件放在
  `src/views/<PageName>/components/<ComponentName>.tsx`，页面私有 Hook 放在 `hooks/use*.ts`，纯函数
  放在 `utils/*.ts`。禁止在 `views/` 根目录平铺页面文件。
- 只有跨页面复用的组件才能进入 `src/components/<ComponentName>.tsx`。单文件组件不得额外套同名目录；
  只有组件同时拥有两个及以上强关联文件，例如私有 Hook、类型、样式或子组件时，才建立
  `components/<ComponentName>/` 功能目录。禁止用 `index.ts` 聚合导出掩盖真实依赖；路由懒加载和组件
  引用必须直达具体文件，保持 Bundle 路径可静态分析。
- 路由页只负责信息顺序、页面级状态分支和组件组合。远程状态归 TanStack Query，可分享状态归 URL，
  局部交互归组件或页面 Hook，高频图表状态归引擎实例。API 调用、表单副作用、复杂派生和可测试纯函数
  不得混入大块 JSX。
- 不以行数机械拆分，但生产 UI 组件或 Hook 超过约 200 行时必须检查职责；页面编排超过约 150 行时
  必须优先拆出私有组件、Hook 或纯函数。配置、契约适配、测试等保持单一职责的文件可例外，并在评审中
  说明原因。
- 组件变体优先使用组合与显式命名，禁止持续增加布尔属性制造隐式模式。禁止在组件内部定义 React
  组件，避免父组件重渲染导致子树卸载重建。
- 组件拆分必须至少具备一项真实边界：多处复用、独立状态或副作用、明确业务语义、昂贵渲染隔离、
  独立可测试契约。单次使用、无状态、只有少量 JSX 的片段默认保留在父组件或同一模块内；禁止为了降低
  文件行数制造转发大量 props 的微型组件。模块级私有辅助组件可以与主组件放在同一文件。
- MUI 样式按最小作用域选择：单实例使用 `sx`，重复封装使用 `styled()`，全局一致行为进入
  `theme.components`，HTML 基线才使用 `CssBaseline` 或全局 CSS。超过约 100 行的复杂样式移出组件。
- 颜色、间距、圆角、阴影、组件几何必须来自 canonical design tokens 或 theme。不得用页面局部常量
  复制全局设计值；MUI `sx` 使用主题缩写，`styled()` 中显式使用 `theme.spacing()`。
- 重型图表与路由保持动态加载；避免聚合导入、组件内组件、无必要 Effect 和派生状态回写。拆分后必须
  保留 loading、empty、error/retry、权限、键盘焦点和 reduced-motion 状态，并补受影响行为测试。

## 产品范围

产品范围仅限 PC 桌面浏览器，默认设计与视觉验收视口为 `1440×900`。禁止为移动端、平板、触控设备或
窄屏窗口设计、实现、生成原型、截图或测试；禁止新增只服务于这些场景的断点、媒体查询、布局重排、
移动导航、手势、虚拟键盘处理或触控目标。前端方案不得把 responsive/mobile/tablet/touch 列为需求、
验收项、兼容目标或待决问题。桌面端仍须满足键盘操作、语义化、ARIA、焦点可见、对比度和 reduced-motion。
既有移动端设计与图片只视为历史产物，不维护、不重绘、不回归，也不构成验收条件；与本规则冲突时以本文件
为准。任何单项设计或实现任务不得临时放宽此限制；如需改变产品范围，必须先明确修改本条仓库规则。

## 前端状态与图表

- KLineChart 仅承载 K 线、指标和 overlay；ECharts 仅承载非 K 线分析图。
- 服务端状态只进 TanStack Query，图表高频交互状态留在引擎实例。
- 未冻结的 API 或实时契约只能用 fixture 或 MSW，禁止猜测 endpoint。
