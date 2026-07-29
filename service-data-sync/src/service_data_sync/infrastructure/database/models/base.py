"""所有同步服务 ORM 表模型共享的 SQLAlchemy `Declarative` 基类。"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """汇集当前目标 schema 的 metadata，不承担运行时建表或迁移职责。

    每个模型继承本类后才会登记进同一份 `metadata`，供 Alembic 和架构测试核对表、列、索引
    与约束。历史数据库版本必须按 Alembic revision 演进；直接调用 `metadata.create_all()` 会
    跳过可审计迁移、回滚路径和生产部署顺序，因此不属于本服务的运行方式。
    """

    metadata = MetaData()
