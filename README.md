# quant-v2

量化数据与应用平台。项目处于架构规划阶段，具体技术栈尚未确定。

## 计划结构

```text
quant-v2/
├── data-sync/   # Python 财经与股票基础数据同步服务
├── backend/     # 后端 API 服务
├── frontend/    # 前端应用
├── docs/        # 架构与决策记录
└── .github/     # GitHub 协作配置
```

三个服务应能独立构建、测试、运行和部署；本地开发计划由根目录 Docker Compose 统一管理。

## 当前状态

- Git 仓库与基础忽略规则已建立。
- 服务目录已预留，但尚未初始化框架。
- Compose、CI、数据库和服务依赖将在技术选型后补充。

## 文档

- [代理与仓库规则](AGENTS.md)
- [参与开发](CONTRIBUTING.md)
- [架构边界](docs/architecture.md)
- [架构决策记录](docs/decisions/README.md)

## 待决策

- 后端与前端技术栈
- 数据库、缓存和消息系统
- 服务间通信方式与 API 契约
- 数据源、同步频率和数据质量策略
- 本地、测试与生产环境的部署模型
- 开源许可证

这些决定应使用 ADR 记录，避免重要取舍只存在于聊天或提交信息中。

