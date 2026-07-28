"""数据同步服务的环境变量配置模型与输入校验。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_data_sync.bootstrap.errors import ConfigurationError


class Environment(StrEnum):
    """服务运行环境的受限枚举。"""

    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    """日志输出格式的受限枚举。"""

    CONSOLE = "console"
    JSON = "json"


class Settings(BaseSettings):
    """从环境变量加载并校验的服务配置，不在日志中暴露密钥。"""

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
    akshare_enabled: bool = Field(default=False, validation_alias="DATA_SYNC_AKSHARE_ENABLED")
    equity_market_enabled: bool = Field(
        default=False,
        validation_alias="DATA_SYNC_EQUITY_MARKET_ENABLED",
    )
    equity_scheduler_enabled: bool = Field(
        default=False,
        validation_alias="DATA_SYNC_EQUITY_SCHEDULER_ENABLED",
    )
    sector_enabled: bool = Field(default=False, validation_alias="DATA_SYNC_SECTOR_ENABLED")
    sector_membership_enabled: bool = Field(
        default=False,
        validation_alias="DATA_SYNC_SECTOR_MEMBERSHIP_ENABLED",
    )
    sector_eod_enabled: bool = Field(
        default=False,
        validation_alias="DATA_SYNC_SECTOR_EOD_ENABLED",
    )
    sector_eod_publish_enabled: bool = Field(
        default=False,
        validation_alias="DATA_SYNC_SECTOR_EOD_PUBLISH_ENABLED",
    )
    sector_eod_scheduler_enabled: bool = Field(
        default=False,
        validation_alias="DATA_SYNC_SECTOR_EOD_SCHEDULER_ENABLED",
    )
    sw_sector_enabled: bool = Field(
        default=False,
        validation_alias="DATA_SYNC_SW_SECTOR_ENABLED",
    )
    financial_enabled: bool = Field(
        default=False,
        validation_alias="DATA_SYNC_FINANCIAL_ENABLED",
    )
    money_flow_enabled: bool = Field(
        default=False,
        validation_alias="DATA_SYNC_MONEY_FLOW_ENABLED",
    )
    financial_source_policy: str = Field(
        default="disabled",
        validation_alias="DATA_SYNC_FINANCIAL_SOURCE_POLICY",
    )
    financial_max_concurrency: int | None = Field(
        default=None,
        ge=1,
        validation_alias="DATA_SYNC_FINANCIAL_MAX_CONCURRENCY",
    )
    financial_requests_per_minute: int | None = Field(
        default=None,
        ge=1,
        validation_alias="DATA_SYNC_FINANCIAL_REQUESTS_PER_MINUTE",
    )
    financial_request_timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        validation_alias="DATA_SYNC_FINANCIAL_REQUEST_TIMEOUT_SECONDS",
    )
    trading_calendar_enabled: bool = Field(
        default=False,
        validation_alias="DATA_SYNC_TRADING_CALENDAR_ENABLED",
    )
    internal_api_bearer_token: SecretStr = Field(
        validation_alias="DATA_SYNC_INTERNAL_API_BEARER_TOKEN"
    )
    internal_api_host: str = Field(
        default="0.0.0.0", validation_alias="DATA_SYNC_INTERNAL_API_HOST"
    )
    internal_api_port: int = Field(
        default=8000, ge=1, le=65535, validation_alias="DATA_SYNC_INTERNAL_API_PORT"
    )
    akshare_request_timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=120,
        validation_alias="DATA_SYNC_AKSHARE_REQUEST_TIMEOUT_SECONDS",
    )

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        """规范化日志级别，并限制为 Python 标准日志级别。"""
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("must be a standard Python log level")
        return normalized

    @field_validator("s3_endpoint_url")
    @classmethod
    def validate_s3_endpoint_url(cls, value: str) -> str:
        """要求对象存储端点使用 HTTP(S)，并移除末尾分隔符。"""
        if not value.startswith(("http://", "https://")):
            raise ValueError("must use http or https")
        return value.rstrip("/")

    @field_validator("s3_bucket")
    @classmethod
    def validate_s3_bucket(cls, value: str) -> str:
        """去除桶名称首尾空白，并拒绝空对象存储目标。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("financial_source_policy")
    @classmethod
    def validate_financial_source_policy(cls, value: str) -> str:
        """规范化财务来源策略标识；它必须是可审计配置名而非空白开关。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("financial source policy must not be blank")
        return "disabled" if normalized.lower() == "disabled" else normalized

    @field_validator("internal_api_bearer_token")
    @classmethod
    def validate_internal_api_bearer_token(cls, value: SecretStr) -> SecretStr:
        """要求内部服务凭据非空且达到最小长度，避免误用开发占位符。"""
        if len(value.get_secret_value().strip()) < 32:
            raise ValueError("must contain at least 32 characters")
        return value

    @model_validator(mode="after")
    def validate_financial_dark_launch_settings(self) -> Settings:
        """启用财务能力前强制声明来源策略和资源预算，缺项时拒绝启动。"""
        if self.equity_scheduler_enabled and not self.equity_market_enabled:
            raise ValueError(
                "DATA_SYNC_EQUITY_SCHEDULER_ENABLED requires DATA_SYNC_EQUITY_MARKET_ENABLED"
            )
        if self.sw_sector_enabled and (not self.akshare_enabled or not self.sector_enabled):
            raise ValueError(
                "DATA_SYNC_SW_SECTOR_ENABLED requires DATA_SYNC_AKSHARE_ENABLED "
                "and DATA_SYNC_SECTOR_ENABLED"
            )
        if self.money_flow_enabled and not self.akshare_enabled:
            raise ValueError("DATA_SYNC_MONEY_FLOW_ENABLED requires DATA_SYNC_AKSHARE_ENABLED")
        if not self.financial_enabled:
            return self
        missing: list[str] = []
        if self.financial_source_policy == "disabled":
            missing.append("DATA_SYNC_FINANCIAL_SOURCE_POLICY")
        if self.financial_max_concurrency is None:
            missing.append("DATA_SYNC_FINANCIAL_MAX_CONCURRENCY")
        if self.financial_requests_per_minute is None:
            missing.append("DATA_SYNC_FINANCIAL_REQUESTS_PER_MINUTE")
        if self.financial_request_timeout_seconds is None:
            missing.append("DATA_SYNC_FINANCIAL_REQUEST_TIMEOUT_SECONDS")
        if missing:
            raise ValueError(f"financial dark launch requires: {', '.join(missing)}")
        return self


def load_settings() -> Settings:
    """加载已校验配置，同时向调用方隐藏详细校验值。"""
    try:
        return Settings()  # type: ignore[call-arg]  # 值由 Pydantic 配置源加载。
    except ValidationError as error:
        raise ConfigurationError("invalid service-data-sync configuration") from error
