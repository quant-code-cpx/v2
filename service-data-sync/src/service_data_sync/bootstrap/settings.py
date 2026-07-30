"""数据同步服务的环境变量配置模型与输入校验。

它把部署注入的字符串转换为有类型、可审计的运行策略，并在基础设施客户端或 provider
adapter 创建前验证开关依赖、授权策略和资源上限，避免配置错误在同步中途才暴露。
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path

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


class MarketDataLicenseScope(StrEnum):
    """限制市场数据许可状态，配置字符串不能自行创造商业授权。"""

    DISABLED = "disabled"
    PERSONAL_RESEARCH = "personal-research"
    COMMERCIAL_REDISTRIBUTION_APPROVED = "commercial-redistribution-approved"


class StockConnectRawRetentionMode(StrEnum):
    """限制 licensed 互联互通失败证据是否允许持久化原始字节。"""

    MANIFEST_ONLY = "MANIFEST_ONLY"
    LICENSED_RAW_ALLOWED = "LICENSED_RAW_ALLOWED"


class Settings(BaseSettings):
    """从环境变量加载服务运行契约，并在创建基础设施客户端前拒绝危险组合。

    此模型是进程启动时唯一的配置入口。`SecretStr` 让密码、令牌和连接串在
    常规日志或异常展示中自动隐藏；各项功能开关必须与对应来源策略同时满足，
    因而不会因单独打开某个布尔值而意外访问尚未验证的数据源。
    """

    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # 基础运行参数同时决定日志标签和诊断输出，不能由业务任务在运行中改写。
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
    # 下列开关采用“默认拒绝”：来源或下游能力未经过验证时，组合根不会注册 adapter。
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
    index_enabled: bool = Field(default=False, validation_alias="DATA_SYNC_INDEX_ENABLED")
    stock_connect_enabled: bool = Field(
        default=False,
        validation_alias="DATA_SYNC_STOCK_CONNECT_ENABLED",
    )
    tushare_enabled: bool = Field(
        default=False,
        validation_alias="DATA_SYNC_TUSHARE_ENABLED",
    )
    market_overview_enabled: bool = Field(
        default=False,
        validation_alias="DATA_SYNC_MARKET_OVERVIEW_ENABLED",
    )
    market_data_license_scope: MarketDataLicenseScope = Field(
        default=MarketDataLicenseScope.DISABLED,
        validation_alias="DATA_SYNC_MARKET_DATA_LICENSE_SCOPE",
    )
    market_data_license_reference: str = Field(
        default="",
        max_length=160,
        validation_alias="DATA_SYNC_MARKET_DATA_LICENSE_REFERENCE",
    )
    tushare_token: SecretStr | None = Field(
        default=None,
        validation_alias="DATA_SYNC_TUSHARE_TOKEN",
    )
    tushare_minimum_entitlement_points: int = Field(
        default=6000,
        ge=6000,
        validation_alias="DATA_SYNC_TUSHARE_MINIMUM_ENTITLEMENT_POINTS",
    )
    tushare_request_timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=120,
        validation_alias="DATA_SYNC_TUSHARE_REQUEST_TIMEOUT_SECONDS",
    )
    tushare_response_row_limit: int = Field(
        default=6000,
        ge=1000,
        le=10000,
        validation_alias="DATA_SYNC_TUSHARE_RESPONSE_ROW_LIMIT",
    )
    tushare_max_requests_per_minute: int = Field(
        default=180,
        ge=1,
        le=500,
        validation_alias="DATA_SYNC_TUSHARE_MAX_REQUESTS_PER_MINUTE",
    )
    tushare_max_retries: int = Field(
        default=3,
        ge=0,
        le=5,
        validation_alias="DATA_SYNC_TUSHARE_MAX_RETRIES",
    )
    tushare_retry_base_seconds: float = Field(
        default=1.0,
        gt=0,
        le=30,
        validation_alias="DATA_SYNC_TUSHARE_RETRY_BASE_SECONDS",
    )
    # 财务、指数使用具名来源策略而非单纯开关，便于在账本和事故排查中追溯批准依据。
    financial_source_policy: str = Field(
        default="disabled",
        validation_alias="DATA_SYNC_FINANCIAL_SOURCE_POLICY",
    )
    index_source_policy: str = Field(
        default="disabled",
        validation_alias="DATA_SYNC_INDEX_SOURCE_POLICY",
    )
    stock_connect_license_scope: str = Field(
        default="",
        validation_alias="DATA_SYNC_STOCK_CONNECT_LICENSE_SCOPE",
    )
    hkex_sftp_host: str = Field(
        default="sftp.data.hkex.com.hk",
        validation_alias="DATA_SYNC_HKEX_SFTP_HOST",
    )
    hkex_sftp_port: int = Field(
        default=22,
        ge=1,
        le=65535,
        validation_alias="DATA_SYNC_HKEX_SFTP_PORT",
    )
    hkex_sftp_username: str = Field(
        default="",
        validation_alias="DATA_SYNC_HKEX_SFTP_USERNAME",
    )
    hkex_sftp_private_key_path: Path = Field(
        default=Path("/run/secrets/hkex_data_marketplace_private_key"),
        validation_alias="DATA_SYNC_HKEX_SFTP_PRIVATE_KEY_PATH",
    )
    hkex_sftp_private_key_passphrase: SecretStr | None = Field(
        default=None,
        validation_alias="DATA_SYNC_HKEX_SFTP_PRIVATE_KEY_PASSPHRASE",
    )
    hkex_sftp_known_hosts_path: Path = Field(
        default=Path("/etc/ssh/ssh_known_hosts"),
        validation_alias="DATA_SYNC_HKEX_SFTP_KNOWN_HOSTS_PATH",
    )
    hkex_sh_daily_path_template: str = Field(
        default="",
        validation_alias="DATA_SYNC_HKEX_SH_DAILY_PATH_TEMPLATE",
    )
    hkex_sz_daily_path_template: str = Field(
        default="",
        validation_alias="DATA_SYNC_HKEX_SZ_DAILY_PATH_TEMPLATE",
    )
    hkex_securities_master_path_template: str = Field(
        default="",
        validation_alias="DATA_SYNC_HKEX_SECURITIES_MASTER_PATH_TEMPLATE",
    )
    hkex_securities_master_profile_manifest_path: Path = Field(
        default=Path(
            "/etc/quant-v2/stock-connect/hkex-securities-master-fixed-length-profile.json"
        ),
        validation_alias="DATA_SYNC_HKEX_SECURITIES_MASTER_PROFILE_MANIFEST_PATH",
    )
    hkex_securities_master_profile_manifest_sha256: str = Field(
        default="",
        validation_alias="DATA_SYNC_HKEX_SECURITIES_MASTER_PROFILE_MANIFEST_SHA256",
    )
    hkex_calendar_url_template: str = Field(
        default=(
            "https://www.hkex.com.hk/-/media/HKEX-Market/Mutual-Market/Stock-Connect/"
            "Reference-Materials/Trading-Hour%2C-Trading-and-Settlement-Calendar/"
            "{year}-Calendar_csv_e.csv"
        ),
        validation_alias="DATA_SYNC_HKEX_CALENDAR_URL_TEMPLATE",
    )
    hkex_calendar_manifest_path: Path = Field(
        default=Path("/run/quant-v2/stock-connect-config/hkex-calendar-manifest.json"),
        validation_alias="DATA_SYNC_HKEX_CALENDAR_MANIFEST_PATH",
    )
    hkex_sftp_delivery_manifest_path: Path = Field(
        default=Path("/run/quant-v2/stock-connect-config/hkex-sftp-delivery-manifest.json"),
        validation_alias="DATA_SYNC_HKEX_SFTP_DELIVERY_MANIFEST_PATH",
    )
    stock_connect_status_manifest_path: Path = Field(
        default=Path("/run/quant-v2/stock-connect-config/stock-connect-status-manifest.json"),
        validation_alias="DATA_SYNC_STOCK_CONNECT_STATUS_MANIFEST_PATH",
    )
    stock_connect_status_required_from: date | None = Field(
        default=None,
        validation_alias="DATA_SYNC_STOCK_CONNECT_STATUS_REQUIRED_FROM",
    )
    stock_connect_status_delivery_root: Path = Field(
        default=Path("/var/lib/quant-v2/stock-connect-status"),
        validation_alias="DATA_SYNC_STOCK_CONNECT_STATUS_DELIVERY_ROOT",
    )
    hkex_omdc_status_path_template: str = Field(
        default="hkex-omdc/{channel}/{year}/{trade_date}.bin",
        validation_alias="DATA_SYNC_HKEX_OMDC_STATUS_PATH_TEMPLATE",
    )
    sse_mdgw_status_path_template: str = Field(
        default="sse-mdgw/{year}/{trade_date}/trdses04.projection.csv",
        validation_alias="DATA_SYNC_SSE_MDGW_STATUS_PATH_TEMPLATE",
    )
    szse_step_status_path_template: str = Field(
        default="szse-step/{year}/{trade_date}/390019.projection.csv",
        validation_alias="DATA_SYNC_SZSE_STEP_STATUS_PATH_TEMPLATE",
    )
    stock_connect_request_timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=120,
        validation_alias="DATA_SYNC_STOCK_CONNECT_REQUEST_TIMEOUT_SECONDS",
    )
    stock_connect_preflight_timeout_seconds: int = Field(
        default=300,
        ge=5,
        le=3_600,
        validation_alias="DATA_SYNC_STOCK_CONNECT_PREFLIGHT_TIMEOUT_SECONDS",
    )
    stock_connect_min_partitions_per_minute: int = Field(
        default=20,
        ge=1,
        le=10_000,
        validation_alias="DATA_SYNC_STOCK_CONNECT_MIN_PARTITIONS_PER_MINUTE",
    )
    stock_connect_delivery_expiry_safety_seconds: int = Field(
        default=3_600,
        ge=0,
        le=86_400,
        validation_alias="DATA_SYNC_STOCK_CONNECT_DELIVERY_EXPIRY_SAFETY_SECONDS",
    )
    stock_connect_max_delivery_bytes: int = Field(
        default=67_108_864,
        ge=1_048_576,
        le=536_870_912,
        validation_alias="DATA_SYNC_STOCK_CONNECT_MAX_DELIVERY_BYTES",
    )
    stock_connect_max_manifest_bytes: int = Field(
        default=262_144,
        ge=1_024,
        le=1_048_576,
        validation_alias="DATA_SYNC_STOCK_CONNECT_MAX_MANIFEST_BYTES",
    )
    stock_connect_max_zip_compression_ratio: int = Field(
        default=100,
        ge=1,
        le=1_000,
        validation_alias="DATA_SYNC_STOCK_CONNECT_MAX_ZIP_COMPRESSION_RATIO",
    )
    stock_connect_raw_retention_mode: StockConnectRawRetentionMode = Field(
        default=StockConnectRawRetentionMode.MANIFEST_ONLY,
        validation_alias="DATA_SYNC_STOCK_CONNECT_RAW_RETENTION_MODE",
    )
    stock_connect_raw_retention_license_reference: str = Field(
        default="",
        max_length=320,
        validation_alias="DATA_SYNC_STOCK_CONNECT_RAW_RETENTION_LICENSE_REFERENCE",
    )
    stock_connect_raw_kms_key_id: str = Field(
        default="",
        max_length=320,
        validation_alias="DATA_SYNC_STOCK_CONNECT_RAW_KMS_KEY_ID",
    )
    stock_connect_cursor_hmac_secret: SecretStr | None = Field(
        default=None,
        validation_alias="DATA_SYNC_STOCK_CONNECT_CURSOR_HMAC_SECRET",
    )
    # dark launch 必须显式给出并发、速率和超时，防止默认值无意间压垮上游来源。
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
    # 既有内部读取 API 保留独立 bearer；数据运维控制面另行分离读与写服务身份。
    internal_api_bearer_token: SecretStr = Field(
        validation_alias="DATA_SYNC_INTERNAL_API_BEARER_TOKEN"
    )
    internal_read_api_bearer_token: SecretStr | None = Field(
        default=None,
        validation_alias="DATA_SYNC_INTERNAL_READ_API_BEARER_TOKEN",
    )
    internal_operations_api_bearer_token: SecretStr | None = Field(
        default=None,
        validation_alias="DATA_SYNC_INTERNAL_OPERATIONS_API_BEARER_TOKEN",
    )
    internal_api_host: str = Field(
        default="0.0.0.0", validation_alias="DATA_SYNC_INTERNAL_API_HOST"
    )
    internal_api_port: int = Field(
        default=8000, ge=1, le=65535, validation_alias="DATA_SYNC_INTERNAL_API_PORT"
    )
    # 所有 AKShare adapter 共用有界请求超时，避免 worker 被单次网络阻塞长期占用。
    akshare_request_timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=120,
        validation_alias="DATA_SYNC_AKSHARE_REQUEST_TIMEOUT_SECONDS",
    )
    etf_provider_min_interval_seconds: float = Field(
        default=0.25,
        ge=0,
        le=10,
        validation_alias="DATA_SYNC_ETF_PROVIDER_MIN_INTERVAL_SECONDS",
    )
    etf_auto_retry_max_attempts: int = Field(
        default=3,
        ge=1,
        le=5,
        validation_alias="DATA_SYNC_ETF_AUTO_RETRY_MAX_ATTEMPTS",
    )
    etf_auto_retry_base_seconds: float = Field(
        default=2,
        ge=0,
        le=60,
        validation_alias="DATA_SYNC_ETF_AUTO_RETRY_BASE_SECONDS",
    )
    etf_auto_retry_max_seconds: float = Field(
        default=30,
        ge=0,
        le=300,
        validation_alias="DATA_SYNC_ETF_AUTO_RETRY_MAX_SECONDS",
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

    @field_validator("index_source_policy")
    @classmethod
    def validate_index_source_policy(cls, value: str) -> str:
        """规范化指数来源策略，避免空白或大小写造成错误 adapter 注册。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("index source policy must not be blank")
        return normalized.lower()

    @field_validator("tushare_token", mode="before")
    @classmethod
    def normalize_optional_tushare_token(cls, value: object) -> object | None:
        """把 Compose 的可选空串规范为未配置，非空 token 继续接受统一长度门禁。"""
        if isinstance(value, SecretStr):
            return None if not value.get_secret_value().strip() else value
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "stock_connect_status_required_from",
        "stock_connect_cursor_hmac_secret",
        mode="before",
    )
    @classmethod
    def normalize_optional_stock_connect_setting(cls, value: object) -> object | None:
        """把关闭互联互通能力时 Compose 注入的空可选值规范为未配置。"""
        if isinstance(value, SecretStr):
            return None if not value.get_secret_value().strip() else value
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "internal_read_api_bearer_token",
        "internal_operations_api_bearer_token",
        "internal_api_bearer_token",
        "stock_connect_cursor_hmac_secret",
        "tushare_token",
    )
    @classmethod
    def validate_internal_api_bearer_token(cls, value: SecretStr | None) -> SecretStr | None:
        """要求已配置的内部服务凭据非空且达到最小长度，避免误用开发占位符。"""
        if value is None:
            return None
        if len(value.get_secret_value().strip()) < 32:
            raise ValueError("must contain at least 32 characters")
        return value

    @model_validator(mode="after")
    def validate_source_policy_settings(self) -> Settings:
        """验证能力开关、来源策略和资源上限之间的依赖关系。

        校验在任何网络连接、数据库连接或任务注册之前执行。这样配置错误会以启动
        失败暴露，而不是在同步过程中静默降级到错误来源或不受控的默认并发。
        """
        self._validate_internal_service_tokens()
        if self.market_overview_enabled:
            if not self.tushare_enabled:
                raise ValueError(
                    "DATA_SYNC_MARKET_OVERVIEW_ENABLED requires DATA_SYNC_TUSHARE_ENABLED"
                )
            if self.tushare_token is None:
                raise ValueError(
                    "DATA_SYNC_MARKET_OVERVIEW_ENABLED requires DATA_SYNC_TUSHARE_TOKEN"
                )
            if self.market_data_license_scope is MarketDataLicenseScope.DISABLED:
                raise ValueError(
                    "DATA_SYNC_MARKET_OVERVIEW_ENABLED requires an approved market data "
                    "license scope"
                )
            if self.environment in {Environment.STAGING, Environment.PRODUCTION} and (
                self.market_data_license_scope
                is not MarketDataLicenseScope.COMMERCIAL_REDISTRIBUTION_APPROVED
                or not self.market_data_license_reference.strip()
            ):
                raise ValueError(
                    "staging/production market overview requires commercial redistribution "
                    "approval and a license reference"
                )
        if self.tushare_enabled and self.tushare_token is None:
            raise ValueError("DATA_SYNC_TUSHARE_ENABLED requires DATA_SYNC_TUSHARE_TOKEN")
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
        if self.index_enabled:
            if not self.akshare_enabled:
                raise ValueError("DATA_SYNC_INDEX_ENABLED requires DATA_SYNC_AKSHARE_ENABLED")
            if self.index_source_policy not in {
                "akshare-csindex",
                "akshare-cnindex",
                "akshare-csindex-cnindex",
            }:
                raise ValueError(
                    "DATA_SYNC_INDEX_ENABLED requires an explicit approved index source policy"
                )
        if self.stock_connect_enabled:
            required_stock_connect = {
                "DATA_SYNC_STOCK_CONNECT_LICENSE_SCOPE": self.stock_connect_license_scope,
                "DATA_SYNC_HKEX_SFTP_USERNAME": self.hkex_sftp_username,
                "DATA_SYNC_HKEX_SH_DAILY_PATH_TEMPLATE": self.hkex_sh_daily_path_template,
                "DATA_SYNC_HKEX_SZ_DAILY_PATH_TEMPLATE": self.hkex_sz_daily_path_template,
                "DATA_SYNC_HKEX_SECURITIES_MASTER_PATH_TEMPLATE": (
                    self.hkex_securities_master_path_template
                ),
                "DATA_SYNC_HKEX_SECURITIES_MASTER_PROFILE_MANIFEST_SHA256": (
                    self.hkex_securities_master_profile_manifest_sha256
                ),
                "DATA_SYNC_STOCK_CONNECT_CURSOR_HMAC_SECRET": (
                    ""
                    if self.stock_connect_cursor_hmac_secret is None
                    else self.stock_connect_cursor_hmac_secret.get_secret_value()
                ),
            }
            missing_stock_connect = [
                name for name, value in required_stock_connect.items() if not value.strip()
            ]
            if missing_stock_connect:
                raise ValueError(
                    "official stock-connect source requires: " + ", ".join(missing_stock_connect)
                )
            if not self.hkex_sftp_private_key_path.is_absolute():
                raise ValueError("DATA_SYNC_HKEX_SFTP_PRIVATE_KEY_PATH must be absolute")
            if not self.hkex_sftp_known_hosts_path.is_absolute():
                raise ValueError("DATA_SYNC_HKEX_SFTP_KNOWN_HOSTS_PATH must be absolute")
            if not self.hkex_securities_master_profile_manifest_path.is_absolute():
                raise ValueError(
                    "DATA_SYNC_HKEX_SECURITIES_MASTER_PROFILE_MANIFEST_PATH must be absolute"
                )
            if not self.hkex_calendar_manifest_path.is_absolute():
                raise ValueError("DATA_SYNC_HKEX_CALENDAR_MANIFEST_PATH must be absolute")
            if not self.hkex_sftp_delivery_manifest_path.is_absolute():
                raise ValueError("DATA_SYNC_HKEX_SFTP_DELIVERY_MANIFEST_PATH must be absolute")
            if not self.stock_connect_status_manifest_path.is_absolute():
                raise ValueError("DATA_SYNC_STOCK_CONNECT_STATUS_MANIFEST_PATH must be absolute")
            if self.stock_connect_status_required_from is None:
                raise ValueError("DATA_SYNC_STOCK_CONNECT_STATUS_REQUIRED_FROM is required")
            manifest_digest = self.hkex_securities_master_profile_manifest_sha256
            if len(manifest_digest) != 64 or any(
                char not in "0123456789abcdef" for char in manifest_digest
            ):
                raise ValueError(
                    "DATA_SYNC_HKEX_SECURITIES_MASTER_PROFILE_MANIFEST_SHA256 "
                    "must be lowercase SHA-256"
                )
            if not self.stock_connect_status_delivery_root.is_absolute():
                raise ValueError("DATA_SYNC_STOCK_CONNECT_STATUS_DELIVERY_ROOT must be absolute")
            if "{channel}" not in self.hkex_omdc_status_path_template:
                raise ValueError("DATA_SYNC_HKEX_OMDC_STATUS_PATH_TEMPLATE must contain {channel}")
            if not self.hkex_calendar_url_template.startswith("https://"):
                raise ValueError("DATA_SYNC_HKEX_CALENDAR_URL_TEMPLATE must use HTTPS")
            if (
                self.stock_connect_raw_retention_mode
                is StockConnectRawRetentionMode.LICENSED_RAW_ALLOWED
                and (
                    not self.stock_connect_raw_retention_license_reference.strip()
                    or not self.stock_connect_raw_kms_key_id.strip()
                )
            ):
                raise ValueError(
                    "LICENSED_RAW_ALLOWED requires "
                    "DATA_SYNC_STOCK_CONNECT_RAW_RETENTION_LICENSE_REFERENCE and "
                    "DATA_SYNC_STOCK_CONNECT_RAW_KMS_KEY_ID"
                )
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

    def _validate_internal_service_tokens(self) -> None:
        """校验数据运维读写服务身份分离，不影响既有内部 API 的通用 bearer。"""
        read_token = self.internal_read_api_bearer_token
        operations_token = self.internal_operations_api_bearer_token
        if self.environment in {Environment.LOCAL, Environment.TEST}:
            if read_token is None and operations_token is None:
                return
            if read_token is not None and operations_token is not None:
                return
            raise ValueError(
                "DATA_SYNC_INTERNAL_READ_API_BEARER_TOKEN and "
                "DATA_SYNC_INTERNAL_OPERATIONS_API_BEARER_TOKEN must be configured together"
            )
        if read_token is None or operations_token is None:
            raise ValueError(
                "DATA_SYNC_INTERNAL_READ_API_BEARER_TOKEN and "
                "DATA_SYNC_INTERNAL_OPERATIONS_API_BEARER_TOKEN are required outside local/test"
            )
        if read_token.get_secret_value() == operations_token.get_secret_value():
            raise ValueError(
                "DATA_SYNC_INTERNAL_READ_API_BEARER_TOKEN and "
                "DATA_SYNC_INTERNAL_OPERATIONS_API_BEARER_TOKEN must differ outside local/test"
            )


def load_settings() -> Settings:
    """加载已校验配置，同时向调用方隐藏详细校验值。"""
    try:
        return Settings()  # type: ignore[call-arg]  # 值由 Pydantic 配置源加载。
    except ValidationError as error:
        raise ConfigurationError("invalid service-data-sync configuration") from error
