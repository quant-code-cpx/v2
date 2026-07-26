from __future__ import annotations

from enum import StrEnum

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_data_sync.bootstrap.errors import ConfigurationError


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    CONSOLE = "console"
    JSON = "json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    environment: Environment = Field(validation_alias="DATA_SYNC_ENV")
    log_level: str = Field(default="INFO", validation_alias="DATA_SYNC_LOG_LEVEL")
    log_format: LogFormat = Field(
        default=LogFormat.CONSOLE, validation_alias="DATA_SYNC_LOG_FORMAT"
    )
    database_url: SecretStr = Field(validation_alias="DATA_SYNC_DATABASE_URL")
    broker_url: SecretStr = Field(validation_alias="DATA_SYNC_BROKER_URL")
    s3_endpoint_url: str = Field(validation_alias="DATA_SYNC_S3_ENDPOINT_URL")
    s3_access_key: SecretStr = Field(validation_alias="DATA_SYNC_S3_ACCESS_KEY")
    s3_secret_key: SecretStr = Field(validation_alias="DATA_SYNC_S3_SECRET_KEY")
    s3_bucket: str = Field(validation_alias="DATA_SYNC_S3_BUCKET")
    s3_region: str = Field(default="us-east-1", validation_alias="DATA_SYNC_S3_REGION")
    diagnostics_timeout_seconds: int = Field(
        default=10,
        ge=1,
        le=60,
        validation_alias="DATA_SYNC_DIAGNOSTICS_TIMEOUT_SECONDS",
    )

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        """Normalize and constrain configured level to standard Python logging levels."""
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("must be a standard Python log level")
        return normalized

    @field_validator("s3_endpoint_url")
    @classmethod
    def validate_s3_endpoint_url(cls, value: str) -> str:
        """Require HTTP(S) object-storage endpoint and remove trailing separator."""
        if not value.startswith(("http://", "https://")):
            raise ValueError("must use http or https")
        return value.rstrip("/")

    @field_validator("s3_bucket")
    @classmethod
    def validate_s3_bucket(cls, value: str) -> str:
        """Trim bucket input and reject empty object-storage target."""
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


def load_settings() -> Settings:
    """Load validated settings while hiding detailed validation values from callers."""
    try:
        return Settings()  # type: ignore[call-arg]  # Values are loaded through Pydantic settings sources.
    except ValidationError as error:
        raise ConfigurationError("invalid service-data-sync configuration") from error
