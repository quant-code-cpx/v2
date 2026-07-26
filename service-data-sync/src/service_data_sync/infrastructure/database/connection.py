from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from service_data_sync.bootstrap.errors import DependencyUnavailable
from service_data_sync.bootstrap.settings import Settings


@dataclass
class DatabaseClient:
    engine: Engine

    @classmethod
    def from_settings(cls, settings: Settings) -> DatabaseClient:
        """Create resilient PostgreSQL engine from validated secret connection URL."""
        return cls(
            engine=create_engine(
                settings.database_url.get_secret_value(),
                connect_args={"connect_timeout": 5},
                pool_pre_ping=True,
                pool_recycle=1800,
            )
        )

    def ping(self) -> None:
        """Execute minimal query and convert SQL driver failure to domain error."""
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as error:
            raise DependencyUnavailable("postgres", "ping") from error

    def close(self) -> None:
        """Dispose all database-engine pools during shutdown."""
        self.engine.dispose()
