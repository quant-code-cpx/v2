"""所有同步服务数据库模型共享的 Declarative 基类。"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """表达当前目标 schema；历史 Alembic revision 仍是数据库演进唯一入口。"""

    metadata = MetaData()
