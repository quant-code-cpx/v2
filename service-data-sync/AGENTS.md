# AGENTS.md

## 适用范围

本文件适用于 `service-data-sync/`。同时继承仓库根目录 `AGENTS.md`；冲突时以本文件为准。

## 架构边界

- 所有外部数据必须通过 provider-neutral port 与独立 adapter 获取；任务、应用、领域、质量和持久化代码
  禁止直接调用数据源 SDK、HTTP 接口或具体 adapter。
- 供应商 SDK、HTTP 地址和字段只能出现在对应 adapter 实现中；adapter 禁止直接写 canonical 数据库。
- 数据同步任务必须可重复执行、可恢复，并明确数据源、市场、时区、交易日历和幂等策略。

## 测试目录与归属

- 本服务作为独立 Python 功能模块，测试统一进入服务级 `tests/`，并按 `unit/`、`integration/`、
  `architecture/` 分类。
- 禁止在 `src/service_data_sync/` 的生产源码目录旁散落 `test_*.py`。
- 新增更细功能域时，在上述分类下建立同名功能子目录。

## 数据口径注释

数据同步代码遇到供应商字段映射、单位换算、时区转换、复权处理、口径选择、异常值修复、降级或数据源
切换时，必须说明原始口径、目标口径、采用条件和不可混用的边界。

## 验证要求

仅使用 Docker；宿主机不要求安装 Python 或 uv。以下命令在仓库根目录执行：

- 测试镜像：`docker build --target test --tag quant-v2/service-data-sync:test service-data-sync`
- 格式检查与静态检查：以测试镜像分别运行 `ruff format --check .`、`ruff check .`、`pyright`
- 单元测试：以测试镜像运行 `pytest -m "not integration"`
- 架构测试：以测试镜像运行 `pytest tests/architecture --no-cov`
- 集成测试：先运行 `docker compose -f compose.yaml -f compose.dev.yaml --env-file .env.example
  --profile data-sync-infra up -d`，再运行 `docker compose -f compose.yaml -f compose.dev.yaml
  --env-file .env.example --profile data-sync-infra --profile data-sync-test run --rm data-sync-test`
- 镜像构建：`docker build --tag quant-v2/service-data-sync:local service-data-sync`
- 基础设施诊断：`docker compose -f compose.yaml -f compose.dev.yaml --env-file .env.example
  --profile data-sync-infra --profile data-sync-worker run --rm --no-deps data-sync-worker
  data-sync-diagnostics --format json`

完成任务前，至少运行受影响范围内已存在的格式化、检查和测试，并报告未运行项目及原因。
