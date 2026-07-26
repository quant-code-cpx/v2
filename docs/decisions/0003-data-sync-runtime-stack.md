# 0003：数据同步服务运行时与存储技术栈

- 状态：Proposed
- 日期：2026-07-25
- 决策者：项目维护者

## 背景

同步服务需要同时承载内部控制/查询 API、长耗时和可重试的数据任务、动态定时策略、批量数据处理、幂等入库及来源追踪。AKShare 与 Tushare 均以 Python 生态为主，因此主运行时确定为 Python；仍需决定 Web、持久化、任务、调度和原始数据存储方案。

本 ADR 记录目标技术栈及分阶段采用方式。`0001-data-sync-foundation` 只搭工程骨架和基础设施接线，不实现 API、业务表、迁移 revision、调度流程或真实数据同步。

## 候选方案

1. FastAPI + SQLAlchemy + Celery + PostgreSQL + Redis + S3 兼容对象存储。
   - 优点：类型和 OpenAPI 支持好；任务、重试和队列生态成熟；关系数据、批量文件和运行控制各有明确存储。
   - 缺点：包含多个进程和三类基础设施，需要处理数据库与消息代理之间的一致性。
2. Django + Django ORM + Celery + PostgreSQL。
   - 优点：管理后台和 ORM 完整，数据库定时任务插件成熟。
   - 缺点：本服务不需要面向人的 Django Admin；数据批处理仍需绕过 ORM；框架约束和体量较大。
3. FastAPI + APScheduler 单进程 + PostgreSQL。
   - 优点：依赖少、开发快。
   - 缺点：长任务隔离、水平扩展、崩溃重投和队列治理不足。
4. FastAPI + Temporal。
   - 优点：长流程、重试和恢复语义强。
   - 缺点：初期平台和学习成本过高，当前同步流程尚不需要复杂工作流编排。

## 决策

目标技术栈采用方案 1：

- Python 3.13 作为首个运行时基线；依赖使用 `uv` 锁定。
- FastAPI + Pydantic v2 作为未来内部 API 和配置模型方案；当前不创建 app、route 或 OpenAPI。
- SQLAlchemy 2 + Alembic 作为未来持久化方案；当前只准备连接和工具配置，不创建业务模型、表或迁移 revision。
- PostgreSQL 作为同步服务未来专属权威数据库；当前只提供独立 database、role、连接检查和本地容器。
- Celery 作为未来异步任务执行器，Redis 作为内部 broker 和短期协调层；当前只验证 worker 空载启动与 broker 连接，不注册业务任务或定时计划。
- S3 兼容对象存储作为未来原始数据存储；本地使用 MinIO。当前只验证 bucket 配置和连接，不定义业务对象结构。
- Ruff、Pyright、pytest 和 provider 边界架构测试作为当前质量门禁；Alembic migration check 与 OpenAPI contract test 在对应方案落地后加入。

未来可由同一镜像提供 `api`、`scheduler`、`dispatcher`、`worker` 等独立角色。当前只交付空载 `worker` 与基础设施诊断入口；其他角色由后续编号方案引入。所有部署配置由环境变量或 secret 注入。

## 后果

- 需要 PostgreSQL、Redis 和 S3 兼容对象存储；开发环境由 Docker Compose 编排，生产环境可使用托管服务。
- API、数据模型、调度、可靠投递和幂等语义仍需后续方案明确，当前基建不对这些行为作实现承诺。
- Redis 不得成为未来业务数据或任务权威状态的唯一副本。
- Python 3.14、Temporal、事件平台或 ClickHouse 等升级须由容量、生态兼容性或流程复杂度指标触发，不提前引入。

## 替代关系

无。
