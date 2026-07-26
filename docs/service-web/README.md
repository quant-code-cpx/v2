# Web 服务技术方案

服务实现目录：[service-web/](../../service-web/)。

本目录记录用户流程、页面信息架构、前后端契约消费、状态管理与访问控制方案。
前端基线已实现：Vite+、pnpm、React 19、React Router 7、MUI 7、TypeScript 7、TanStack Query、
KLineChart 10 与 ECharts 6。当前页面使用受控 fixture；真实 API、认证与实时行情待跨服务契约。

## 本地命令

在 `service-web/` 执行：

- 安装：`vp install --frozen-lockfile`
- 开发：`vp dev`
- 格式化、lint、类型检查：`vp check`
- 单元测试：`vp test`
- 构建：`vp build`
- E2E：先 `vp build`，再 `vp run e2e`（首次需 `vp exec playwright install chromium`）
- 容器构建：`docker build --tag quant-v2/service-web:local service-web`
- 容器健康检查：`docker run --rm -p 15173:8080 quant-v2/service-web:local`，访问 `http://127.0.0.1:15173/healthz`

环境变量见 [service-web/.env.example](../../service-web/.env.example)。仅允许公开的 `VITE_*` 配置。

## 方案索引

- [0001：Web 前端工程基础与双图表实现方案](0001-frontend-foundation/index.html) — Accepted（基础实现完成）
- [0002：Minimal 风格设计系统与 MUI 落地方案](0002-minimal-inspired-design-system/index.html) — Proposed
