@./.agents/skills/caveman/SKILL.md
@./.agents/skills/rtk/SKILL.md
@./.agents/skills/react-best-practices/SKILL.md
@./.agents/skills/composition-patterns/SKILL.md
@./.agents/skills/material-ui-styling/SKILL.md
@./.agents/skills/material-ui-theming/SKILL.md

# AGENTS.md

## 项目目标

`quant-v2` 是由三个独立服务组成的量化数据与应用平台：

- `service-data-sync/`：同步财经与股票基础数据，计划使用 Python。
- `service-api/`：对外提供业务 API，技术栈待定。
- `service-web/`：用户界面，技术栈待定。

## 适用范围

本文件适用于整个仓库。若子目录以后包含自己的 `AGENTS.md`，以离待修改文件最近的规则为准。

## 架构边界

- 三个服务应能独立构建、测试、运行和部署。
- 服务之间通过明确、可版本化的 API 或消息契约交互。
- 不要跨服务直接导入源码，也不要让前端直接访问数据库或第三方数据源。
- `service-data-sync` 的所有外部数据必须通过 provider-neutral port 与独立 adapter 获取；任务、应用、
  领域、质量和持久化代码禁止直接调用数据源 SDK、HTTP 接口或具体 adapter。
- 供应商 SDK、HTTP 地址和字段只能出现在 `service-data-sync` 的对应 adapter 实现中；adapter 禁止直接写 canonical 数据库。
- 根目录只放服务目录、跨服务配置、编排、文档和仓库治理文件。
- 影响多个服务的设计变更，应先在 `docs/decisions/` 新建 ADR。

## 文档归属

- 每个服务的技术方案放在根目录 `docs/service-*/`。
- 全局架构放在 `docs/architecture/`，跨服务契约放在 `docs/contracts/`，架构决策放在 `docs/decisions/`。

## 数据与配置

- 配置通过环境变量注入；新增变量时同步更新 `.env.example`。
- 不得提交密钥、令牌、真实账号、生产配置、数据库文件或大体积行情数据。
- 数据同步任务应可重复执行、可恢复，并明确数据源、市场、时区、交易日历和幂等策略。
- 数据库结构变更必须可迁移、可回滚；禁止依赖人工直接修改生产数据库。

## 修改原则

- 先阅读目标目录的 README、配置和最近的 `AGENTS.md`。
- 修改保持小而聚焦，不顺手重构无关代码。
- 不猜测未决定的技术栈；需要选择时记录候选方案、约束和取舍。
- 新增行为必须配套测试；修复缺陷应优先添加回归测试。
- 修改接口、配置、部署或架构时，同步更新相关文档。
- 不提交生成物、缓存、本地数据或仅适用于个人机器的配置。

## 测试目录与归属

- 测试文件必须进入最近功能模块的专用测试目录，禁止与被测试源码同层平铺。使用 `.test.*`、
  `.spec.*`、`test_*.py` 等后缀不能替代目录隔离；禁止出现 `service.ts` 与 `service.spec.ts`
  位于同一目录的结构。
- `service-web` 的路由页测试统一放在 `src/views/<PageName>/test/`，覆盖该页面私有组件、Hook、
  工具函数和页面编排；共享组件、API、Hook、工具或样式测试放在对应功能目录的 `test/`。
- `service-api` 的每个 Nest module 使用 `src/modules/<ModuleName>/test/`，该 module 的
  Controller、Service、DTO、Guard 等测试全部集中于此。`platform/` 与 `scripts/` 下的独立功能也在
  自己目录内建立 `test/`。
- `service-data-sync` 整体作为独立 Python 功能模块，测试统一进入服务级 `tests/`，并按 `unit/`、
  `integration/`、`architecture/` 分类；禁止在 `src/service_data_sync/` 的生产源码目录旁散落
  `test_*.py`。新增更细功能域时，在上述分类下建立同名功能子目录。
- 只有确实跨越多个功能模块的 E2E、集成、架构或契约测试，才可进入服务级专用测试树；不得借此把普通
  单元测试重新集中为无归属的平铺目录。
- 移动测试时必须同步修正 import、fixture、测试发现、覆盖率、静态检查与 Docker 构建路径，并运行
  对应服务的完整测试命令。

## 注释与可读性

### 注释语言

- 项目自行维护的代码注释必须使用简体中文，包括 Python docstring、JSDoc、普通行内注释、
  SQL 注释以及 `TODO`、`FIXME` 的说明文字；禁止新增只有英文叙述的注释。
- 类名、函数名、字段名、协议名、标准缩写和难以准确翻译的技术术语应保留原文，并使用反引号标识；
  注释主体仍须使用中文说明其含义。例如：`data_version` 表示一次可见数据发布的稳定版本。
- 第三方源码、自动生成文件、许可证文本、工具指令、lint 抑制指令和外部协议要求的固定文本可以保留英文。
  自动生成文件需要改变注释时，应优先修改生成模板或生成配置，不直接修改生成结果。
- 修改既有代码时，受影响范围内的英文叙述性注释必须同步改为中文；不得继续复制、扩散不符合本规则的旧注释。

### 定义级注释

- 每个函数、方法、构造函数、getter、setter、回调和 React 组件都必须有贴近定义位置的中文注释；
  测试函数、测试辅助函数、fixture、mock、fake 及其回调同样适用。
- Python 优先使用紧随定义的中文 docstring；TypeScript/JavaScript 优先使用定义前的中文 JSDoc；
  无法放置 docstring/JSDoc 的匿名回调，应在其赋值或传入位置之前添加中文注释。
- 具有业务语义的模块、类、接口、类型、枚举、DTO 和数据表也应添加中文注释，说明它在系统中的职责；
  对单位、币种、时区、复权口径、数据来源或空值语义不直观的字段，必须就近说明。
- 定义级注释应按实际需要说明职责、关键输入与输出、可观察副作用、失败方式及重要约束；
  不要求机械罗列所有项目，也不得只复述名称、参数类型或返回类型。

### 行内注释

- 重要、分支较多或不易直观理解的逻辑必须补充中文行内注释，重点解释业务原因、设计取舍、
  关键不变量、顺序要求、幂等边界、并发假设和异常处理策略，而非逐行翻译代码。
- 数据同步相关代码遇到供应商字段映射、单位换算、时区转换、复权处理、口径选择、异常值修复、
  降级或数据源切换时，必须说明原始口径、目标口径、采用条件和不可混用的边界。
- 涉及安全、权限、资金、缓存一致性、数据版本、迁移回滚或外部副作用的逻辑，必须说明“为什么这样做”
  以及违反约束可能造成的结果。
- 不为显而易见的赋值、循环、条件判断添加无信息量注释；注释应帮助维护者理解代码本身无法表达的内容。

### 维护与验收

- 注释是实现的一部分。修改行为、数据口径、参数、返回值或副作用时，必须同步更新相关注释；
  过期、矛盾或可能误导维护者的注释视为缺陷。
- 修改既有代码时，应补齐受影响定义和复杂逻辑缺失的注释，但不要借机批量改动无关文件。
- 完成任务前必须人工检查本次 diff：确认新增和修改的定义均有合格中文注释、复杂分支有行内说明、
  未新增英文叙述性注释。格式化、lint、类型检查和测试通过不能替代该检查。

## 验证要求

各服务尚未确定标准命令。引入技术栈时，应在对应服务 README 和本文件中补充：

- 安装与启动命令
- 格式化与静态检查命令
- 单元、集成和端到端测试命令
- Docker 构建与健康检查命令

### service-data-sync

仅使用 Docker；宿主机不要求安装 Python 或 uv。在仓库根目录执行：

- 测试镜像：docker build --target test --tag quant-v2/service-data-sync:test service-data-sync
- 格式检查与静态检查：以测试镜像分别运行 ruff format --check .、ruff check .、pyright。
- 单元测试：以测试镜像运行 pytest -m "not integration"；架构测试运行 pytest tests/architecture --no-cov。
- 集成测试：先运行 docker compose -f compose.yaml -f compose.dev.yaml --env-file .env.example
  --profile data-sync-infra up -d，再运行 docker compose -f compose.yaml -f compose.dev.yaml
  --env-file .env.example --profile data-sync-infra --profile data-sync-test run --rm data-sync-test。
- 镜像构建：docker build --tag quant-v2/service-data-sync:local service-data-sync。
- 基础设施诊断：docker compose -f compose.yaml -f compose.dev.yaml --env-file .env.example
  --profile data-sync-infra --profile data-sync-worker run --rm --no-deps data-sync-worker
  data-sync-diagnostics --format json。

完成任务前，至少运行受影响范围内已存在的格式化、检查和测试，并报告未运行项目及原因。

### service-api

使用 Node.js 24 与 pnpm 11；宿主机 Node 版本不满足时，全部命令通过 Docker 运行。在 `service-api/` 目录执行：

- 安装：`corepack pnpm@11.17.0 install`
- 开发：`pnpm start:dev`
- 格式化与静态检查：`pnpm format:check`、`pnpm lint`、`pnpm typecheck`
- 单元测试：`pnpm test`
- 构建：`pnpm build`
- 迁移：`pnpm prisma:deploy`；生产应用启动不得自动迁移。
- 容器测试：`docker build --target test --tag quant-v2/service-api:test service-api`
- 容器构建：`docker build --tag quant-v2/service-api:local service-api`
- 本地完整启动：`docker compose -f compose.yaml -f compose.dev.yaml --env-file .env.example --profile api up --build`
- 容器健康检查：请求 `GET /health` 和 `GET /ready`。

`AuthModule` 只能单向调用 `UserModule` 与 `RedisModule`；禁止 `forwardRef()`。Redis 只保存短期鉴权安全状态，
不得保存用户、凭证或会话权威数据。任何用户禁用、改密、角色变化必须递增 `securityVersion`。

完成任务前，至少运行受影响范围内可执行的格式化、静态检查、单元测试与容器构建，并报告未运行项目及原因。

### service-web

在 `service-web/` 目录执行：

- 安装：`vp install --frozen-lockfile`
- 开发：`vp dev`
- 格式化、lint、类型检查：`vp check`
- 单元测试：`vp test`
- 生产构建：`vp build`
- E2E：先 `vp exec playwright install chromium`，再 `vp build && vp run e2e`
- 容器构建：`docker build --tag quant-v2/service-web:local service-web`
- 容器健康检查：运行镜像并请求 `GET /healthz`

React 与 MUI 开发规范：

- 当前运行时以 `service-web/package.json` 锁定的 React 19 与 MUI 7 为准。仓库内 MUI 官方 Skill
  面向更新主版本时，只采用不依赖版本的分层与 Token 原则；组件 API、slot、类型和导入方式必须以
  当前 MUI 7 类型检查及官方文档为准。
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

产品范围仅限 PC 桌面浏览器，默认设计与视觉验收视口为 `1440×900`。禁止为移动端、平板、触控设备或
窄屏窗口设计、实现、生成原型、截图或测试；禁止新增只服务于这些场景的断点、媒体查询、布局重排、
移动导航、手势、虚拟键盘处理或触控目标。前端方案不得把 responsive/mobile/tablet/touch 列为需求、
验收项、兼容目标或待决问题。桌面端仍须满足键盘操作、语义化、ARIA、焦点可见、对比度和 reduced-motion。
既有移动端设计与图片只视为历史产物，不维护、不重绘、不回归，也不构成验收条件；与本规则冲突时以本规则为准。
任何单项设计或实现任务不得临时放宽此限制；如需改变产品范围，必须先明确修改本条仓库规则。

前端图表规则：KLineChart 仅承载 K 线、指标和 overlay；ECharts 仅承载非 K 线分析图。
服务端状态只进 TanStack Query，图表高频交互状态留在引擎实例；未冻结的 API/实时契约只能用 fixture 或 MSW，禁止猜测 endpoint。

## Git 约定

- 使用小而独立的提交，建议采用 Conventional Commits，例如 `feat:`、`fix:`、`docs:`、`chore:`。
- 不改写或删除用户已有改动。
- 未经明确要求，不推送分支、不创建 PR、不重写共享历史。
- 提交前检查暂存内容，确认没有密钥、本地数据和无关文件。
