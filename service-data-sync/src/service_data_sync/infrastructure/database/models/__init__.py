"""当前 PostgreSQL 目标 schema 的可读 ORM 模型入口。"""

from service_data_sync.infrastructure.database.models.base import Base
from service_data_sync.infrastructure.database.models.registry import ALL_MODELS

__all__ = ["ALL_MODELS", "Base"]
