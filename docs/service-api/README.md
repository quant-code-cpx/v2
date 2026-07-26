# API 服务技术方案

服务实现目录：[service-api/](../../service-api/)。

本目录记录 API 风格、认证授权、领域边界、查询策略、错误模型与版本策略。

## 方案索引

- [0001：API 服务基础架构与技术方案](0001-service-api-foundation/index.html) — Implemented

当前提议采用 Node.js 24 LTS + NestJS 11 单一 API 进程，只保留 UserModule、AuthModule，
以及为鉴权限流与短期安全状态服务的 RedisModule。
最终技术决策见 [ADR-0005](../decisions/0005-service-api-runtime-and-architecture.md)。
