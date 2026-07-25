# 0001：服务目录与技术方案文档布局

- 状态：Accepted
- 日期：2026-07-25
- 决策者：项目维护者

## 背景

三个服务需要在仓库根目录保持可见，同时技术方案需要集中管理，避免服务代码目录与设计文档混放。此前的 `data-sync/`、`backend/`、`frontend/` 目录名称缺少统一视觉标识，且服务目录为空，无法被 Git 追踪。

## 候选方案

1. 保持 `data-sync/`、`backend/`、`frontend/` 顶层目录，并将服务文档留在各服务内部。
2. 以 `services/` 包裹三个服务，再在每个服务内部维护文档。
3. 使用统一 `service-*` 前缀保留三个服务在根目录，并将技术方案集中到根目录 `docs/`。

## 决策

采用方案 3。

- 服务实现目录为 `service-data-sync/`、`service-api/`、`service-web/`。
- 服务技术方案分别位于 `docs/service-data-sync/`、`docs/service-api/`、`docs/service-web/`。
- 全局架构、跨服务契约与 ADR 分别位于 `docs/architecture/`、`docs/contracts/` 与 `docs/decisions/`。

## 后果

- 根目录可直观看到三个具有相同前缀的服务，无额外嵌套层级。
- 服务代码与技术方案分离；服务 README 只保留实现与运行入口。
- 原有空目录 `data-sync/`、`backend/`、`frontend/` 被替换；任何后续脚本、Compose 或 CI 必须使用新路径。

## 替代关系

无。
