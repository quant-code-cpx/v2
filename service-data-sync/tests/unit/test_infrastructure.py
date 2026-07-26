from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from service_data_sync.bootstrap import container as container_module
from service_data_sync.bootstrap.errors import DependencyUnavailable
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.database import connection as database_module
from service_data_sync.infrastructure.messaging import redis_client as redis_module
from service_data_sync.infrastructure.object_storage import client as storage_module


def test_database_client_builds_pings_and_closes(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build PostgreSQL wrapper and translate driver probe failure."""
    engine = MagicMock()
    # Return fixture engine so wrapper behavior stays independent from real PostgreSQL.
    monkeypatch.setattr(database_module, "create_engine", lambda *_args, **_kwargs: engine)
    client = database_module.DatabaseClient.from_settings(load_settings())

    client.ping()
    client.close()

    engine.connect.assert_called_once()
    engine.dispose.assert_called_once()

    engine.connect.side_effect = SQLAlchemyError("offline")
    with pytest.raises(DependencyUnavailable, match="postgres unavailable"):
        client.ping()


def test_redis_client_builds_pings_and_closes(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build Redis wrapper and translate driver probe failure."""
    backend = MagicMock()
    # Return fixture backend so wrapper behavior stays independent from real Redis.
    monkeypatch.setattr(redis_module.redis.Redis, "from_url", lambda *_args, **_kwargs: backend)
    client = redis_module.RedisClient.from_settings(load_settings())

    client.ping()
    client.close()

    backend.close.assert_called_once()
    backend.ping.side_effect = RedisError("offline")
    with pytest.raises(DependencyUnavailable, match="redis unavailable"):
        client.ping()


def test_object_storage_client_builds_pings_and_closes(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build S3 wrapper and translate object-store probe failure."""
    backend = MagicMock()
    # Return fixture backend so wrapper behavior stays independent from real object storage.
    monkeypatch.setattr(storage_module.boto3, "client", lambda *_args, **_kwargs: backend)
    client = storage_module.ObjectStorageClient.from_settings(load_settings())

    client.ping()
    client.close()

    backend.head_bucket.assert_called_once_with(Bucket="quant-data-sync-test")
    backend.close.assert_called_once()
    backend.head_bucket.side_effect = ClientError({"Error": {"Code": "403"}}, "HeadBucket")
    with pytest.raises(DependencyUnavailable, match="s3 unavailable"):
        client.ping()


def test_container_composes_dependencies_without_registering_provider_adapters(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose only infrastructure clients and leave provider registry intentionally empty."""
    database = MagicMock()
    broker = MagicMock()
    object_storage = MagicMock()
    monkeypatch.setattr(
        container_module.DatabaseClient,
        "from_settings",
        # Inject fixture database instead of opening an external connection.
        classmethod(lambda _cls, _settings: database),
    )
    monkeypatch.setattr(
        container_module.RedisClient,
        "from_settings",
        # Inject fixture broker instead of opening an external connection.
        classmethod(lambda _cls, _settings: broker),
    )
    monkeypatch.setattr(
        container_module.ObjectStorageClient,
        "from_settings",
        # Inject fixture object storage instead of opening an external connection.
        classmethod(lambda _cls, _settings: object_storage),
    )

    container = container_module.build_container(load_settings())
    container.close()

    assert container.source_registry.provider_ids() == frozenset()
    database.close.assert_called_once()
    broker.close.assert_called_once()
    object_storage.close.assert_called_once()
