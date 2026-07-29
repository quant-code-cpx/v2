"""当前 PostgreSQL 目标 schema 的可读 ORM 模型入口。

导入 `Base` 与 `ALL_MODELS` 会登记全部逻辑表，供 Alembic 自动比对和架构测试使用；这只是
Python 到既有数据库表的映射，不会创建、迁移或删除任何表。消费者查询应经仓储，而非直接
依赖这里的 ORM 类绕过发布、质量和双时态边界。
"""

from service_data_sync.infrastructure.database.models.base import Base
from service_data_sync.infrastructure.database.models.registry import ALL_MODELS

__all__ = ["ALL_MODELS", "Base"]
