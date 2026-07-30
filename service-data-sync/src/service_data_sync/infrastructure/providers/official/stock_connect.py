"""HKEX、上交所和深交所互联互通官方交付适配器。

HKEX Data Marketplace 文件经严格校验的 SFTP 私钥连接读取；官方交易日历经 HTTPS
读取；OMD-C、MDGW 和 STEP 日终落地文件只从部署挂载的只读目录读取。适配器只产生
provider-neutral 批次，不接触 canonical 数据库，也不以网页抓取或第三方数据补洞。
"""

from __future__ import annotations

import asyncio
import codecs
import csv
import hashlib
import io
import json
import math
import re
import stat as stat_module
import struct
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from threading import Lock
from time import monotonic
from typing import Final
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    ProviderPreflightComponent,
    ProviderPreflightReport,
    ProviderPreflightRequest,
    ProviderStatusCoverageBoundary,
    SourceRequest,
)

MARKET_STAT_CAPABILITY: Final = "market.stock_connect.market_stat.reported"
ACTIVE_SECURITY_CAPABILITY: Final = "market.stock_connect.active_security.snapshot"
TRADING_CALENDAR_CAPABILITY: Final = "market.stock_connect.trading_calendar"
INSTRUMENT_MASTER_CAPABILITY: Final = "market.stock_connect.instrument_master.reported"
CHANNEL_STATUS_CAPABILITY: Final = "market.stock_connect.channel_status.eod"

_CAPABILITIES = frozenset(
    {
        MARKET_STAT_CAPABILITY,
        ACTIVE_SECURITY_CAPABILITY,
        TRADING_CALENDAR_CAPABILITY,
        INSTRUMENT_MASTER_CAPABILITY,
        CHANNEL_STATUS_CAPABILITY,
    }
)
_PROVIDER_ID = "official-stock-connect"
_ADAPTER_VERSION = "official-stock-connect-v1"
_PREFLIGHT_MANIFEST_SCHEMA = "quant-v2.stock-connect-preflight-delivery-manifest.v1"
_DELIVERY_MANIFEST_DAY_SCHEMA = "quant-v2.stock-connect-delivery-day.v1"
_DELIVERY_MANIFEST_PAGE_SCHEMA = "quant-v2.delivery-manifest-page.v1"
_READINESS_EVIDENCE_SCHEMA = "quant-v2.stock-connect-readiness-evidence.v1"
_CALENDAR_MANIFEST_SCHEMA = "quant-v2.hkex-stock-connect-calendar-manifest.v1"
_STATUS_MANIFEST_SCHEMA = "quant-v2.stock-connect-status-coverage-manifest.v1"
_SFTP_DELIVERY_MANIFEST_SCHEMA = "quant-v2.hkex-sftp-delivery-manifest.v1"
_SFTP_DELIVERY_PAGE_SCHEMA = "quant-v2.hkex-sftp-delivery-manifest-page.v1"
_NORTHBOUND_DISCLOSURE_CHANGE = date(2024, 8, 19)
_BUNDLE_FIRST_ISSUE_DATES = {
    "SH": date(2014, 11, 17),
    "SZ": date(2016, 12, 5),
}
_OMDC_NULL_INT64 = -(2**63)
_SCHEMA_PROFILES = {
    "hkex-daily-statistics-v1",
    "hkex-securities-master-fixed-length-manifest-v2",
    "hkex-calendar-v1",
    "hkex-omdc-mmdh-msg80-v2.1",
    "sse-is117-v1.09-is124-v3.50-gateway-v1",
    "szse-step-binary-v1.17-msg390019-gateway-v1",
}


@dataclass(frozen=True, slots=True)
class OfficialStockConnectConfig:
    """保存官方交付位置、私钥认证和冻结 schema profile，不包含业务默认降级。"""

    sftp_host: str
    sftp_port: int
    sftp_username: str
    sftp_private_key_path: Path
    sftp_private_key_passphrase: str | None
    sftp_known_hosts_path: Path
    sh_daily_path_template: str
    sz_daily_path_template: str
    securities_master_path_template: str
    securities_master_profile_manifest_path: Path
    securities_master_profile_manifest_sha256: str
    calendar_url_template: str
    calendar_manifest_path: Path
    sftp_delivery_manifest_path: Path
    status_delivery_root: Path
    status_manifest_path: Path
    status_required_from: date | None
    omdc_status_path_template: str
    sse_status_path_template: str
    szse_status_path_template: str
    request_timeout_seconds: int
    preflight_timeout_seconds: int
    min_partitions_per_minute: int
    delivery_expiry_safety_seconds: int
    max_delivery_bytes: int
    max_manifest_bytes: int
    max_zip_compression_ratio: int
    daily_statistics_profile: str = "hkex-daily-statistics-v1"
    securities_master_profile: str = "hkex-securities-master-fixed-length-manifest-v2"
    calendar_profile: str = "hkex-calendar-v1"
    omdc_profile: str = "hkex-omdc-mmdh-msg80-v2.1"
    sse_profile: str = "sse-is117-v1.09-is124-v3.50-gateway-v1"
    szse_profile: str = "szse-step-binary-v1.17-msg390019-gateway-v1"

    def __post_init__(self) -> None:
        """在网络访问前拒绝空授权输入、非标准 profile 和不安全交付路径。"""
        if not self.sftp_host.strip() or not self.sftp_username.strip():
            raise ValueError("HKEX SFTP host and registered-email username are required")
        if not 1 <= self.sftp_port <= 65535:
            raise ValueError("HKEX SFTP port is invalid")
        if not 1 <= self.request_timeout_seconds <= 120:
            raise ValueError("stock-connect request timeout is invalid")
        if not 5 <= self.preflight_timeout_seconds <= 3_600:
            raise ValueError("stock-connect preflight timeout is invalid")
        if not 1 <= self.min_partitions_per_minute <= 10_000:
            raise ValueError("stock-connect minimum partition throughput is invalid")
        if not 0 <= self.delivery_expiry_safety_seconds <= 86_400:
            raise ValueError("stock-connect delivery expiry safety window is invalid")
        if not 1_048_576 <= self.max_delivery_bytes <= 536_870_912:
            raise ValueError("stock-connect delivery byte limit is invalid")
        if not 1_024 <= self.max_manifest_bytes <= 1_048_576:
            raise ValueError("stock-connect manifest byte limit is invalid")
        if not 1 <= self.max_zip_compression_ratio <= 1_000:
            raise ValueError("stock-connect ZIP compression ratio limit is invalid")
        if not self.sftp_private_key_path.is_absolute():
            raise ValueError("HKEX SFTP private key path must be absolute")
        if not self.sftp_known_hosts_path.is_absolute():
            raise ValueError("HKEX SFTP known-hosts path must be absolute")
        if not self.securities_master_profile_manifest_path.is_absolute():
            raise ValueError("HKEX Securities Master profile manifest path must be absolute")
        if not self.calendar_manifest_path.is_absolute():
            raise ValueError("HKEX calendar manifest path must be absolute")
        if not self.sftp_delivery_manifest_path.is_absolute():
            raise ValueError("HKEX SFTP delivery manifest path must be absolute")
        if not self.status_manifest_path.is_absolute():
            raise ValueError("stock-connect status manifest path must be absolute")
        if self.status_required_from is None:
            raise ValueError("stock-connect status required-from date is unavailable")
        if len(self.securities_master_profile_manifest_sha256) != 64 or any(
            char not in "0123456789abcdef"
            for char in self.securities_master_profile_manifest_sha256
        ):
            raise ValueError("HKEX Securities Master profile manifest digest is invalid")
        if not self.status_delivery_root.is_absolute():
            raise ValueError("stock-connect status delivery root must be absolute")
        profiles = {
            self.daily_statistics_profile,
            self.securities_master_profile,
            self.calendar_profile,
            self.omdc_profile,
            self.sse_profile,
            self.szse_profile,
        }
        if not profiles <= _SCHEMA_PROFILES:
            raise ValueError("stock-connect schema profile is unsupported")
        for template in (
            self.sh_daily_path_template,
            self.sz_daily_path_template,
            self.securities_master_path_template,
            self.omdc_status_path_template,
            self.sse_status_path_template,
            self.szse_status_path_template,
        ):
            if not template.strip():
                raise ValueError("stock-connect delivery path template must not be blank")
        if not any(
            placeholder in self.securities_master_path_template
            for placeholder in ("{issued_date}", "{issued_iso_date}")
        ):
            raise ValueError(
                "HKEX Securities Master path must use an explicit issued-date placeholder"
            )
        if "{channel}" not in self.omdc_status_path_template:
            raise ValueError("OMD-C status path must contain an explicit channel placeholder")
        _validate_hkex_https_url(self.calendar_url_template.format(year=2026))


@dataclass(frozen=True, slots=True)
class _FetchedObject:
    """保存真实交付对象；来源未报告 publication 时保持空值。"""

    payload: bytes
    content_type: str
    published_at: datetime | None
    product_name: str
    upstream_source: str


@dataclass(frozen=True, slots=True)
class _CalendarManifestEntry:
    """冻结一个年度官方日历的取得方式、摘要与来源时间。"""

    year: int
    source_kind: str
    url: str | None
    relative_path: str | None
    payload_sha256: str
    source_publication_at: datetime
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class _StatusManifestEntry:
    """冻结一份明确纳入运营交付的状态对象及其 sidecar 摘要。"""

    trade_date: date
    channel: str
    direction: str
    relative_path: str
    profile_id: str
    payload_sha256: str
    sidecar_sha256: str


@dataclass(frozen=True, slots=True)
class _StatusCoverageManifest:
    """保存状态覆盖边界；历史未声明日期保留真实缺源语义。"""

    observed_at: datetime
    required_from: date
    manifest_sha256: str
    entries: Mapping[tuple[date, str, str], _StatusManifestEntry]


@dataclass(frozen=True, slots=True)
class StockConnectManifestValidation:
    """返回只读清单校验的非敏感摘要，不携带对象路径或订单引用。"""

    manifest_kind: str
    sha256: str
    entry_count: int
    root_hash: str | None = None
    required_from: date | None = None


@dataclass(frozen=True, slots=True)
class _SftpDeliveryEntitlement:
    """冻结 licensed 历史订单对象的远端路径、订单引用和可用截止。"""

    remote_path: str
    order_reference: str
    available_until: datetime


@dataclass(frozen=True, slots=True)
class _SftpDeliveryManifest:
    """保存摘要分页的 SFTP entitlement 目录，单页不超过 sidecar 字节上限。"""

    root_hash: str
    page_count: int
    entries: Mapping[str, _SftpDeliveryEntitlement]


@dataclass(frozen=True, slots=True)
class _DailyStatisticsRegimeProfile:
    """描述 Daily Statistics 在明确制度区间内的买卖额披露约束。"""

    profile_id: str
    effective_from: date
    effective_to: date | None
    direction: str
    buy_sell_policy: str


_DAILY_STATISTICS_REGIME_PROFILES = (
    _DailyStatisticsRegimeProfile(
        profile_id="hkex-daily-statistics-v1-northbound-buy-sell-reported",
        effective_from=date(2014, 11, 17),
        effective_to=date(2024, 8, 18),
        direction="NORTHBOUND",
        buy_sell_policy="REQUIRED",
    ),
    _DailyStatisticsRegimeProfile(
        profile_id="hkex-daily-statistics-v2-northbound-turnover-only",
        effective_from=_NORTHBOUND_DISCLOSURE_CHANGE,
        effective_to=None,
        direction="NORTHBOUND",
        buy_sell_policy="NOT_DISCLOSED",
    ),
    _DailyStatisticsRegimeProfile(
        profile_id="hkex-daily-statistics-v1-southbound-buy-sell-reported",
        effective_from=date(2014, 11, 17),
        effective_to=None,
        direction="SOUTHBOUND",
        buy_sell_policy="REQUIRED",
    ),
)


@dataclass(frozen=True, slots=True)
class _FixedFieldLayout:
    """描述 licensed fixed-length 记录内一个按字节定位的字段。"""

    offset_bytes: int
    length_bytes: int
    encoding: str
    trim: str
    date_format: str | None = None


@dataclass(frozen=True, slots=True)
class _FixedRecordSelector:
    """描述用于识别数据记录的固定字段和值；空 selector 表示每行都是数据记录。"""

    field: _FixedFieldLayout
    equals: str


@dataclass(frozen=True, slots=True)
class _MasterFixedLengthProfile:
    """保存经摘要钉住的 HKEX Securities Master 精确字节布局。"""

    effective_from: date
    effective_to: date | None
    profile_id: str
    specification_reference: str
    record_length_bytes: int
    line_ending: str
    file_name_pattern: str
    file_name_date_group: str
    file_name_date_format: str
    record_selector: _FixedRecordSelector | None
    security_id: _FixedFieldLayout
    instrument_code: _FixedFieldLayout
    display_name: _FixedFieldLayout
    effective_trade_date: _FixedFieldLayout


@dataclass(frozen=True, slots=True)
class _MasterFixedLengthProfileSet:
    """保存互不重叠的历史布局区间，并按主档生效日唯一选择。"""

    profiles: tuple[_MasterFixedLengthProfile, ...]

    def select(self, effective_date: date) -> _MasterFixedLengthProfile:
        """返回唯一覆盖生效日的布局；缺口或重叠都不能猜列宽。"""
        matches = [
            profile
            for profile in self.profiles
            if profile.effective_from <= effective_date
            and (profile.effective_to is None or effective_date <= profile.effective_to)
        ]
        if len(matches) != 1:
            raise ValueError("HKEX Securities Master has no unique profile for effective date")
        return matches[0]


@dataclass(frozen=True, slots=True)
class _StockConnectPreflightScope:
    """保存一次来源预检真正会影响的通道、方向和日期边界。"""

    channels: tuple[str, ...]
    directions: tuple[str, ...]
    channel_starts: tuple[tuple[str, date], ...]
    end: date


@dataclass(frozen=True, slots=True)
class _StockConnectProbeDates:
    """保存官方日历解析出的全窗口开放日和交付目标，禁止首尾抽样。"""

    daily_dates: tuple[tuple[str, str, date], ...]
    status_dates: tuple[tuple[str, str, str, date], ...]
    master_dates: tuple[tuple[str, date, date], ...]
    calendar_deliveries: tuple[dict[str, object], ...] = ()
    bundle_targets: tuple[tuple[str, str, date], ...] = ()
    readiness_calendar_days: tuple[dict[str, object], ...] = ()


class OfficialStockConnectAdapter(DataSourcePort):
    """通过官方 SFTP、HTTPS 和只读落地目录提供五项互联互通能力。"""

    @property
    def provider_id(self) -> str:
        """返回来源注册表使用的稳定官方适配器标识。"""
        return _PROVIDER_ID

    def __init__(
        self,
        config: OfficialStockConnectConfig,
        *,
        now: Callable[[], datetime] | None = None,
        sftp_reader: Callable[[str], _FetchedObject] | None = None,
        https_reader: Callable[[str], _FetchedObject] | None = None,
    ) -> None:
        """保存配置并在注册能力前校验只读挂载与 fixed-length profile。"""
        self._config = config
        _validate_official_mounts(config)
        self._master_profiles = _load_master_fixed_length_profile(
            config.securities_master_profile_manifest_path,
            expected_sha256=config.securities_master_profile_manifest_sha256,
            max_bytes=config.max_manifest_bytes,
        )
        self._calendar_manifest = _load_calendar_manifest(
            config.calendar_manifest_path,
            max_bytes=config.max_manifest_bytes,
        )
        self._calendar_manifest_sha256 = _sha256(
            _read_bounded_file(
                config.calendar_manifest_path,
                max_bytes=config.max_manifest_bytes,
            )
        )
        self._sftp_delivery_manifest = _load_sftp_delivery_manifest(
            config.sftp_delivery_manifest_path,
            max_bytes_per_page=config.max_manifest_bytes,
        )
        self._status_manifest = _load_status_coverage_manifest(
            config.status_manifest_path,
            expected_required_from=config.status_required_from,
            max_bytes=config.max_manifest_bytes,
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._custom_https_reader = https_reader
        self._sftp_reader = sftp_reader or self._read_sftp
        self._https_reader = https_reader or self._read_https
        self._daily_cache: dict[tuple[str, date], _FetchedObject] = {}
        self._calendar_cache: dict[int, tuple[_FetchedObject, list[dict[str, object]]]] = {}
        self._cache_lock = Lock()

    def capabilities(self) -> frozenset[str]:
        """返回无需发起网络请求即可确定的官方能力集合。"""
        return _CAPABILITIES

    def status_coverage_boundary(self) -> ProviderStatusCoverageBoundary:
        """返回本地清单已验证的候选状态边界，供控制面持久化单向锁确认。"""
        return ProviderStatusCoverageBoundary(
            required_from=self._status_manifest.required_from,
            manifest_sha256=self._status_manifest.manifest_sha256,
        )

    def preflight_probe(self, request: ProviderPreflightRequest) -> ProviderPreflightReport:
        """在人工提交前全量验证窗口内日历、SFTP entitlement 与最终状态 landing。"""
        probe_observed_at = self._now()
        timeout = min(
            request.timeout_seconds,
            self._config.preflight_timeout_seconds,
        )
        deadline = monotonic() + timeout
        checks = [
            ProviderPreflightComponent(
                component="fixed-length-profile-manifest",
                accepted=True,
                reason="PROFILE_MANIFEST_VERIFIED",
            )
        ]
        try:
            scope = _preflight_scope(
                request,
                anchor_date=probe_observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date(),
            )
        except (TypeError, ValueError):
            checks.append(
                ProviderPreflightComponent(
                    component="request-scope",
                    accepted=False,
                    reason="REQUEST_SCOPE_INVALID",
                )
            )
            return ProviderPreflightReport(components=tuple(checks))
        try:
            probe_dates = self._preflight_calendar_dates(
                scope,
                deadline=deadline,
            )
        except TimeoutError:
            return ProviderPreflightReport(
                components=tuple(
                    checks
                    + _unavailable_preflight_components(
                        scope, calendar_reason="PROBE_DEADLINE_EXCEEDED"
                    )
                ),
                readiness_evidence=_unavailable_readiness_evidence(
                    scope=scope,
                    calendar_manifest_sha256=self._calendar_manifest_sha256,
                    evidence_observed_at=probe_observed_at,
                ),
            )
        except (OSError, ValueError, ProviderError):
            return ProviderPreflightReport(
                components=tuple(
                    checks
                    + _unavailable_preflight_components(
                        scope, calendar_reason="CALENDAR_PROBE_FAILED"
                    )
                ),
                readiness_evidence=_unavailable_readiness_evidence(
                    scope=scope,
                    calendar_manifest_sha256=self._calendar_manifest_sha256,
                    evidence_observed_at=probe_observed_at,
                ),
            )
        checks.append(
            ProviderPreflightComponent(
                component="hkex-calendar-https",
                accepted=True,
                reason="CALENDAR_DELIVERY_VERIFIED",
            )
        )
        sftp_checks, sftp_deliveries = self._preflight_sftp(
            probe_dates,
            deadline=deadline,
        )
        checks.extend(sftp_checks)
        status_checks, status_deliveries = self._preflight_status_landings(
            probe_dates,
            deadline=deadline,
        )
        checks.extend(status_checks)
        manifest = _preflight_delivery_manifest(
            request=request,
            probe_dates=probe_dates,
            sftp_deliveries=sftp_deliveries,
            status_deliveries=status_deliveries,
            profile_manifest_sha256=(self._config.securities_master_profile_manifest_sha256),
            calendar_manifest_sha256=self._calendar_manifest_sha256,
            sftp_delivery_manifest_root_hash=(self._sftp_delivery_manifest.root_hash),
            status_manifest_sha256=self._status_manifest.manifest_sha256,
            min_partitions_per_minute=self._config.min_partitions_per_minute,
            delivery_expiry_safety_seconds=self._config.delivery_expiry_safety_seconds,
        )
        return ProviderPreflightReport(
            components=tuple(checks),
            execution_evidence=manifest,
            readiness_evidence=_available_readiness_evidence(
                probe_dates=probe_dates,
                calendar_manifest_sha256=self._calendar_manifest_sha256,
                evidence_observed_at=probe_observed_at,
            ),
        )

    def verify_preflight_evidence(
        self,
        evidence: Mapping[str, object],
        *,
        timeout_seconds: int,
        target_keys: tuple[str, ...] | None = None,
    ) -> tuple[ProviderPreflightComponent, ...]:
        """执行前复核当前公平批次，并要求所需交付对象版本与全窗冻结清单一致。"""
        try:
            frozen_request = _request_from_preflight_manifest(
                evidence,
                timeout_seconds=timeout_seconds,
            )
            request = (
                frozen_request
                if target_keys is None
                else _batch_revalidation_request(
                    evidence,
                    target_keys=target_keys,
                    timeout_seconds=timeout_seconds,
                )
            )
        except (TypeError, ValueError):
            return (
                ProviderPreflightComponent(
                    component="frozen-delivery-manifest",
                    accepted=False,
                    reason="DELIVERY_MANIFEST_INVALID",
                ),
            )
        current = self.preflight_probe(request)
        checks = list(current.components)
        if not checks or not all(item.accepted for item in checks):
            checks.append(
                ProviderPreflightComponent(
                    component="frozen-delivery-manifest",
                    accepted=False,
                    reason="DELIVERY_REVALIDATION_FAILED",
                )
            )
            return tuple(checks)
        current_evidence = current.execution_evidence
        if current_evidence is None or not _revalidated_evidence_matches(
            frozen=evidence,
            current=current_evidence,
            target_keys=target_keys,
        ):
            checks.append(
                ProviderPreflightComponent(
                    component="frozen-delivery-manifest",
                    accepted=False,
                    reason="DELIVERY_MANIFEST_DRIFTED",
                )
            )
            return tuple(checks)
        checks.append(
            ProviderPreflightComponent(
                component="frozen-delivery-manifest",
                accepted=True,
                reason="DELIVERY_MANIFEST_REVERIFIED",
            )
        )
        return tuple(checks)

    def _preflight_calendar_dates(
        self,
        scope: _StockConnectPreflightScope,
        *,
        deadline: float,
    ) -> _StockConnectProbeDates:
        """在线读取覆盖完整窗口的官方日历，并枚举每条通道方向的全部开放日。"""
        records_by_year: dict[int, list[dict[str, object]]] = {}
        fetched_by_year: dict[int, _FetchedObject] = {}

        def load_year(year: int) -> list[dict[str, object]]:
            """在同一总 deadline 内下载、解析并缓存一个年度官方日历。"""
            cached = records_by_year.get(year)
            if cached is not None:
                return cached
            remaining = _deadline_remaining(deadline)
            fetched = self._calendar_object(
                year,
                timeout_seconds=remaining,
            )
            _deadline_remaining(deadline)
            _required_publication_at(fetched)
            parsed = parse_hkex_stock_connect_calendar(
                fetched.payload,
                year=year,
                profile=self._config.calendar_profile,
            )
            records_by_year[year] = parsed
            fetched_by_year[year] = fetched
            with self._cache_lock:
                self._calendar_cache[year] = (fetched, parsed)
            return parsed

        def open_dates(
            channel: str,
            direction: str,
        ) -> tuple[date, ...]:
            """按通道上线边界和方向日历返回完整窗口开放日。"""
            start = dict(scope.channel_starts)[channel]
            key = "northboundTrading" if direction == "NORTHBOUND" else "southboundTrading"
            selected: list[date] = []
            for year in range(start.year, scope.end.year + 1):
                selected.extend(
                    current
                    for item in load_year(year)
                    if item.get(key) is True
                    and start
                    <= (current := date.fromisoformat(str(item["calendarDate"])))
                    <= scope.end
                )
            return tuple(sorted(set(selected)))

        selected: dict[tuple[str, str], tuple[date, ...]] = {
            (channel, direction): open_dates(channel, direction)
            for channel in scope.channels
            for direction in scope.directions
        }
        daily_dates = sorted(
            {
                (channel, boundary)
                for (channel, _direction), boundaries in selected.items()
                for boundary in boundaries
            }
        )
        status_dates = sorted(
            {
                (channel, direction, boundary)
                for (channel, direction), boundaries in selected.items()
                for boundary in boundaries
            },
            key=lambda item: (item[2], item[0], item[1]),
        )
        master_effective_dates = sorted(
            {
                boundary
                for (channel, direction), boundaries in selected.items()
                if direction == "SOUTHBOUND"
                for boundary in boundaries
            }
        )
        if master_effective_dates:
            load_year(master_effective_dates[0].year - 1)
        hong_kong_open = sorted(
            {
                current
                for records in records_by_year.values()
                for item in records
                if str(item["hongKongState"]).strip().lower() not in {"closed", "holiday"}
                for current in (date.fromisoformat(str(item["calendarDate"])),)
            }
        )

        def master_issued_date(effective_date: date) -> date:
            """从完整官方香港日历寻找主档 T+1 生效日前最近的 issued date。"""
            candidates = [current for current in hong_kong_open if current < effective_date]
            if not candidates:
                raise ValueError("HKEX Securities Master issued date is unavailable")
            return candidates[-1]

        calendar_deliveries = tuple(
            {
                "year": year,
                "sourceKind": self._calendar_manifest[year].source_kind,
                "locationRef": (
                    self._calendar_manifest[year].relative_path
                    or self._calendar_manifest[year].url
                    or self._config.calendar_url_template.format(year=year)
                ),
                "byteSize": len(fetched_by_year[year].payload),
                "payloadSha256": _sha256(fetched_by_year[year].payload),
                "sourcePublicationAt": _timestamp(_required_publication_at(fetched_by_year[year])),
                "manifestObservedAt": _timestamp(self._calendar_manifest[year].observed_at),
            }
            for year in sorted(fetched_by_year)
        )
        calendar_by_date = {
            date.fromisoformat(str(item["calendarDate"])): item
            for records in records_by_year.values()
            for item in records
        }
        readiness_calendar_days: list[dict[str, object]] = []
        for channel in scope.channels:
            start = dict(scope.channel_starts)[channel]
            for direction in scope.directions:
                trading_key = (
                    "northboundTrading" if direction == "NORTHBOUND" else "southboundTrading"
                )
                for calendar_date in _inclusive_dates(start, scope.end):
                    record = calendar_by_date.get(calendar_date)
                    if record is None:
                        raise ValueError("stock-connect official calendar has a date coverage gap")
                    source = self._calendar_manifest[calendar_date.year]
                    fetched = fetched_by_year[calendar_date.year]
                    readiness_calendar_days.append(
                        {
                            "calendarDate": calendar_date.isoformat(),
                            "channel": channel,
                            "direction": direction,
                            "calendarState": (
                                "OPEN" if record.get(trading_key) is True else "CLOSED"
                            ),
                            "sourceFileSha256": _sha256(fetched.payload),
                            "sourcePublicationAt": _timestamp(_required_publication_at(fetched)),
                            "publicationAvailability": "REPORTED",
                            "sourceObservedAt": _timestamp(source.observed_at),
                        }
                    )
        return _StockConnectProbeDates(
            daily_dates=tuple(
                (
                    f"hkex-daily-statistics-{channel.lower()}-{trade_date.isoformat()}",
                    channel,
                    trade_date,
                )
                for channel, trade_date in daily_dates
            ),
            status_dates=tuple(
                (
                    (f"status-{channel.lower()}-{direction.lower()}-{trade_date.isoformat()}"),
                    channel,
                    direction,
                    trade_date,
                )
                for channel, direction, trade_date in status_dates
            ),
            master_dates=tuple(
                (
                    f"hkex-securities-master-{effective_date.isoformat()}",
                    effective_date,
                    master_issued_date(effective_date),
                )
                for effective_date in master_effective_dates
            ),
            calendar_deliveries=calendar_deliveries,
            bundle_targets=tuple(
                (channel, direction, trade_date) for channel, direction, trade_date in status_dates
            ),
            readiness_calendar_days=tuple(
                sorted(
                    readiness_calendar_days,
                    key=lambda item: (
                        str(item["calendarDate"]),
                        str(item["channel"]),
                        str(item["direction"]),
                    ),
                )
            ),
        )

    def _preflight_sftp(
        self,
        probe_dates: _StockConnectProbeDates,
        *,
        deadline: float,
    ) -> tuple[tuple[ProviderPreflightComponent, ...], tuple[dict[str, object], ...]]:
        """以目录批量 stat 全窗口产品，并冻结远端对象大小和版本元数据。"""
        targets: list[dict[str, object]] = []
        for component, channel, trade_date in probe_dates.daily_dates:
            template = (
                self._config.sh_daily_path_template
                if channel == "SH"
                else self._config.sz_daily_path_template
            )
            targets.append(
                {
                    "component": component,
                    "deliveryKind": "DAILY_STATISTICS",
                    "capability": MARKET_STAT_CAPABILITY,
                    "channel": channel,
                    "direction": None,
                    "tradeDate": trade_date.isoformat(),
                    "issuedDate": None,
                    "remotePath": _format_remote_path(
                        template,
                        trade_date=trade_date,
                        channel=channel,
                    ),
                }
            )
        for component, effective_date, issued_date in probe_dates.master_dates:
            try:
                master_profile = self._master_profiles.select(effective_date)
            except ValueError:
                return (
                    (
                        ProviderPreflightComponent(
                            component="hkex-securities-master-deliveries",
                            accepted=False,
                            reason="MASTER_PROFILE_UNAVAILABLE",
                        ),
                    ),
                    tuple(targets),
                )
            master_path = _format_remote_path(
                self._config.securities_master_path_template,
                trade_date=effective_date,
                issued_date=issued_date,
                channel="HKEX",
            )
            if (
                re.fullmatch(
                    master_profile.file_name_pattern,
                    PurePosixPath(master_path).name,
                )
                is None
            ):
                return (
                    (
                        ProviderPreflightComponent(
                            component="sftp-authentication",
                            accepted=False,
                            reason="MASTER_PRODUCT_PATH_INVALID",
                        ),
                        ProviderPreflightComponent(
                            component="hkex-securities-master-deliveries",
                            accepted=False,
                            reason="MASTER_PRODUCT_PATH_INVALID",
                        ),
                    ),
                    tuple(targets),
                )
            targets.append(
                {
                    "component": component,
                    "deliveryKind": "SECURITIES_MASTER",
                    "capability": INSTRUMENT_MASTER_CAPABILITY,
                    "channel": "HKEX",
                    "direction": "SOUTHBOUND",
                    "tradeDate": effective_date.isoformat(),
                    "issuedDate": issued_date.isoformat(),
                    "remotePath": master_path,
                }
            )

        missing_required_entitlements: list[str] = []
        available_until_values: list[datetime] = []
        for target in targets:
            remote_path = str(target["remotePath"])
            entitlement = self._sftp_delivery_manifest.entries.get(remote_path)
            if entitlement is None:
                target["orderReference"] = None
                target["availableUntil"] = None
                if target["deliveryKind"] == "DAILY_STATISTICS":
                    missing_required_entitlements.append(remote_path)
                continue
            target["orderReference"] = entitlement.order_reference
            target["availableUntil"] = _timestamp(entitlement.available_until)
            if target["deliveryKind"] == "DAILY_STATISTICS":
                available_until_values.append(entitlement.available_until)
        if missing_required_entitlements:
            return (
                (
                    ProviderPreflightComponent(
                        component="hkex-sftp-entitlement-manifest",
                        accepted=False,
                        reason="DELIVERY_ENTITLEMENT_MISSING",
                    ),
                ),
                tuple(
                    {
                        **target,
                        "available": False,
                        "byteSize": None,
                        "remoteModifiedAtEpochSeconds": None,
                        "availabilityReason": (
                            "DELIVERY_ENTITLEMENT_MISSING"
                            if target["deliveryKind"] == "DAILY_STATISTICS"
                            else "SOURCE_MISSING"
                        ),
                    }
                    for target in targets
                ),
            )
        if not available_until_values:
            return (
                (
                    ProviderPreflightComponent(
                        component="hkex-sftp-entitlement-manifest",
                        accepted=False,
                        reason="DELIVERY_ENTITLEMENT_MISSING",
                    ),
                ),
                tuple(targets),
            )
        estimated_seconds = math.ceil(
            len(probe_dates.bundle_targets) / self._config.min_partitions_per_minute * 60
        )
        estimated_completion_at = self._now() + timedelta(
            seconds=(estimated_seconds + self._config.delivery_expiry_safety_seconds)
        )
        minimum_available_until = min(available_until_values)
        if estimated_completion_at >= minimum_available_until:
            return (
                (
                    ProviderPreflightComponent(
                        component="hkex-sftp-entitlement-manifest",
                        accepted=False,
                        reason="DELIVERY_WINDOW_INSUFFICIENT",
                    ),
                ),
                tuple(
                    {
                        **target,
                        "available": False,
                        "byteSize": None,
                        "remoteModifiedAtEpochSeconds": None,
                        "estimatedCompletionAt": _timestamp(estimated_completion_at),
                    }
                    for target in targets
                ),
            )

        results: list[ProviderPreflightComponent] = [
            ProviderPreflightComponent(
                component="hkex-sftp-entitlement-manifest",
                accepted=True,
                reason="DELIVERY_WINDOW_SUFFICIENT",
            )
        ]
        evidence_by_path: dict[str, dict[str, object]] = {
            str(target["remotePath"]): {
                **target,
                "available": False,
                "byteSize": None,
                "remoteModifiedAtEpochSeconds": None,
                "availabilityReason": "SOURCE_MISSING",
            }
            for target in targets
            if target["orderReference"] is None
        }
        client = None
        sftp = None
        try:
            import paramiko

            remaining = _deadline_remaining(deadline)
            client = paramiko.SSHClient()
            client.load_host_keys(str(self._config.sftp_known_hosts_path))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.connect(
                hostname=self._config.sftp_host,
                port=self._config.sftp_port,
                username=self._config.sftp_username,
                key_filename=str(self._config.sftp_private_key_path),
                passphrase=self._config.sftp_private_key_passphrase,
                timeout=remaining,
                banner_timeout=remaining,
                auth_timeout=remaining,
                allow_agent=False,
                look_for_keys=False,
            )
            _deadline_remaining(deadline)
            transport = client.get_transport()
            if transport is None or not transport.is_authenticated():
                raise RuntimeError("SFTP transport is not authenticated")
            transport.sock.settimeout(_deadline_remaining(deadline))
            sftp = client.open_sftp()
            results.append(
                ProviderPreflightComponent(
                    component="sftp-authentication",
                    accepted=True,
                    reason="STRICT_HOST_KEY_AND_KEY_AUTH_VERIFIED",
                )
            )
            grouped: dict[str, dict[str, dict[str, object]]] = {}
            for target in targets:
                if target["orderReference"] is None:
                    continue
                remote_path = str(target["remotePath"])
                pure_path = PurePosixPath(remote_path)
                grouped.setdefault(str(pure_path.parent), {})[pure_path.name] = target
            failed_paths: set[str] = set()
            for parent, expected in sorted(grouped.items()):
                try:
                    transport.sock.settimeout(_deadline_remaining(deadline))
                    attributes = {
                        str(attribute.filename): attribute
                        for attribute in sftp.listdir_attr(parent)
                        if str(attribute.filename) in expected
                    }
                except TimeoutError:
                    failed_paths.update(
                        str(PurePosixPath(parent) / filename) for filename in expected
                    )
                    continue
                except OSError:
                    failed_paths.update(
                        str(PurePosixPath(parent) / filename) for filename in expected
                    )
                    continue
                for filename, target in expected.items():
                    remote_path = str(target["remotePath"])
                    attribute = attributes.get(filename)
                    try:
                        if attribute is None:
                            raise ValueError("delivery is absent from remote directory listing")
                        size = attribute.st_size
                        mode = attribute.st_mode
                        if (
                            not isinstance(size, int)
                            or size <= 0
                            or size > self._config.max_delivery_bytes
                            or (
                                isinstance(mode, int)
                                and mode != 0
                                and not stat_module.S_ISREG(mode)
                            )
                        ):
                            raise ValueError("delivery stat is outside approved bounds")
                        remote_mtime = attribute.st_mtime
                        if remote_mtime is not None and not isinstance(remote_mtime, int):
                            raise ValueError("delivery stat modification time is invalid")
                    except ValueError:
                        failed_paths.add(remote_path)
                        continue
                    evidence_by_path[remote_path] = {
                        **target,
                        "available": True,
                        "byteSize": size,
                        "availabilityReason": "AVAILABLE",
                        # 远端 mtime 仅作为对象版本复核值，绝不映射为 publication。
                        "remoteModifiedAtEpochSeconds": remote_mtime,
                    }
            product_groups: dict[tuple[str, str], list[dict[str, object]]] = {}
            for target in targets:
                product_groups.setdefault(
                    (str(target["deliveryKind"]), str(target["channel"])),
                    [],
                ).append(target)
            for group_targets in product_groups.values():
                # 每个产品组的首尾对象再执行精确 stat，证明目录列表不是陈旧缓存。
                for target in (group_targets[0], group_targets[-1]):
                    remote_path = str(target["remotePath"])
                    if remote_path in failed_paths:
                        continue
                    try:
                        transport.sock.settimeout(_deadline_remaining(deadline))
                        precise = sftp.stat(remote_path)
                        frozen = evidence_by_path[remote_path]
                        if (
                            precise.st_size != frozen["byteSize"]
                            or precise.st_mtime != frozen["remoteModifiedAtEpochSeconds"]
                        ):
                            raise ValueError("delivery changed between list and precise stat")
                    except (OSError, TimeoutError, ValueError):
                        failed_paths.add(remote_path)
                        evidence_by_path.pop(remote_path, None)
            for target in targets:
                remote_path = str(target["remotePath"])
                if remote_path not in evidence_by_path:
                    evidence_by_path[remote_path] = {
                        **target,
                        "available": False,
                        "byteSize": None,
                        "remoteModifiedAtEpochSeconds": None,
                        "availabilityReason": "SOURCE_MISSING",
                    }
            for delivery_kind, component in (
                ("DAILY_STATISTICS", "hkex-daily-statistics-deliveries"),
                ("SECURITIES_MASTER", "hkex-securities-master-deliveries"),
            ):
                selected = [
                    item
                    for item in evidence_by_path.values()
                    if item["deliveryKind"] == delivery_kind
                ]
                if not selected:
                    continue
                all_available = all(item["available"] is True for item in selected)
                accepted = all_available or delivery_kind == "SECURITIES_MASTER"
                results.append(
                    ProviderPreflightComponent(
                        component=component,
                        accepted=accepted,
                        reason=(
                            "ALL_DELIVERY_STATS_VERIFIED"
                            if all_available
                            else "SOURCE_MISSING_IDENTITY_DEGRADED"
                            if delivery_kind == "SECURITIES_MASTER"
                            else "DELIVERY_STAT_FAILED"
                        ),
                    )
                )
        except TimeoutError:
            results = [
                ProviderPreflightComponent(
                    component="sftp-authentication",
                    accepted=False,
                    reason="PROBE_DEADLINE_EXCEEDED",
                )
            ]
        except Exception as error:
            error_name = error.__class__.__name__
            if error_name == "AuthenticationException":
                reason = "SFTP_AUTHENTICATION_FAILED"
            elif error_name in {"BadHostKeyException", "SSHException"}:
                reason = "SFTP_HOST_KEY_OR_HANDSHAKE_FAILED"
            elif isinstance(error, ImportError):
                reason = "SFTP_CLIENT_UNAVAILABLE"
            else:
                reason = "SFTP_CONNECT_FAILED"
            results = [
                ProviderPreflightComponent(
                    component="sftp-authentication",
                    accepted=False,
                    reason=reason,
                )
            ]
        finally:
            if sftp is not None:
                sftp.close()
            if client is not None:
                client.close()
        if not evidence_by_path:
            evidence_by_path = {
                str(target["remotePath"]): {
                    **target,
                    "available": False,
                    "byteSize": None,
                    "remoteModifiedAtEpochSeconds": None,
                    "availabilityReason": "SOURCE_MISSING",
                }
                for target in targets
            }
        completed = {item.component for item in results}
        for delivery_kind, component in (
            ("DAILY_STATISTICS", "hkex-daily-statistics-deliveries"),
            ("SECURITIES_MASTER", "hkex-securities-master-deliveries"),
        ):
            if component in completed or not any(
                target["deliveryKind"] == delivery_kind for target in targets
            ):
                continue
            results.append(
                ProviderPreflightComponent(
                    component=component,
                    accepted=component == "hkex-securities-master-deliveries",
                    reason=(
                        "SOURCE_MISSING_IDENTITY_DEGRADED"
                        if component == "hkex-securities-master-deliveries"
                        else "DEPENDENCY_UNAVAILABLE"
                    ),
                )
            )
        sanitized_evidence = tuple(
            {key: value for key, value in item.items() if key != "component"}
            for _path, item in sorted(evidence_by_path.items())
        )
        return tuple(results), sanitized_evidence

    def _preflight_status_landings(
        self,
        probe_dates: _StockConnectProbeDates,
        *,
        deadline: float,
    ) -> tuple[tuple[ProviderPreflightComponent, ...], tuple[dict[str, object], ...]]:
        """校验清单声明的最终状态；未声明历史日期保留缺源警告，运营期 fail-closed。"""
        evidence: list[dict[str, object]] = []
        failed_groups: set[tuple[str, str]] = set()
        historical_gap_groups: set[tuple[str, str]] = set()
        for _component, channel, direction, trade_date in probe_dates.status_dates:
            required = trade_date >= self._status_manifest.required_from
            manifest_entry = self._status_manifest.entries.get((trade_date, channel, direction))
            if manifest_entry is None:
                reason = (
                    "REQUIRED_STATUS_MANIFEST_ENTRY_MISSING"
                    if required
                    else "STATUS_SOURCE_NOT_AVAILABLE_HISTORICAL"
                )
                if required:
                    failed_groups.add((channel, direction))
                else:
                    historical_gap_groups.add((channel, direction))
                evidence.append(
                    _unavailable_status_evidence(
                        channel=channel,
                        direction=direction,
                        trade_date=trade_date,
                        relative_path=None,
                        reason=reason,
                        required=required,
                    )
                )
                continue
            relative_path: str | None = None
            try:
                _deadline_remaining(deadline)
                if direction == "NORTHBOUND":
                    template = self._config.omdc_status_path_template
                    profile = self._config.omdc_profile
                    source_code = "HKEX_OMDC"
                    product_name = "HKEX OMD-C MMDH Stock Connect DQB"
                elif channel == "SH":
                    template = self._config.sse_status_path_template
                    profile = self._config.sse_profile
                    source_code = "SSE_MDGW"
                    product_name = "SSE IS117/IS124 trdses04 versioned gateway projection"
                else:
                    template = self._config.szse_status_path_template
                    profile = self._config.szse_profile
                    source_code = "SZSE_STEP"
                    product_name = "SZSE STEP Binary 390019 versioned gateway projection"
                expected_path = _resolve_delivery_path(
                    self._config.status_delivery_root,
                    template,
                    trade_date=trade_date,
                    channel=channel,
                )
                expected_relative = str(
                    expected_path.relative_to(self._config.status_delivery_root.resolve())
                )
                if (
                    manifest_entry.relative_path != expected_relative
                    or manifest_entry.profile_id != profile
                ):
                    raise ValueError("status coverage entry does not match approved delivery")
                path = _resolve_delivery_path(
                    self._config.status_delivery_root,
                    manifest_entry.relative_path,
                    trade_date=trade_date,
                    channel=channel,
                )
                relative_path = str(path.relative_to(self._config.status_delivery_root.resolve()))
                fetched = _read_delivery_file(
                    path,
                    source_code=source_code,
                    product_name=product_name,
                    max_bytes=self._config.max_delivery_bytes,
                )
                _validate_status_gateway_manifest(
                    path,
                    payload=fetched.payload,
                    profile=profile,
                    trade_date=trade_date,
                    channel=channel,
                    direction=direction,
                    max_manifest_bytes=self._config.max_manifest_bytes,
                )
                parsed = parse_stock_connect_status(
                    fetched.payload,
                    channel=channel,
                    direction=direction,
                    trade_date=trade_date,
                    profile=profile,
                    source_code=source_code,
                    product_name=product_name,
                    calendar_trading_day=True if direction == "NORTHBOUND" else None,
                    landing_final=True,
                )
                sidecar_path = path.with_suffix(path.suffix + ".manifest.json")
                sidecar_payload = _read_bounded_file(
                    sidecar_path,
                    max_bytes=self._config.max_manifest_bytes,
                )
                if (
                    _sha256(fetched.payload) != manifest_entry.payload_sha256
                    or _sha256(sidecar_payload) != manifest_entry.sidecar_sha256
                ):
                    raise ValueError("status delivery digest drifted from coverage manifest")
                _deadline_remaining(deadline)
            except TimeoutError:
                failed_groups.add((channel, direction))
                evidence.append(
                    _unavailable_status_evidence(
                        channel=channel,
                        direction=direction,
                        trade_date=trade_date,
                        relative_path=relative_path,
                        reason="PROBE_DEADLINE_EXCEEDED",
                        required=required,
                    )
                )
            except (OSError, ValueError, ProviderError):
                failed_groups.add((channel, direction))
                evidence.append(
                    _unavailable_status_evidence(
                        channel=channel,
                        direction=direction,
                        trade_date=trade_date,
                        relative_path=relative_path,
                        reason="FINAL_LANDING_VERIFICATION_FAILED",
                        required=required,
                    )
                )
            else:
                evidence.append(
                    {
                        "deliveryKind": "CHANNEL_STATUS",
                        "capability": CHANNEL_STATUS_CAPABILITY,
                        "channel": channel,
                        "direction": direction,
                        "tradeDate": trade_date.isoformat(),
                        "relativePath": relative_path,
                        "profileId": manifest_entry.profile_id,
                        "required": required,
                        "available": True,
                        "byteSize": len(fetched.payload),
                        "payloadSha256": str(parsed["sourceFileSha256"]),
                        "sidecarSha256": _sha256(sidecar_payload),
                        "finality": "END_OF_DAY_FINAL",
                    }
                )
        checks = tuple(
            ProviderPreflightComponent(
                component=f"status-{channel.lower()}-{direction.lower()}-deliveries",
                accepted=(channel, direction) not in failed_groups,
                reason=(
                    "FINAL_LANDING_VERIFICATION_FAILED"
                    if (channel, direction) in failed_groups
                    else "APPROVED_WITH_HISTORICAL_STATUS_GAPS"
                    if (channel, direction) in historical_gap_groups
                    else "ALL_DECLARED_END_OF_DAY_FINAL_DELIVERIES_VERIFIED"
                ),
            )
            for channel, direction in sorted(
                {
                    (channel, direction)
                    for _component, channel, direction, _trade_date in probe_dates.status_dates
                }
            )
        )
        return checks, tuple(evidence)

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """读取并标准化一项官方交付；任何缺文件、鉴权或 schema 漂移均 fail-closed。"""
        if request.capability not in _CAPABILITIES:
            raise _provider_error(
                ProviderErrorCode.INVALID_REQUEST,
                "unsupported official stock-connect capability",
                retryable=False,
            )
        parameters = _parameters(request)
        try:
            if request.capability in {MARKET_STAT_CAPABILITY, ACTIVE_SECURITY_CAPABILITY}:
                return await asyncio.to_thread(
                    self._daily_statistics_batch, request.capability, parameters
                )
            if request.capability == TRADING_CALENDAR_CAPABILITY:
                return await asyncio.to_thread(self._calendar_batch, parameters)
            if request.capability == INSTRUMENT_MASTER_CAPABILITY:
                return await asyncio.to_thread(self._master_batch, parameters)
            return await asyncio.to_thread(self._status_batch, parameters)
        except ProviderError:
            raise
        except OSError as error:
            raise _provider_error(
                ProviderErrorCode.UNAVAILABLE,
                "official stock-connect delivery is unavailable",
                retryable=True,
            ) from error
        except (ValueError, csv.Error, InvalidOperation) as error:
            raise _provider_error(
                ProviderErrorCode.SCHEMA,
                "official stock-connect delivery cannot be parsed",
                retryable=False,
            ) from error

    def _daily_statistics_batch(
        self, capability: str, parameters: Mapping[str, str]
    ) -> ProviderBatch:
        """解析单日 HKEX licensed CSV，并按 capability 输出市场或活跃榜标准载荷。"""
        channel = _channel(parameters)
        direction = _direction(parameters)
        trade_date = _single_trade_date(parameters)
        fetched = self._cached_daily_file(channel, trade_date)
        market, active = parse_hkex_daily_statistics(
            fetched.payload,
            channel=channel,
            direction=direction,
            trade_date=trade_date,
            profile=self._config.daily_statistics_profile,
        )
        regime = _daily_statistics_regime(
            trade_date=trade_date,
            direction=direction,
        )
        observed_at = self._now()
        if capability == MARKET_STAT_CAPABILITY:
            normalized = {
                "schema": "quant-v2.stock-connect-market-daily.v1",
                "channel": channel,
                "direction": direction,
                "profileId": regime.profile_id,
                "valueKind": "REPORTED",
                "productName": fetched.product_name,
                "sourcePublicationAvailability": "NOT_PROVIDED_BY_SOURCE",
                "sourcePublicationAt": None,
                "sourceObservedAt": _timestamp(observed_at),
                "sourceFileSha256": _sha256(fetched.payload),
                "records": [market],
            }
        else:
            normalized = {
                "schema": "quant-v2.stock-connect-active-security.v1",
                "channel": channel,
                "direction": direction,
                "profileId": regime.profile_id,
                "valueKind": "REPORTED",
                "productName": fetched.product_name,
                "sourcePublicationAvailability": "NOT_PROVIDED_BY_SOURCE",
                "sourcePublicationAt": None,
                "sourceObservedAt": _timestamp(observed_at),
                "sourceFileSha256": _sha256(fetched.payload),
                "records": active,
            }
        return _batch(
            capability=capability,
            normalized=normalized,
            fetched=fetched,
            observed_at=observed_at,
        )

    def _calendar_batch(self, parameters: Mapping[str, str]) -> ProviderBatch:
        """下载并解析指定年度 HKEX Stock Connect 官方 CSV 日历。"""
        year = _year(parameters)
        fetched, records = self._calendar_delivery(year)
        observed_at = self._now()
        publication_at = _required_publication_at(fetched)
        normalized = {
            "schema": "quant-v2.stock-connect-calendar.v1",
            "year": year,
            "productName": fetched.product_name,
            "sourcePublicationAvailability": "REPORTED",
            "sourcePublicationAt": _timestamp(publication_at),
            "sourceObservedAt": _timestamp(observed_at),
            "sourceFileSha256": _sha256(fetched.payload),
            "records": records,
        }
        return _batch(
            capability=TRADING_CALENDAR_CAPABILITY,
            normalized=normalized,
            fetched=fetched,
            observed_at=observed_at,
        )

    def _master_batch(self, parameters: Mapping[str, str]) -> ProviderBatch:
        """读取 T+1 fixed-length 主档，并分别校验 issued date 与 effective trade date。"""
        trade_date = _single_trade_date(parameters)
        master_profile = self._master_profiles.select(trade_date)
        issued_date = self._master_issued_date(trade_date)
        remote_path = _format_remote_path(
            self._config.securities_master_path_template,
            trade_date=trade_date,
            channel="HKEX",
            issued_date=issued_date,
        )
        entitlement = self._sftp_delivery_manifest.entries.get(remote_path)
        if entitlement is None or entitlement.available_until <= self._now():
            raise _provider_error(
                ProviderErrorCode.UNAVAILABLE,
                "HKEX Securities Master delivery is unavailable",
                retryable=False,
            )
        fetched = self._sftp_reader(remote_path)
        records = parse_hkex_securities_master(
            fetched.payload,
            effective_trade_date=trade_date,
            expected_issued_date=issued_date,
            product_name=fetched.product_name,
            profile=self._config.securities_master_profile,
            layout=master_profile,
        )
        observed_at = self._now()
        normalized = {
            "schema": "quant-v2.stock-connect-instrument-master.v2",
            "issuedDate": issued_date.isoformat(),
            "effectiveTradeDate": trade_date.isoformat(),
            "profileId": master_profile.profile_id,
            "specificationReference": master_profile.specification_reference,
            "productName": fetched.product_name,
            "sourcePublicationAvailability": "NOT_PROVIDED_BY_SOURCE",
            "sourcePublicationAt": None,
            "sourceObservedAt": _timestamp(observed_at),
            "sourceFileSha256": _sha256(fetched.payload),
            "records": records,
        }
        return _batch(
            capability=INSTRUMENT_MASTER_CAPABILITY,
            normalized=normalized,
            fetched=fetched,
            observed_at=observed_at,
        )

    def _calendar_delivery(self, year: int) -> tuple[_FetchedObject, list[dict[str, object]]]:
        """缓存年度官方日历对象，供公开日历能力和 T+1 主档路径共同使用。"""
        with self._cache_lock:
            cached = self._calendar_cache.get(year)
        if cached is not None:
            return cached
        fetched = self._calendar_object(
            year,
            timeout_seconds=self._config.request_timeout_seconds,
        )
        records = parse_hkex_stock_connect_calendar(
            fetched.payload,
            year=year,
            profile=self._config.calendar_profile,
        )
        result = (fetched, records)
        with self._cache_lock:
            self._calendar_cache[year] = result
        return result

    def _calendar_object(self, year: int, *, timeout_seconds: float) -> _FetchedObject:
        """按逐年摘要清单取得官方日历；缺年份、404 和本地散列漂移均 fail-closed。"""
        entry = self._calendar_manifest.get(year)
        if entry is None:
            raise ValueError("HKEX calendar manifest does not cover the requested year")
        if entry.source_kind == "LOCAL_ARCHIVE":
            assert entry.relative_path is not None
            path = _resolve_manifest_relative_file(
                self._config.calendar_manifest_path,
                entry.relative_path,
            )
            payload = _read_bounded_file(
                path,
                max_bytes=self._config.max_delivery_bytes,
            )
            product_name = path.name
        else:
            url = (
                self._config.calendar_url_template.format(year=year)
                if entry.source_kind == "HTTPS_TEMPLATE"
                else entry.url
            )
            assert url is not None
            _validate_hkex_https_url(url)
            if self._custom_https_reader is None:
                current = self._read_https(url, timeout_seconds=timeout_seconds)
            else:
                current = self._custom_https_reader(url)
            payload = current.payload
            product_name = current.product_name
            if (
                current.published_at is not None
                and current.published_at != entry.source_publication_at
            ):
                raise ValueError("HKEX calendar publication time drifted from manifest")
        if _sha256(payload) != entry.payload_sha256:
            raise ValueError("HKEX calendar payload digest drifted from manifest")
        return _FetchedObject(
            payload=payload,
            content_type="text/csv",
            published_at=entry.source_publication_at,
            product_name=product_name,
            upstream_source="HKEX_CALENDAR",
        )

    def _master_issued_date(self, effective_trade_date: date) -> date:
        """用官方香港开市日找出 T+1 主档的 issued date，禁止按工作日猜测。"""
        records: list[dict[str, object]] = []
        for year in {effective_trade_date.year - 1, effective_trade_date.year}:
            records.extend(self._calendar_delivery(year)[1])
        open_dates = sorted(
            date.fromisoformat(str(item["calendarDate"]))
            for item in records
            if str(item["hongKongState"]).strip().lower() not in {"closed", "holiday"}
        )
        if effective_trade_date not in open_dates:
            raise ValueError("HKEX Securities Master effective date is not a Hong Kong trading day")
        candidates = [item for item in open_dates if item < effective_trade_date]
        if not candidates:
            raise ValueError("HKEX Securities Master issued date is unavailable")
        return candidates[-1]

    def _status_batch(self, parameters: Mapping[str, str]) -> ProviderBatch:
        """解析日终 OMD-C、MDGW 或 STEP 落地对象，不把落地时间当交易日。"""
        channel = _channel(parameters)
        direction = _direction(parameters)
        trade_date = _single_trade_date(parameters)
        manifest_entry = self._status_manifest.entries.get((trade_date, channel, direction))
        if manifest_entry is None:
            if trade_date >= self._status_manifest.required_from:
                raise ValueError("required stock-connect status manifest entry is unavailable")
            return self._historical_missing_status_batch(
                channel=channel,
                direction=direction,
                trade_date=trade_date,
            )
        if direction == "NORTHBOUND":
            template = self._config.omdc_status_path_template
            profile = self._config.omdc_profile
            source_code = "HKEX_OMDC"
            product_name = "HKEX OMD-C MMDH Stock Connect DQB"
        elif channel == "SH":
            template = self._config.sse_status_path_template
            profile = self._config.sse_profile
            source_code = "SSE_MDGW"
            product_name = "SSE IS117/IS124 trdses04 versioned gateway projection"
        else:
            template = self._config.szse_status_path_template
            profile = self._config.szse_profile
            source_code = "SZSE_STEP"
            product_name = "SZSE STEP Binary 390019 versioned gateway projection"
        expected_path = _resolve_delivery_path(
            self._config.status_delivery_root,
            template,
            trade_date=trade_date,
            channel=channel,
        )
        expected_relative = str(
            expected_path.relative_to(self._config.status_delivery_root.resolve())
        )
        if (
            manifest_entry.relative_path != expected_relative
            or manifest_entry.profile_id != profile
        ):
            raise ValueError("status coverage entry does not match approved delivery")
        path = _resolve_delivery_path(
            self._config.status_delivery_root,
            manifest_entry.relative_path,
            trade_date=trade_date,
            channel=channel,
        )
        fetched = _read_delivery_file(
            path,
            source_code=source_code,
            product_name=product_name,
            max_bytes=self._config.max_delivery_bytes,
        )
        _validate_status_gateway_manifest(
            path,
            payload=fetched.payload,
            profile=profile,
            trade_date=trade_date,
            channel=channel,
            direction=direction,
            max_manifest_bytes=self._config.max_manifest_bytes,
        )
        sidecar_payload = _read_bounded_file(
            path.with_suffix(path.suffix + ".manifest.json"),
            max_bytes=self._config.max_manifest_bytes,
        )
        if (
            _sha256(fetched.payload) != manifest_entry.payload_sha256
            or _sha256(sidecar_payload) != manifest_entry.sidecar_sha256
        ):
            raise ValueError("status delivery digest drifted from coverage manifest")
        calendar_trading_day: bool | None = None
        if direction == "NORTHBOUND":
            _calendar_object, calendar_records = self._calendar_delivery(trade_date.year)
            matches = [
                item for item in calendar_records if item["calendarDate"] == trade_date.isoformat()
            ]
            if len(matches) != 1 or matches[0]["northboundTrading"] is not True:
                raise ValueError(
                    "OMD-C status requires an open day confirmed by the official calendar"
                )
            calendar_trading_day = True
        status = parse_stock_connect_status(
            fetched.payload,
            channel=channel,
            direction=direction,
            trade_date=trade_date,
            profile=profile,
            source_code=source_code,
            product_name=product_name,
            calendar_trading_day=calendar_trading_day,
            landing_final=True,
        )
        observed_at = self._now()
        normalized = {
            "schema": "quant-v2.stock-connect-channel-status.v1",
            "channel": channel,
            "direction": direction,
            "productName": fetched.product_name,
            "sourcePublicationAvailability": "REPORTED",
            "sourcePublicationAt": status["sourcePublicationAt"],
            "sourceObservedAt": _timestamp(observed_at),
            "sourceFileSha256": _sha256(fetched.payload),
            "records": [status],
        }
        return _batch(
            capability=CHANNEL_STATUS_CAPABILITY,
            normalized=normalized,
            fetched=fetched,
            observed_at=observed_at,
        )

    def _historical_missing_status_batch(
        self,
        *,
        channel: str,
        direction: str,
        trade_date: date,
    ) -> ProviderBatch:
        """发布清单确认的历史缺源状态，不制造会话、额度、publication 或来源摘要。"""
        observed_at = self._status_manifest.observed_at
        record = {
            "tradeDate": trade_date.isoformat(),
            "tradingDay": True,
            "sessionState": "UNKNOWN",
            "sessionAvailability": "SOURCE_MISSING",
            "buyOrderAccepted": None,
            "sellOrderAccepted": None,
            "quotaState": "SOURCE_MISSING",
            "quotaBalance": None,
            "quotaCurrency": "CNY",
            "observedAt": _timestamp(observed_at),
            "sourceCode": "STATUS_COVERAGE_MANIFEST",
            "productName": "Stock Connect historical status coverage manifest",
            "sourcePublicationAt": None,
            "sourceFileSha256": None,
        }
        normalized = {
            "schema": "quant-v2.stock-connect-channel-status.v1",
            "channel": channel,
            "direction": direction,
            "productName": record["productName"],
            "sourcePublicationAvailability": "NOT_PROVIDED_BY_SOURCE",
            "sourcePublicationAt": None,
            "sourceObservedAt": _timestamp(observed_at),
            "sourceFileSha256": None,
            "records": [record],
        }
        return _batch(
            capability=CHANNEL_STATUS_CAPABILITY,
            normalized=normalized,
            fetched=_FetchedObject(
                payload=b"",
                content_type="application/octet-stream",
                published_at=None,
                product_name=str(record["productName"]),
                upstream_source="STATUS_COVERAGE_MANIFEST",
            ),
            observed_at=observed_at,
        )

    def _cached_daily_file(self, channel: str, trade_date: date) -> _FetchedObject:
        """复用同次进程内市场统计与活跃榜的同一官方对象，摘要始终一致。"""
        key = (channel, trade_date)
        with self._cache_lock:
            cached = self._daily_cache.get(key)
        if cached is not None:
            return cached
        template = (
            self._config.sh_daily_path_template
            if channel == "SH"
            else self._config.sz_daily_path_template
        )
        remote_path = _format_remote_path(template, trade_date=trade_date, channel=channel)
        fetched = self._sftp_reader(remote_path)
        with self._cache_lock:
            self._daily_cache[key] = fetched
        return fetched

    def _read_sftp(self, remote_path: str) -> _FetchedObject:
        """用注册邮箱、私钥和 known_hosts 严格读取 HKEX Data Marketplace 对象。"""
        if not self._config.sftp_private_key_path.is_file():
            raise _provider_error(
                ProviderErrorCode.AUTHENTICATION,
                "HKEX SFTP private key is unavailable",
                retryable=False,
            )
        if not self._config.sftp_known_hosts_path.is_file():
            raise _provider_error(
                ProviderErrorCode.AUTHENTICATION,
                "HKEX SFTP known-hosts file is unavailable",
                retryable=False,
            )
        try:
            import paramiko

            client = paramiko.SSHClient()
            client.load_host_keys(str(self._config.sftp_known_hosts_path))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.connect(
                hostname=self._config.sftp_host,
                port=self._config.sftp_port,
                username=self._config.sftp_username,
                key_filename=str(self._config.sftp_private_key_path),
                passphrase=self._config.sftp_private_key_passphrase,
                timeout=self._config.request_timeout_seconds,
                banner_timeout=self._config.request_timeout_seconds,
                auth_timeout=self._config.request_timeout_seconds,
                allow_agent=False,
                look_for_keys=False,
            )
            try:
                sftp = client.open_sftp()
                try:
                    file_stat = sftp.stat(remote_path)
                    if not isinstance(file_stat.st_size, int):
                        raise _provider_error(
                            ProviderErrorCode.SCHEMA,
                            "HKEX SFTP delivery size is unavailable",
                            retryable=False,
                        )
                    if (
                        file_stat.st_size <= 0
                        or file_stat.st_size > self._config.max_delivery_bytes
                    ):
                        raise _provider_error(
                            ProviderErrorCode.SCHEMA,
                            "HKEX SFTP delivery exceeds the approved byte limit",
                            retryable=False,
                        )
                    with sftp.open(remote_path, "rb") as stream:
                        payload = bytes(stream.read(self._config.max_delivery_bytes + 1))
                finally:
                    sftp.close()
            finally:
                client.close()
        except ProviderError:
            raise
        except Exception as error:
            code = (
                ProviderErrorCode.AUTHENTICATION
                if error.__class__.__name__ in {"AuthenticationException", "BadHostKeyException"}
                else ProviderErrorCode.UNAVAILABLE
            )
            raise _provider_error(
                code,
                "HKEX SFTP delivery is unavailable",
                retryable=code == ProviderErrorCode.UNAVAILABLE,
            ) from error
        if not payload:
            raise _provider_error(
                ProviderErrorCode.UNAVAILABLE,
                "HKEX SFTP delivery is empty",
                retryable=True,
            )
        if len(payload) > self._config.max_delivery_bytes:
            raise _provider_error(
                ProviderErrorCode.SCHEMA,
                "HKEX SFTP delivery exceeds the approved byte limit",
                retryable=False,
            )
        unpacked, content_type = _unpack_delivery(
            payload,
            remote_path,
            max_delivery_bytes=self._config.max_delivery_bytes,
            max_compression_ratio=self._config.max_zip_compression_ratio,
        )
        return _FetchedObject(
            payload=unpacked,
            content_type=content_type,
            # Data Marketplace 的 SFTP mtime 仅表示交付元数据，不能冒充官方 publication。
            published_at=None,
            product_name=PurePosixPath(remote_path).name,
            upstream_source="HKEX_DATA_MARKETPLACE",
        )

    def _read_https(
        self,
        url: str,
        *,
        timeout_seconds: float | None = None,
    ) -> _FetchedObject:
        """通过 HTTPS 获取公开 HKEX 参考文件，禁止重定向到非 HTTPS 目标。"""
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/csv,application/octet-stream",
                "User-Agent": "quant-v2-service-data-sync/1.0",
            },
        )
        try:
            with urllib.request.urlopen(  # noqa: S310  # URL 已由配置校验为 HTTPS。
                request,
                timeout=min(
                    float(self._config.request_timeout_seconds),
                    timeout_seconds
                    if timeout_seconds is not None
                    else float(self._config.request_timeout_seconds),
                ),
            ) as response:
                final_url = response.geturl()
                if not final_url.startswith("https://"):
                    raise ValueError("HKEX reference redirect left HTTPS")
                payload = response.read(self._config.max_delivery_bytes + 1)
                last_modified = response.headers.get("Last-Modified")
                content_type = response.headers.get_content_type()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise _provider_error(
                ProviderErrorCode.UNAVAILABLE,
                "HKEX reference delivery is unavailable",
                retryable=True,
            ) from error
        if not payload:
            raise _provider_error(
                ProviderErrorCode.UNAVAILABLE,
                "HKEX reference delivery is empty",
                retryable=True,
            )
        if len(payload) > self._config.max_delivery_bytes:
            raise _provider_error(
                ProviderErrorCode.SCHEMA,
                "HKEX reference delivery exceeds the approved byte limit",
                retryable=False,
            )
        published_at = _http_datetime(last_modified)
        if published_at is None:
            raise ValueError("HKEX reference delivery has no official publication metadata")
        return _FetchedObject(
            payload=payload,
            content_type=content_type,
            published_at=published_at,
            product_name=PurePosixPath(urllib.parse.urlparse(url).path).name,
            upstream_source="HKEX_CALENDAR",
        )


def parse_hkex_daily_statistics(
    payload: bytes,
    *,
    channel: str,
    direction: str,
    trade_date: date,
    profile: str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """按冻结 HKEX CSV profile 解析市场统计和官方 Top 10，不接受预标准化 JSON。"""
    _require_profile(profile, "hkex-daily-statistics-v1")
    rows = _csv_rows(payload)
    market_values: dict[str, str] = {}
    active: list[dict[str, object]] = []
    current_direction: str | None = None
    active_section = False
    header: dict[str, int] | None = None
    header_units: dict[str, Decimal] = {}
    for row in rows:
        cells = [cell.strip() for cell in row]
        joined = " ".join(cells).lower()
        detected_direction = _direction_from_text(joined)
        if detected_direction is not None:
            current_direction = detected_direction
        if "top 10" in joined or "most active" in joined or "actively traded" in joined:
            active_section = True
            header = None
            continue
        if "trading statistics" in joined or "market statistics" in joined:
            active_section = False
            header = None
            continue
        candidate = _header_map(cells)
        if candidate is not None:
            header = candidate
            header_units = {
                field: _unit_multiplier(cells[index])
                for field, index in candidate.items()
                if field in {"buy", "sell", "turnover", "etf_turnover"}
            }
            continue
        if header is not None and _row_has_values(cells, header):
            row_direction = _value(cells, header.get("direction"))
            normalized_direction = _direction_from_text(row_direction.lower())
            effective_direction = normalized_direction or current_direction
            if effective_direction != direction:
                continue
            code = _normalize_security_code(_value(cells, header.get("code")))
            rank_text = _value(cells, header.get("rank"))
            is_active = active_section or bool(code) or bool(rank_text)
            if is_active:
                if not code:
                    continue
                rank = int(rank_text) if rank_text else len(active) + 1
                active.append(
                    _active_record_from_columns(
                        cells,
                        header,
                        header_units,
                        trade_date=trade_date,
                        direction=direction,
                        rank=rank,
                        code=code,
                    )
                )
            else:
                _merge_market_columns(market_values, cells, header, header_units)
            continue
        if current_direction != direction:
            continue
        if not active_section:
            _merge_market_key_value(market_values, cells)
    if not market_values:
        raise ValueError("HKEX daily statistics has no market section for requested direction")
    market = _market_record(market_values, trade_date=trade_date, direction=direction)
    if len(active) > 10 or len({_active_rank(item) for item in active}) != len(active):
        raise ValueError("HKEX active-security ranking is invalid")
    turnover = Decimal(str(market["turnoverAmount"]))
    if turnover > 0 and not active:
        raise ValueError("HKEX daily statistics omitted active securities for non-zero turnover")
    return market, sorted(active, key=_active_rank)


def parse_hkex_stock_connect_calendar(
    payload: bytes, *, year: int, profile: str
) -> list[dict[str, object]]:
    """解析 HKEX 年度 CSV；空交易列表示开放，Closed 表示关闭，Half Day 仍是交易日。"""
    _require_profile(profile, "hkex-calendar-v1")
    rows = _csv_rows(payload)
    header_index = next(
        (
            index
            for index, row in enumerate(rows)
            if {_normalize_header(cell) for cell in row}
            >= {"date", "northboundtrading", "southboundtrading"}
        ),
        None,
    )
    if header_index is None:
        raise ValueError("HKEX calendar header is unavailable")
    header = {_normalize_header(cell): index for index, cell in enumerate(rows[header_index])}
    records: list[dict[str, object]] = []
    for row in rows[header_index + 1 :]:
        value = _value(row, header.get("date"))
        if not value:
            continue
        calendar_date = date.fromisoformat(value)
        if calendar_date.year != year:
            raise ValueError("HKEX calendar contains an unexpected year")
        north = _value(row, header.get("northboundtrading"))
        south = _value(row, header.get("southboundtrading"))
        records.append(
            {
                "calendarDate": calendar_date.isoformat(),
                "northboundTrading": north.lower() not in {"closed", "holiday"},
                "southboundTrading": south.lower() not in {"closed", "holiday"},
                "hongKongState": _value(row, header.get("hongkong")) or "OPEN",
                "mainlandState": _value(row, header.get("shanghaishenzhen"))
                or _value(row, header.get("shanghaishenzhenmarket"))
                or "OPEN",
            }
        )
    if len(records) < 200 or len({item["calendarDate"] for item in records}) != len(records):
        raise ValueError("HKEX calendar coverage is incomplete or duplicated")
    return records


def _load_calendar_manifest(
    path: Path,
    *,
    max_bytes: int,
) -> Mapping[int, _CalendarManifestEntry]:
    """读取受控年度日历清单；历史年份必须逐年钉住官方对象，不能依赖 URL 猜测。"""
    if not path.is_file():
        raise ValueError("HKEX calendar manifest is unavailable")
    payload = _read_bounded_file(path, max_bytes=max_bytes)
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("HKEX calendar manifest is invalid JSON") from error
    _require_exact_mapping_keys(
        raw,
        required={"manifestSchema", "entries"},
        label="HKEX calendar manifest",
    )
    if raw["manifestSchema"] != _CALENDAR_MANIFEST_SCHEMA:
        raise ValueError("HKEX calendar manifest schema is unsupported")
    entries_raw = raw["entries"]
    if not isinstance(entries_raw, list) or not entries_raw:
        raise ValueError("HKEX calendar manifest entries are unavailable")
    entries: dict[int, _CalendarManifestEntry] = {}
    for item in entries_raw:
        _require_exact_mapping_keys(
            item,
            required={
                "year",
                "sourceKind",
                "url",
                "relativePath",
                "sha256",
                "sourcePublicationAt",
                "observedAt",
            },
            label="HKEX calendar manifest entry",
        )
        assert isinstance(item, dict)
        year = item["year"]
        if isinstance(year, bool) or not isinstance(year, int) or not 2013 <= year <= 2100:
            raise ValueError("HKEX calendar manifest year is invalid")
        if year in entries:
            raise ValueError("HKEX calendar manifest year is duplicated")
        source_kind = item["sourceKind"]
        url_value = item["url"]
        relative_value = item["relativePath"]
        if source_kind == "HTTPS_TEMPLATE":
            if url_value is not None or relative_value is not None:
                raise ValueError("HKEX template calendar entry must not override a location")
            if year != datetime.now(UTC).year:
                raise ValueError("HKEX historical calendar cannot use a URL template")
            url: str | None = None
            relative_path: str | None = None
        elif source_kind == "HTTPS_OBJECT":
            if not isinstance(url_value, str) or relative_value is not None:
                raise ValueError("HKEX HTTPS calendar entry location is invalid")
            _validate_hkex_https_url(url_value)
            url = url_value
            relative_path = None
        elif source_kind == "LOCAL_ARCHIVE":
            if url_value is not None or not isinstance(relative_value, str):
                raise ValueError("HKEX local calendar entry location is invalid")
            _validate_safe_relative_path(relative_value, label="HKEX calendar")
            url = None
            relative_path = relative_value
        else:
            raise ValueError("HKEX calendar source kind is unsupported")
        entries[year] = _CalendarManifestEntry(
            year=year,
            source_kind=source_kind,
            url=url,
            relative_path=relative_path,
            payload_sha256=_lowercase_sha256(item["sha256"], label="HKEX calendar"),
            source_publication_at=_manifest_datetime(
                item["sourcePublicationAt"],
                label="HKEX calendar publication",
            ),
            observed_at=_manifest_datetime(
                item["observedAt"],
                label="HKEX calendar observation",
            ),
        )
    return entries


def _load_sftp_delivery_manifest(
    path: Path,
    *,
    max_bytes_per_page: int,
) -> _SftpDeliveryManifest:
    """读取摘要分页 entitlement；每个历史订单对象必须带可用截止与订单引用。"""
    if not path.is_file():
        raise ValueError("HKEX SFTP delivery manifest is unavailable")
    header_payload = _read_bounded_file(path, max_bytes=max_bytes_per_page)
    try:
        raw = json.loads(header_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("HKEX SFTP delivery manifest is invalid JSON") from error
    _require_exact_mapping_keys(
        raw,
        required={"manifestSchema", "rootHash", "pages"},
        label="HKEX SFTP delivery manifest",
    )
    if raw["manifestSchema"] != _SFTP_DELIVERY_MANIFEST_SCHEMA:
        raise ValueError("HKEX SFTP delivery manifest schema is unsupported")
    root_hash = _lowercase_sha256(raw["rootHash"], label="HKEX SFTP manifest root")
    pages_raw = raw["pages"]
    if not isinstance(pages_raw, list) or not pages_raw:
        raise ValueError("HKEX SFTP delivery manifest pages are unavailable")
    normalized_pages: list[dict[str, int | str]] = []
    entries: dict[str, _SftpDeliveryEntitlement] = {}
    seen_page_numbers: set[int] = set()
    for page_ref in pages_raw:
        _require_exact_mapping_keys(
            page_ref,
            required={"pageNo", "relativePath", "sha256"},
            label="HKEX SFTP delivery page reference",
        )
        assert isinstance(page_ref, dict)
        page_no = page_ref["pageNo"]
        if (
            isinstance(page_no, bool)
            or not isinstance(page_no, int)
            or page_no < 0
            or page_no in seen_page_numbers
        ):
            raise ValueError("HKEX SFTP delivery page number is invalid")
        seen_page_numbers.add(page_no)
        relative_path = _strict_string(
            page_ref["relativePath"],
            label="HKEX SFTP delivery page path",
        )
        _validate_safe_relative_path(relative_path, label="HKEX SFTP delivery page")
        expected_digest = _lowercase_sha256(
            page_ref["sha256"],
            label="HKEX SFTP delivery page",
        )
        page_path = _resolve_manifest_relative_file(path, relative_path)
        page_payload = _read_bounded_file(
            page_path,
            max_bytes=max_bytes_per_page,
        )
        if _sha256(page_payload) != expected_digest:
            raise ValueError("HKEX SFTP delivery page digest mismatch")
        try:
            page = json.loads(page_payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("HKEX SFTP delivery page is invalid JSON") from error
        _require_exact_mapping_keys(
            page,
            required={"pageSchema", "pageNo", "entries"},
            label="HKEX SFTP delivery page",
        )
        if (
            page["pageSchema"] != _SFTP_DELIVERY_PAGE_SCHEMA
            or page["pageNo"] != page_no
            or not isinstance(page["entries"], list)
        ):
            raise ValueError("HKEX SFTP delivery page envelope is invalid")
        for item in page["entries"]:
            _require_exact_mapping_keys(
                item,
                required={"remotePath", "orderReference", "availableUntil"},
                label="HKEX SFTP delivery entitlement",
            )
            assert isinstance(item, dict)
            remote_path = _strict_string(
                item["remotePath"],
                label="HKEX SFTP remote path",
            )
            pure_path = PurePosixPath(remote_path)
            if not pure_path.parts or ".." in pure_path.parts:
                raise ValueError("HKEX SFTP entitlement path is unsafe")
            if remote_path in entries:
                raise ValueError("HKEX SFTP entitlement path is duplicated")
            entries[remote_path] = _SftpDeliveryEntitlement(
                remote_path=remote_path,
                order_reference=_strict_string(
                    item["orderReference"],
                    label="HKEX SFTP order reference",
                ),
                available_until=_manifest_datetime(
                    item["availableUntil"],
                    label="HKEX SFTP availableUntil",
                ),
            )
        normalized_pages.append(
            {
                "pageNo": page_no,
                "relativePath": relative_path,
                "sha256": expected_digest,
            }
        )
    ordered_pages = sorted(normalized_pages, key=lambda item: int(item["pageNo"]))
    if [item["pageNo"] for item in ordered_pages] != list(range(len(ordered_pages))):
        raise ValueError("HKEX SFTP delivery pages are not contiguous")
    if _canonical_json_sha256({"pages": ordered_pages}) != root_hash:
        raise ValueError("HKEX SFTP delivery manifest root hash mismatch")
    return _SftpDeliveryManifest(
        root_hash=root_hash,
        page_count=len(ordered_pages),
        entries=entries,
    )


def _load_status_coverage_manifest(
    path: Path,
    *,
    expected_required_from: date | None,
    max_bytes: int,
) -> _StatusCoverageManifest:
    """读取状态覆盖清单；未声明历史日期与运营期缺件使用不同失败语义。"""
    if expected_required_from is None or not path.is_file():
        raise ValueError("stock-connect status coverage manifest is unavailable")
    payload = _read_bounded_file(path, max_bytes=max_bytes)
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("stock-connect status coverage manifest is invalid JSON") from error
    _require_exact_mapping_keys(
        raw,
        required={"manifestSchema", "requiredFrom", "observedAt", "entries"},
        label="stock-connect status coverage manifest",
    )
    if raw["manifestSchema"] != _STATUS_MANIFEST_SCHEMA:
        raise ValueError("stock-connect status coverage manifest schema is unsupported")
    assert isinstance(raw, dict)
    required_from = date.fromisoformat(_strict_string(raw["requiredFrom"], label="requiredFrom"))
    if required_from != expected_required_from:
        raise ValueError("stock-connect status required-from boundary does not match settings")
    if required_from > datetime.now(ZoneInfo("Asia/Shanghai")).date():
        raise ValueError("stock-connect status required-from boundary cannot be in the future")
    observed_at = _manifest_datetime(
        raw["observedAt"],
        label="stock-connect status coverage observation",
    )
    entries_raw = raw["entries"]
    if not isinstance(entries_raw, list):
        raise ValueError("stock-connect status coverage entries are invalid")
    entries: dict[tuple[date, str, str], _StatusManifestEntry] = {}
    for item in entries_raw:
        _require_exact_mapping_keys(
            item,
            required={
                "tradeDate",
                "channel",
                "direction",
                "relativePath",
                "profileId",
                "payloadSha256",
                "sidecarSha256",
            },
            label="stock-connect status coverage entry",
        )
        assert isinstance(item, dict)
        trade_date = date.fromisoformat(_strict_string(item["tradeDate"], label="status tradeDate"))
        channel = _strict_string(item["channel"], label="status channel")
        direction = _strict_string(item["direction"], label="status direction")
        if channel not in {"SH", "SZ"} or direction not in {
            "NORTHBOUND",
            "SOUTHBOUND",
        }:
            raise ValueError("stock-connect status coverage identity is invalid")
        relative_path = _strict_string(item["relativePath"], label="status relativePath")
        _validate_safe_relative_path(relative_path, label="stock-connect status")
        profile_id = _strict_string(item["profileId"], label="status profileId")
        if profile_id not in _SCHEMA_PROFILES:
            raise ValueError("stock-connect status manifest profile is unsupported")
        key = (trade_date, channel, direction)
        if key in entries:
            raise ValueError("stock-connect status coverage target is duplicated")
        entries[key] = _StatusManifestEntry(
            trade_date=trade_date,
            channel=channel,
            direction=direction,
            relative_path=relative_path,
            profile_id=profile_id,
            payload_sha256=_lowercase_sha256(
                item["payloadSha256"],
                label="stock-connect status payload",
            ),
            sidecar_sha256=_lowercase_sha256(
                item["sidecarSha256"],
                label="stock-connect status sidecar",
            ),
        )
    return _StatusCoverageManifest(
        observed_at=observed_at,
        required_from=required_from,
        manifest_sha256=_sha256(payload),
        entries=entries,
    )


def validate_stock_connect_calendar_manifest(
    path: Path,
    *,
    max_bytes: int,
) -> StockConnectManifestValidation:
    """离线验证年度日历清单 exact schema，并返回摘要与年度数。"""
    entries = _load_calendar_manifest(path, max_bytes=max_bytes)
    return StockConnectManifestValidation(
        manifest_kind="calendar",
        sha256=_sha256(_read_bounded_file(path, max_bytes=max_bytes)),
        entry_count=len(entries),
    )


def validate_stock_connect_sftp_delivery_manifest(
    path: Path,
    *,
    max_bytes_per_page: int,
) -> StockConnectManifestValidation:
    """离线验证 SFTP header、连续页面、页面摘要、exact schema 与 canonical root。"""
    manifest = _load_sftp_delivery_manifest(
        path,
        max_bytes_per_page=max_bytes_per_page,
    )
    return StockConnectManifestValidation(
        manifest_kind="sftp-delivery",
        sha256=_sha256(_read_bounded_file(path, max_bytes=max_bytes_per_page)),
        entry_count=len(manifest.entries),
        root_hash=manifest.root_hash,
    )


def validate_stock_connect_status_manifest(
    path: Path,
    *,
    required_from: date,
    max_bytes: int,
) -> StockConnectManifestValidation:
    """离线验证状态 coverage exact schema、候选边界与所有条目身份摘要。"""
    manifest = _load_status_coverage_manifest(
        path,
        expected_required_from=required_from,
        max_bytes=max_bytes,
    )
    return StockConnectManifestValidation(
        manifest_kind="status-coverage",
        sha256=manifest.manifest_sha256,
        entry_count=len(manifest.entries),
        required_from=manifest.required_from,
    )


def validate_stock_connect_master_profile_manifest(
    path: Path,
    *,
    expected_sha256: str,
    max_bytes: int,
) -> StockConnectManifestValidation:
    """离线验证主档 profile set 摘要、exact schema、布局和生效区间。"""
    profiles = _load_master_fixed_length_profile(
        path,
        expected_sha256=expected_sha256,
        max_bytes=max_bytes,
    )
    return StockConnectManifestValidation(
        manifest_kind="securities-master-profile",
        sha256=expected_sha256,
        entry_count=len(profiles.profiles),
    )


def calculate_stock_connect_sftp_manifest_root(
    path: Path,
    *,
    max_bytes: int,
) -> str:
    """仅从 header 的有序页面引用计算 canonical root，供受控交付流程填回后再完整验证。"""
    payload = _read_bounded_file(path, max_bytes=max_bytes)
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("HKEX SFTP delivery manifest is invalid JSON") from error
    _require_exact_mapping_keys(
        raw,
        required={"manifestSchema", "rootHash", "pages"},
        label="HKEX SFTP delivery manifest",
    )
    assert isinstance(raw, dict)
    if raw["manifestSchema"] != _SFTP_DELIVERY_MANIFEST_SCHEMA:
        raise ValueError("HKEX SFTP delivery manifest schema is unsupported")
    pages = raw["pages"]
    if not isinstance(pages, list) or not pages:
        raise ValueError("HKEX SFTP delivery manifest pages are unavailable")
    normalized: list[dict[str, int | str]] = []
    seen: set[int] = set()
    for page in pages:
        _require_exact_mapping_keys(
            page,
            required={"pageNo", "relativePath", "sha256"},
            label="HKEX SFTP delivery page reference",
        )
        assert isinstance(page, dict)
        page_no = page["pageNo"]
        if (
            isinstance(page_no, bool)
            or not isinstance(page_no, int)
            or page_no < 0
            or page_no in seen
        ):
            raise ValueError("HKEX SFTP delivery page number is invalid")
        seen.add(page_no)
        relative_path = _strict_string(
            page["relativePath"],
            label="HKEX SFTP delivery page path",
        )
        _validate_safe_relative_path(relative_path, label="HKEX SFTP delivery page")
        normalized.append(
            {
                "pageNo": page_no,
                "relativePath": relative_path,
                "sha256": _lowercase_sha256(
                    page["sha256"],
                    label="HKEX SFTP delivery page",
                ),
            }
        )
    ordered = sorted(normalized, key=lambda item: int(item["pageNo"]))
    if [item["pageNo"] for item in ordered] != list(range(len(ordered))):
        raise ValueError("HKEX SFTP delivery pages are not contiguous")
    return _canonical_json_sha256({"pages": ordered})


def _strict_string(value: object, *, label: str) -> str:
    """读取清单内的非空文本，不接受隐式数字或布尔转换。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is invalid")
    return value.strip()


def _lowercase_sha256(value: object, *, label: str) -> str:
    """读取小写 SHA-256，防止对象身份在大小写规范化时漂移。"""
    digest = _strict_string(value, label=label)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"{label} digest is invalid")
    return digest


def _manifest_datetime(value: object, *, label: str) -> datetime:
    """读取带时区的 RFC 3339 清单时间，禁止把本地时间猜成 UTC。"""
    parsed = datetime.fromisoformat(_strict_string(value, label=label).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include timezone")
    return parsed


def _validate_safe_relative_path(value: str, *, label: str) -> None:
    """限制配置卷相对路径，禁止绝对路径、回退段和空路径逃逸挂载根。"""
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise ValueError(f"{label} relative path is unsafe")


def _resolve_manifest_relative_file(manifest_path: Path, relative_path: str) -> Path:
    """把清单相对位置约束在同一只读配置根，拒绝符号链接逃逸。"""
    _validate_safe_relative_path(relative_path, label="manifest delivery")
    root = manifest_path.parent.resolve()
    candidate = (root / relative_path).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError("manifest delivery path escapes the approved config root")
    return candidate


def _validate_hkex_https_url(value: str) -> None:
    """只接受 HKEX 官方 HTTPS 主机，重定向后的响应仍由读取器再次校验。"""
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in {
        "www.hkex.com.hk",
        "hkex.com.hk",
    }:
        raise ValueError("HKEX calendar URL must use an official HTTPS host")


def _load_master_fixed_length_profile(
    path: Path, *, expected_sha256: str, max_bytes: int
) -> _MasterFixedLengthProfileSet:
    """读取摘要钉住的历史 profile set，并拒绝日期重叠、未知键或越界布局。"""
    if not path.is_file():
        raise ValueError("HKEX Securities Master profile manifest is unavailable")
    manifest_bytes = _read_bounded_file(path, max_bytes=max_bytes)
    if _sha256(manifest_bytes) != expected_sha256:
        raise ValueError("HKEX Securities Master profile manifest digest mismatch")
    try:
        raw = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("HKEX Securities Master profile manifest is invalid JSON") from error
    _require_exact_mapping_keys(
        raw,
        required={"manifestSchema", "profiles"},
        label="HKEX Securities Master profile manifest",
    )
    if raw["manifestSchema"] != ("quant-v2.hkex-securities-master-fixed-length-profile-set.v2"):
        raise ValueError("HKEX Securities Master manifest schema is unsupported")
    profiles_raw = raw["profiles"]
    if not isinstance(profiles_raw, list) or not profiles_raw:
        raise ValueError("HKEX Securities Master profiles are unavailable")
    profiles = tuple(
        sorted(
            (_parse_master_fixed_length_profile(item) for item in profiles_raw),
            key=lambda item: item.effective_from,
        )
    )
    if len({profile.profile_id for profile in profiles}) != len(profiles):
        raise ValueError("HKEX Securities Master profile id is duplicated")
    for previous, current in zip(profiles, profiles[1:], strict=False):
        if previous.effective_to is None or current.effective_from <= previous.effective_to:
            raise ValueError("HKEX Securities Master profile effective ranges overlap")
    return _MasterFixedLengthProfileSet(profiles=profiles)


def _parse_master_fixed_length_profile(raw: object) -> _MasterFixedLengthProfile:
    """解析一个带明确生效区间的 fixed-length 布局，禁止用当前版覆盖未知历史。"""
    _require_exact_mapping_keys(
        raw,
        required={
            "effectiveFrom",
            "effectiveTo",
            "profileId",
            "specificationReference",
            "recordLengthBytes",
            "lineEnding",
            "fileName",
            "dataRecord",
        },
        label="HKEX Securities Master dated profile",
    )
    assert isinstance(raw, dict)
    effective_from = date.fromisoformat(
        _strict_string(raw["effectiveFrom"], label="master effectiveFrom")
    )
    effective_to_value = raw["effectiveTo"]
    effective_to = (
        None
        if effective_to_value is None
        else date.fromisoformat(_strict_string(effective_to_value, label="master effectiveTo"))
    )
    if effective_to is not None and effective_to < effective_from:
        raise ValueError("HKEX Securities Master profile effective range is inverted")
    profile_id = _manifest_string(raw, "profileId", max_length=160)
    specification_reference = _manifest_string(raw, "specificationReference", max_length=320)
    record_length = _manifest_positive_int(raw, "recordLengthBytes", maximum=65_536)
    line_ending = raw["lineEnding"]
    if line_ending not in {"CRLF", "LF"}:
        raise ValueError("HKEX Securities Master line ending is unsupported")
    file_name = raw["fileName"]
    _require_exact_mapping_keys(
        file_name,
        required={"pattern", "issuedDateGroup", "issuedDateFormat", "dateRole"},
        label="HKEX Securities Master file-name profile",
    )
    if file_name["dateRole"] != "ISSUED_DATE":
        raise ValueError("HKEX Securities Master file-name date must be ISSUED_DATE")
    file_name_pattern = _manifest_string(file_name, "pattern", max_length=512)
    file_name_date_group = _manifest_string(file_name, "issuedDateGroup", max_length=64)
    file_name_date_format = _manifest_string(file_name, "issuedDateFormat", max_length=64)
    try:
        compiled_name = re.compile(file_name_pattern)
    except re.error as error:
        raise ValueError("HKEX Securities Master file-name pattern is invalid") from error
    if file_name_date_group not in compiled_name.groupindex:
        raise ValueError("HKEX Securities Master issued-date group is missing")
    data_record = raw["dataRecord"]
    _require_exact_mapping_keys(
        data_record,
        required={
            "selector",
            "securityId",
            "instrumentCode",
            "displayName",
            "effectiveTradeDate",
        },
        label="HKEX Securities Master data-record profile",
    )
    selector_raw = data_record["selector"]
    selector: _FixedRecordSelector | None
    if selector_raw is None:
        selector = None
    else:
        _require_exact_mapping_keys(
            selector_raw,
            required={"field", "equals"},
            label="HKEX Securities Master record selector",
        )
        selector = _FixedRecordSelector(
            field=_fixed_field_layout(
                selector_raw["field"],
                record_length=record_length,
                date_field=False,
            ),
            equals=_manifest_string(selector_raw, "equals", max_length=64),
        )
    security_id = _fixed_field_layout(
        data_record["securityId"],
        record_length=record_length,
        date_field=False,
    )
    instrument_code = _fixed_field_layout(
        data_record["instrumentCode"],
        record_length=record_length,
        date_field=False,
    )
    display_name = _fixed_field_layout(
        data_record["displayName"],
        record_length=record_length,
        date_field=False,
    )
    effective_trade_date = _fixed_field_layout(
        data_record["effectiveTradeDate"],
        record_length=record_length,
        date_field=True,
    )
    _reject_overlapping_master_fields(
        selector=selector,
        security_id=security_id,
        instrument_code=instrument_code,
        display_name=display_name,
        effective_trade_date=effective_trade_date,
    )
    return _MasterFixedLengthProfile(
        effective_from=effective_from,
        effective_to=effective_to,
        profile_id=profile_id,
        specification_reference=specification_reference,
        record_length_bytes=record_length,
        line_ending=line_ending,
        file_name_pattern=file_name_pattern,
        file_name_date_group=file_name_date_group,
        file_name_date_format=file_name_date_format,
        record_selector=selector,
        security_id=security_id,
        instrument_code=instrument_code,
        display_name=display_name,
        effective_trade_date=effective_trade_date,
    )


def _fixed_field_layout(raw: object, *, record_length: int, date_field: bool) -> _FixedFieldLayout:
    """把 manifest 字段描述转换为有界字节切片，并预验证 codec。"""
    required = {"offsetBytes", "lengthBytes", "encoding", "trim"}
    if date_field:
        required.add("dateFormat")
    _require_exact_mapping_keys(
        raw,
        required=required,
        label="HKEX Securities Master fixed field",
    )
    assert isinstance(raw, dict)
    offset = _manifest_non_negative_int(raw, "offsetBytes", maximum=record_length - 1)
    length = _manifest_positive_int(raw, "lengthBytes", maximum=record_length)
    if offset + length > record_length:
        raise ValueError("HKEX Securities Master fixed field exceeds record length")
    encoding = _manifest_string(raw, "encoding", max_length=64)
    try:
        codecs.lookup(encoding)
    except LookupError as error:
        raise ValueError("HKEX Securities Master field encoding is unsupported") from error
    trim = raw["trim"]
    if trim not in {"BOTH", "LEFT", "NONE", "RIGHT"}:
        raise ValueError("HKEX Securities Master field trim rule is invalid")
    date_format = _manifest_string(raw, "dateFormat", max_length=64) if date_field else None
    return _FixedFieldLayout(
        offset_bytes=offset,
        length_bytes=length,
        encoding=encoding,
        trim=trim,
        date_format=date_format,
    )


def _require_exact_mapping_keys(value: object, *, required: set[str], label: str) -> None:
    """要求 manifest 对象键集合精确匹配，避免静默忽略供应商布局升级。"""
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError(f"{label} fields are invalid")


def _manifest_string(mapping: object, key: str, *, max_length: int) -> str:
    """读取非空且有界 manifest 字符串。"""
    assert isinstance(mapping, dict)
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"HKEX Securities Master manifest {key} is invalid")
    return value.strip()


def _manifest_non_negative_int(mapping: object, key: str, *, maximum: int) -> int:
    """读取排除布尔值的有界非负整数。"""
    assert isinstance(mapping, dict)
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"HKEX Securities Master manifest {key} is invalid")
    return value


def _manifest_positive_int(mapping: object, key: str, *, maximum: int) -> int:
    """读取排除布尔值的有界正整数。"""
    value = _manifest_non_negative_int(mapping, key, maximum=maximum)
    if value == 0:
        raise ValueError(f"HKEX Securities Master manifest {key} is invalid")
    return value


def _reject_overlapping_master_fields(
    *,
    selector: _FixedRecordSelector | None,
    security_id: _FixedFieldLayout,
    instrument_code: _FixedFieldLayout,
    display_name: _FixedFieldLayout,
    effective_trade_date: _FixedFieldLayout,
) -> None:
    """拒绝业务字段重叠；selector 可复用业务字段以匹配固定常量。"""
    fields = (security_id, instrument_code, display_name, effective_trade_date)
    ranges = [
        range(field.offset_bytes, field.offset_bytes + field.length_bytes) for field in fields
    ]
    for index, current in enumerate(ranges):
        if any(set(current).intersection(other) for other in ranges[index + 1 :]):
            raise ValueError("HKEX Securities Master business fields overlap")
    if selector is not None and not selector.equals:
        raise ValueError("HKEX Securities Master record selector value is blank")


def _split_fixed_length_records(
    payload: bytes, *, layout: _MasterFixedLengthProfile
) -> list[bytes]:
    """按 manifest 声明的换行符切分记录，并拒绝混合换行与长度漂移。"""
    separator = b"\r\n" if layout.line_ending == "CRLF" else b"\n"
    if layout.line_ending == "CRLF" and re.search(rb"(?<!\r)\n|\r(?!\n)", payload):
        raise ValueError("HKEX Securities Master contains mixed line endings")
    if layout.line_ending == "LF" and b"\r" in payload:
        raise ValueError("HKEX Securities Master contains mixed line endings")
    records = payload.split(separator)
    if records and records[-1] == b"":
        records.pop()
    if not records or any(len(record) != layout.record_length_bytes for record in records):
        raise ValueError("HKEX Securities Master record length does not match profile")
    return records


def _decode_fixed_field(record: bytes, field: _FixedFieldLayout) -> str:
    """按字节 offset/length 解码字段，禁止替换非法字符。"""
    raw = record[field.offset_bytes : field.offset_bytes + field.length_bytes]
    value = raw.decode(field.encoding, errors="strict")
    if field.trim == "BOTH":
        return value.strip(" \x00")
    if field.trim == "LEFT":
        return value.lstrip(" \x00")
    if field.trim == "RIGHT":
        return value.rstrip(" \x00")
    return value


def parse_hkex_securities_master(
    payload: bytes,
    *,
    effective_trade_date: date,
    expected_issued_date: date,
    product_name: str,
    profile: str,
    layout: _MasterFixedLengthProfile,
) -> list[dict[str, object]]:
    """按摘要钉住的 licensed 字节布局解析 T+1 Securities Master。"""
    _require_profile(profile, "hkex-securities-master-fixed-length-manifest-v2")
    issued_match = re.fullmatch(layout.file_name_pattern, product_name)
    if issued_match is None:
        raise ValueError("HKEX Securities Master file name does not match licensed profile")
    try:
        issued_text = issued_match.group(layout.file_name_date_group)
        actual_issued_date = datetime.strptime(issued_text, layout.file_name_date_format).date()
    except (IndexError, ValueError) as error:
        raise ValueError("HKEX Securities Master issued date cannot be parsed") from error
    if actual_issued_date != expected_issued_date:
        raise ValueError("HKEX Securities Master issued date does not match requested T+1 file")
    records: list[dict[str, object]] = []
    for raw_record in _split_fixed_length_records(payload, layout=layout):
        selector = layout.record_selector
        if (
            selector is not None
            and _decode_fixed_field(raw_record, selector.field) != selector.equals
        ):
            continue
        security_id = _decode_fixed_field(raw_record, layout.security_id) or None
        code = _normalize_security_code(_decode_fixed_field(raw_record, layout.instrument_code))
        name = _decode_fixed_field(raw_record, layout.display_name)
        if not code or not name:
            raise ValueError("HKEX Securities Master data record has a blank code or name")
        effective_text = _decode_fixed_field(raw_record, layout.effective_trade_date)
        assert layout.effective_trade_date.date_format is not None
        try:
            record_effective_date = datetime.strptime(
                effective_text,
                layout.effective_trade_date.date_format,
            ).date()
        except ValueError as error:
            raise ValueError(
                "HKEX Securities Master effective trade date cannot be parsed"
            ) from error
        if record_effective_date != effective_trade_date:
            raise ValueError("HKEX Securities Master record effective date does not match request")
        records.append(
            {
                "securityId": security_id,
                "instrumentCode": code,
                "displayName": name,
                "effectiveFrom": record_effective_date.isoformat(),
            }
        )
    if not records or len({item["instrumentCode"] for item in records}) != len(records):
        raise ValueError("HKEX Securities Master is empty or has duplicate active codes")
    stable_ids = [item["securityId"] for item in records if item["securityId"] is not None]
    if len(set(stable_ids)) != len(stable_ids):
        raise ValueError("HKEX Securities Master has duplicate stable security ids")
    return records


def parse_stock_connect_status(
    payload: bytes,
    *,
    channel: str,
    direction: str,
    trade_date: date,
    profile: str,
    source_code: str,
    product_name: str,
    calendar_trading_day: bool | None = None,
    landing_final: bool = False,
) -> dict[str, object]:
    """按明确官方 profile 解析最终状态；不识别的原始格式一律 schema 失败。"""
    if profile == "hkex-omdc-mmdh-msg80-v2.1":
        if calendar_trading_day is not True or not landing_final:
            raise ValueError("OMD-C status requires calendar and end-of-day finality evidence")
        status = _parse_omdc_status(payload, channel=channel, trade_date=trade_date)
        status["sessionAvailability"] = "DERIVED"
    elif profile == "sse-is117-v1.09-is124-v3.50-gateway-v1":
        status = _parse_named_status_delivery(
            payload,
            trade_date=trade_date,
            session_field="sessionstate",
            message_field="recordtype",
            expected_message_type="trdses04",
            market_field=None,
            expected_markets=frozenset(),
            session_map={
                "CLOSED": "CLOSED",
                "HALTED": "HALTED",
                "NOT_OPEN": "NOT_OPEN",
                "OPEN": "OPEN",
            },
            amount_status_map={
                "1": "SOURCE_MISSING",
                "2": "ACTUAL_REPORTED",
                "3": "SUFFICIENT",
            },
        )
    elif profile == "szse-step-binary-v1.17-msg390019-gateway-v1":
        status = _parse_named_status_delivery(
            payload,
            trade_date=trade_date,
            session_field="tradingsessionsubid",
            message_field="messagetype",
            expected_message_type="390019",
            market_field="marketid",
            expected_markets=frozenset({"HKEX", "XHKG"}),
            session_map={
                "0": "CLOSED",
                "7": "HALTED",
                "100": "NOT_OPEN",
                "103": "CLOSED",
                "1": "OPEN",
                "2": "OPEN",
                "3": "OPEN",
                "4": "OPEN",
                "5": "OPEN",
                "101": "OPEN",
                "102": "HALTED",
                "104": "HALTED",
                "105": "OPEN",
                "106": "OPEN",
                "107": "OPEN",
                "108": "OPEN",
            },
            amount_status_map={
                "1": "SOURCE_MISSING",
                "2": "ACTUAL_REPORTED",
                "3": "SUFFICIENT",
            },
        )
    else:
        raise ValueError("stock-connect status profile is unsupported")
    if profile != "hkex-omdc-mmdh-msg80-v2.1":
        status["sessionAvailability"] = "REPORTED"
    status.update(
        {
            "tradeDate": trade_date.isoformat(),
            "sourceCode": source_code,
            "productName": product_name,
            "sourcePublicationAt": status["observedAt"],
            "sourceFileSha256": _sha256(payload),
        }
    )
    return status


def _parse_omdc_status(payload: bytes, *, channel: str, trade_date: date) -> dict[str, object]:
    """解析 OMD-C MMDH v2.1 TCP frame，并按业务时间选择目标市场最终 Msg80。"""
    offset = 0
    previous_sequence: int | None = None
    previous_internal_sequence: int | None = None
    matches: list[tuple[int, int]] = []
    while offset < len(payload):
        if len(payload) - offset < 20:
            raise ValueError("OMD-C MMDH frame header is truncated")
        frame_size, filler, sequence, internal_sequence, send_ns = struct.unpack_from(
            "<H2sIIQ", payload, offset
        )
        if frame_size < 20 or offset + frame_size > len(payload):
            raise ValueError("OMD-C MMDH frame length is invalid")
        if filler != b"\x00\x00":
            raise ValueError("OMD-C MMDH frame filler is invalid")
        if sequence == 0 or (previous_sequence is not None and sequence != previous_sequence + 1):
            raise ValueError("OMD-C MMDH frame sequence is discontinuous")
        if internal_sequence == 0 or (
            previous_internal_sequence is not None
            and internal_sequence < previous_internal_sequence
        ):
            raise ValueError("OMD-C MMDH internal sequence is invalid")
        send_at = _epoch_nanoseconds(send_ns, label="OMD-C MMDH SendTime")
        if send_at.date() != trade_date:
            raise ValueError("OMD-C MMDH SendTime business date mismatch")
        previous_sequence = sequence
        previous_internal_sequence = internal_sequence
        if frame_size == 20:
            offset += frame_size
            continue
        message_offset = offset + 20
        if frame_size - 20 < 4:
            raise ValueError("OMD-C MMDH business message is truncated")
        message_size, message_type = struct.unpack_from("<HH", payload, message_offset)
        if message_size != frame_size - 20:
            raise ValueError("OMD-C MMDH frame contains a partial or extra message")
        if message_type == 80:
            if message_size != 24:
                raise ValueError("OMD-C MMDH MsgType 80 size is invalid")
            _size, _type, market_bytes, direction_bytes, balance, observed_ns = struct.unpack_from(
                "<HH2s2sqQ", payload, message_offset
            )
            try:
                market = market_bytes.decode("ascii")
                direction = direction_bytes.decode("ascii")
            except UnicodeDecodeError as error:
                raise ValueError("OMD-C MMDH Msg80 market encoding is invalid") from error
            if market not in {"SH", "SZ"} or direction != "NB":
                raise ValueError("OMD-C MMDH Msg80 market or direction is invalid")
            observed_at = _epoch_nanoseconds(
                observed_ns,
                label="OMD-C MMDH DailyQuotaBalanceTime",
            )
            if observed_at.date() != trade_date:
                raise ValueError("OMD-C business date does not match requested trade date")
            if observed_ns > send_ns:
                raise ValueError("OMD-C Msg80 business time is after frame SendTime")
            if market == channel:
                matches.append((balance, observed_ns))
        offset += frame_size
    if not matches:
        raise ValueError("OMD-C final DQB message is unavailable")
    latest_ns = max(observed_ns for _balance, observed_ns in matches)
    latest_balances = {balance for balance, observed_ns in matches if observed_ns == latest_ns}
    if len(latest_balances) != 1:
        raise ValueError("OMD-C final DQB messages conflict at the same business time")
    balance = latest_balances.pop()
    observed_at = _epoch_nanoseconds(
        latest_ns,
        label="OMD-C MMDH DailyQuotaBalanceTime",
    )
    if balance == _OMDC_NULL_INT64:
        quota_state = "SUFFICIENT"
        quota_balance = None
    elif balance == 0:
        quota_state = "EXHAUSTED"
        quota_balance = "0"
    elif balance > 0:
        quota_state = "ACTUAL_REPORTED"
        quota_balance = str(balance)
    else:
        raise ValueError("OMD-C DQB value is invalid")
    return {
        "tradingDay": True,
        # Msg80 不报告会话；CLOSED 只由官方开市日与 END_OF_DAY_FINAL manifest 共同派生。
        "sessionState": "CLOSED",
        "buyOrderAccepted": None,
        "sellOrderAccepted": None,
        "quotaState": quota_state,
        "quotaBalance": quota_balance,
        "quotaCurrency": "CNY",
        "observedAt": _timestamp(observed_at),
        "finality": "END_OF_DAY",
    }


def _epoch_nanoseconds(value: int, *, label: str) -> datetime:
    """把非零 UTC epoch 纳秒转换为带时区时间，并拒绝平台不支持的范围。"""
    if value <= 0:
        raise ValueError(f"{label} is invalid")
    try:
        return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError(f"{label} is outside the supported range") from error


def _parse_named_status_delivery(
    payload: bytes,
    *,
    trade_date: date,
    session_field: str,
    message_field: str,
    expected_message_type: str,
    market_field: str | None,
    expected_markets: frozenset[str],
    session_map: Mapping[str, str],
    amount_status_map: Mapping[str, str],
) -> dict[str, object]:
    """解析官方网关按具名字段导出的版本化落地记录，并选取最后一条日终记录。"""
    rows = _csv_rows(payload, allow_pipe=True)
    header_index = next(
        (
            index
            for index, row in enumerate(rows)
            if {"posamt", "amountstatus", session_field, message_field, "origtime"}
            | ({market_field} if market_field is not None else set())
            <= {_normalize_header(cell) for cell in row}
        ),
        None,
    )
    if header_index is None:
        raise ValueError("status delivery header does not match approved profile")
    header = {_normalize_header(cell): index for index, cell in enumerate(rows[header_index])}
    data_rows = [row for row in rows[header_index + 1 :] if _value(row, header.get(session_field))]
    if not data_rows:
        raise ValueError("status delivery contains no records")
    row = data_rows[-1]
    if _value(row, header[message_field]).lower() != expected_message_type.lower():
        raise ValueError("status delivery message type is invalid")
    if market_field is not None and _value(row, header[market_field]).upper() not in (
        expected_markets
    ):
        raise ValueError("status delivery market identifier is invalid")
    session_raw = _value(row, header[session_field]).upper()
    session_state = session_map.get(session_raw)
    amount_state = amount_status_map.get(_value(row, header["amountstatus"]).upper())
    if session_state is None or amount_state is None:
        raise ValueError("status delivery contains an unknown official enum")
    pos_amount = _decimal_text(_value(row, header["posamt"]))
    if amount_state == "ACTUAL_REPORTED":
        if pos_amount is None:
            raise ValueError("actual quota status requires PosAmt")
        quota_balance = pos_amount
        if Decimal(pos_amount) == 0:
            amount_state = "EXHAUSTED"
    else:
        quota_balance = None
    observed_text = _value(row, header["origtime"])
    observed_at = _parse_status_time(observed_text, trade_date=trade_date)
    return {
        "tradingDay": session_state != "NOT_OPEN",
        "sessionState": session_state,
        "buyOrderAccepted": _optional_bool(_value(row, header.get("buyorderaccepted"))),
        "sellOrderAccepted": _optional_bool(_value(row, header.get("sellorderaccepted"))),
        "quotaState": amount_state,
        "quotaBalance": quota_balance,
        "quotaCurrency": "CNY",
        "observedAt": _timestamp(observed_at),
        "finality": "END_OF_DAY",
    }


def _daily_statistics_regime(
    *,
    trade_date: date,
    direction: str,
) -> _DailyStatisticsRegimeProfile:
    """按交易日和方向唯一选择披露制度，缺口或重叠时拒绝解析。"""
    matches = [
        profile
        for profile in _DAILY_STATISTICS_REGIME_PROFILES
        if profile.direction == direction
        and profile.effective_from <= trade_date
        and (profile.effective_to is None or trade_date <= profile.effective_to)
    ]
    if len(matches) != 1:
        raise ValueError("HKEX Daily Statistics has no unique regime profile")
    return matches[0]


def _validate_daily_buy_sell_regime(
    *,
    buy: str | None,
    sell: str | None,
    regime: _DailyStatisticsRegimeProfile,
) -> None:
    """按已批准制度约束买卖额成对存在或成对未披露，空列永远不能当零。"""
    if regime.buy_sell_policy == "REQUIRED" and (buy is None or sell is None):
        raise ValueError("HKEX Daily Statistics omitted regime-required buy/sell amounts")
    if regime.buy_sell_policy == "NOT_DISCLOSED" and (buy is not None or sell is not None):
        raise ValueError("HKEX Daily Statistics reported forbidden post-change buy/sell amounts")


def _market_record(
    values: Mapping[str, str], *, trade_date: date, direction: str
) -> dict[str, object]:
    """构造市场统计标准行，制度未披露与源缺失使用不同 availability。"""
    regime = _daily_statistics_regime(
        trade_date=trade_date,
        direction=direction,
    )
    turnover = _required_decimal(values.get("turnover"), "turnover")
    buy = _decimal_text(values.get("buy", ""))
    sell = _decimal_text(values.get("sell", ""))
    _validate_daily_buy_sell_regime(
        buy=buy,
        sell=sell,
        regime=regime,
    )
    trade_count = _optional_non_negative_int(values.get("trade_count"))
    etf_turnover = _decimal_text(values.get("etf_turnover", ""))
    if buy is not None and sell is not None:
        difference = abs(Decimal(buy) + Decimal(sell) - Decimal(turnover))
        if difference > Decimal("1"):
            raise ValueError("HKEX buy plus sell does not reconcile to turnover")
    unavailable_by_regime = regime.buy_sell_policy == "NOT_DISCLOSED"
    field_availability = {
        "buyAmount": (
            "REPORTED"
            if buy is not None
            else "NOT_DISCLOSED_BY_REGIME"
            if unavailable_by_regime
            else "SOURCE_MISSING"
        ),
        "sellAmount": (
            "REPORTED"
            if sell is not None
            else "NOT_DISCLOSED_BY_REGIME"
            if unavailable_by_regime
            else "SOURCE_MISSING"
        ),
        "turnoverAmount": "REPORTED",
        "netBuyAmount": ("NOT_DISCLOSED_BY_REGIME" if unavailable_by_regime else "NOT_APPLICABLE"),
        "tradeCount": "REPORTED" if trade_count is not None else "SOURCE_MISSING",
        "etfTurnoverAmount": ("REPORTED" if etf_turnover is not None else "SOURCE_MISSING"),
    }
    return {
        "tradeDate": trade_date.isoformat(),
        "buyAmount": buy,
        "sellAmount": sell,
        "turnoverAmount": turnover,
        "netBuyAmount": None,
        "quotaBalance": None,
        "currency": "CNY" if direction == "NORTHBOUND" else "HKD",
        "availabilityStatus": "COMPLETE"
        if all(value == "REPORTED" for value in field_availability.values())
        else "PARTIAL",
        "tradeCount": trade_count,
        "etfTurnoverAmount": etf_turnover,
        "fieldAvailability": field_availability,
    }


def _active_record_from_columns(
    cells: Sequence[str],
    header: Mapping[str, int],
    units: Mapping[str, Decimal],
    *,
    trade_date: date,
    direction: str,
    rank: int,
    code: str,
) -> dict[str, object]:
    """把一行官方活跃证券映射为 Top 10 来源事实，净额仍由平台独立派生。"""
    regime = _daily_statistics_regime(
        trade_date=trade_date,
        direction=direction,
    )
    buy = _scaled_decimal(_value(cells, header.get("buy")), units.get("buy", Decimal(1)))
    sell = _scaled_decimal(_value(cells, header.get("sell")), units.get("sell", Decimal(1)))
    _validate_daily_buy_sell_regime(
        buy=buy,
        sell=sell,
        regime=regime,
    )
    turnover = _scaled_decimal(
        _value(cells, header.get("turnover")), units.get("turnover", Decimal(1))
    )
    if turnover is None:
        raise ValueError("HKEX active security turnover is required")
    unavailable_by_regime = regime.buy_sell_policy == "NOT_DISCLOSED"
    return {
        "instrumentCode": code,
        "instrumentName": _value(cells, header.get("name")) or None,
        "tradeDate": trade_date.isoformat(),
        "rankNo": rank,
        "buyAmount": buy,
        "sellAmount": sell,
        "turnoverAmount": turnover,
        "currency": "CNY" if direction == "NORTHBOUND" else "HKD",
        "fieldAvailability": {
            "buyAmount": "REPORTED"
            if buy is not None
            else "NOT_DISCLOSED_BY_REGIME"
            if unavailable_by_regime
            else "SOURCE_MISSING",
            "sellAmount": "REPORTED"
            if sell is not None
            else "NOT_DISCLOSED_BY_REGIME"
            if unavailable_by_regime
            else "SOURCE_MISSING",
            "turnoverAmount": "REPORTED",
            "netBuyAmount": "NOT_DISCLOSED_BY_REGIME"
            if unavailable_by_regime
            else "NOT_APPLICABLE",
        },
    }


def _header_map(cells: Sequence[str]) -> dict[str, int] | None:
    """识别 HKEX CSV 的英文列头；字段顺序变化不会改变映射。"""
    aliases = {
        "direction": {"direction", "tradingdirection", "bound", "flowdirection"},
        "rank": {"rank", "ranking", "rankno", "rankingno"},
        "code": {"stockcode", "securitycode", "instrumentcode", "code"},
        "name": {"stockname", "securityname", "instrumentname", "name"},
        "buy": {"buyturnover", "buytradevalue", "buyamount", "buytrades"},
        "sell": {"sellturnover", "selltradevalue", "sellamount", "selltrades"},
        "turnover": {
            "totalturnover",
            "buyandsellturnover",
            "turnover",
            "totaltradevalue",
        },
        "trade_count": {"tradecount", "numberoftrades", "nooftrades"},
        "etf_turnover": {"etfturnover", "etftradevalue"},
    }
    result: dict[str, int] = {}
    for index, cell in enumerate(cells):
        normalized = _normalize_header(cell)
        for field, values in aliases.items():
            if normalized in values:
                result[field] = index
                break
    if "turnover" in result and (
        {"direction"} <= result.keys() or {"code", "rank"} & result.keys()
    ):
        return result
    return None


def _merge_market_columns(
    values: dict[str, str],
    cells: Sequence[str],
    header: Mapping[str, int],
    units: Mapping[str, Decimal],
) -> None:
    """从平铺市场行读取金额、笔数和 ETF 成交额，不派生缺失列。"""
    for field in ("buy", "sell", "turnover", "etf_turnover"):
        raw = _value(cells, header.get(field))
        parsed = _scaled_decimal(raw, units.get(field, Decimal(1)))
        if parsed is not None:
            values[field] = parsed
    trade_count = _value(cells, header.get("trade_count"))
    if trade_count:
        values["trade_count"] = trade_count


def _merge_market_key_value(values: dict[str, str], cells: Sequence[str]) -> None:
    """兼容 HKEX 分节 CSV 的指标名/数值行，同时拒绝从别的指标猜测金额。"""
    non_empty = [cell for cell in cells if cell.strip()]
    if len(non_empty) < 2:
        return
    label = _normalize_header(non_empty[0])
    aliases = {
        "buy": ("buyturnover", "buytradevalue", "buytrades"),
        "sell": ("sellturnover", "selltradevalue", "selltrades"),
        "turnover": ("totalturnover", "buyandsellturnover", "totaltradevalue"),
        "trade_count": ("tradecount", "numberoftrades", "nooftrades"),
        "etf_turnover": ("etfturnover", "etftradevalue"),
    }
    field = next(
        (name for name, tokens in aliases.items() if any(token in label for token in tokens)),
        None,
    )
    if field is None:
        return
    if field == "trade_count":
        values[field] = non_empty[-1]
        return
    parsed = _scaled_decimal(non_empty[-1], _unit_multiplier(non_empty[0]))
    if parsed is not None:
        values[field] = parsed


def _find_master_header(
    rows: Sequence[Sequence[str]],
) -> tuple[int, dict[str, int]]:
    """定位 Securities Master 实际列头，并映射代码、名称和可选上市日期。"""
    code_aliases = {"stockcode", "securitycode", "instrumentcode", "stockid"}
    name_aliases = {
        "stockshortname",
        "securityname",
        "instrumentname",
        "stockname",
        "englishname",
    }
    for row_index, row in enumerate(rows):
        normalized = [_normalize_header(cell) for cell in row]
        code_index = next((i for i, value in enumerate(normalized) if value in code_aliases), None)
        name_index = next((i for i, value in enumerate(normalized) if value in name_aliases), None)
        if code_index is None or name_index is None:
            continue
        result = {"code": code_index, "name": name_index}
        for index, value in enumerate(normalized):
            if value in {"listingdate", "listdate", "effectivedate"}:
                result["listing_date"] = index
            elif value in {"securitytype", "instrumenttype", "stocktype"}:
                result["security_type"] = index
        return row_index, result
    raise ValueError("HKEX Securities Master header is unavailable")


def _csv_rows(payload: bytes, *, allow_pipe: bool = False) -> list[list[str]]:
    """按真实文本编码和分隔符读取交付对象；二进制或空字节不会被当作 CSV。"""
    text: str | None = None
    for encoding in ("utf-8-sig", "big5-hkscs"):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None or "\x00" in text:
        raise ValueError("official CSV encoding is unsupported")
    sample = text[:4096]
    delimiters = ",\t|" if allow_pipe else ",\t"
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=delimiters)
    except csv.Error:
        dialect = csv.excel
    rows = [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text), dialect)]
    return [row for row in rows if any(cell.strip() for cell in row)]


def _parameters(request: SourceRequest) -> dict[str, str]:
    """将中立参数变成无重复字典，避免后值覆盖前值改变来源定位。"""
    result = dict(request.parameters)
    if len(result) != len(request.parameters):
        raise _provider_error(
            ProviderErrorCode.INVALID_REQUEST,
            "stock-connect request contains duplicate parameters",
            retryable=False,
        )
    return result


def _channel(parameters: Mapping[str, str]) -> str:
    """读取严格沪或深通道代码。"""
    value = parameters.get("channel")
    if value not in {"SH", "SZ"}:
        raise _provider_error(
            ProviderErrorCode.INVALID_REQUEST,
            "stock-connect channel is invalid",
            retryable=False,
        )
    return value


def _direction(parameters: Mapping[str, str]) -> str:
    """读取严格北向或南向方向。"""
    value = parameters.get("direction")
    if value not in {"NORTHBOUND", "SOUTHBOUND"}:
        raise _provider_error(
            ProviderErrorCode.INVALID_REQUEST,
            "stock-connect direction is invalid",
            retryable=False,
        )
    return value


def _single_trade_date(parameters: Mapping[str, str]) -> date:
    """要求单次 provider 请求只消费一个业务日期，禁止把抓取日当交易日。"""
    start = parameters.get("start") or parameters.get("trade_date")
    end = parameters.get("end") or parameters.get("trade_date")
    if start is None or end is None or start != end:
        raise _provider_error(
            ProviderErrorCode.INVALID_REQUEST,
            "official stock-connect request requires one trade date",
            retryable=False,
        )
    try:
        return date.fromisoformat(start)
    except ValueError as error:
        raise _provider_error(
            ProviderErrorCode.INVALID_REQUEST,
            "stock-connect trade date is invalid",
            retryable=False,
        ) from error


def _year(parameters: Mapping[str, str]) -> int:
    """读取受限官方日历年度。"""
    value = parameters.get("year")
    if value is None or not value.isdigit() or not 2014 <= int(value) <= 2100:
        raise _provider_error(
            ProviderErrorCode.INVALID_REQUEST,
            "stock-connect calendar year is invalid",
            retryable=False,
        )
    return int(value)


def _format_remote_path(
    template: str,
    *,
    trade_date: date,
    channel: str,
    issued_date: date | None = None,
) -> str:
    """格式化 SFTP 路径；主档可分别引用 issued date 与 effective trade date。"""
    source_issued_date = issued_date or trade_date
    path = template.format(
        trade_date=trade_date.strftime("%Y%m%d"),
        iso_date=trade_date.isoformat(),
        year=trade_date.year,
        month=f"{trade_date.month:02d}",
        day=f"{trade_date.day:02d}",
        effective_trade_date=trade_date.strftime("%Y%m%d"),
        effective_iso_date=trade_date.isoformat(),
        effective_year=trade_date.year,
        issued_date=source_issued_date.strftime("%Y%m%d"),
        issued_iso_date=source_issued_date.isoformat(),
        issued_year=source_issued_date.year,
        channel=channel,
    )
    pure = PurePosixPath(path)
    if ".." in pure.parts or not path.strip():
        raise ValueError("stock-connect SFTP path is unsafe")
    return str(pure)


def _resolve_delivery_path(root: Path, template: str, *, trade_date: date, channel: str) -> Path:
    """把状态文件模板限制在只读根目录内，防止配置穿越读取其他秘密。"""
    relative = template.format(
        trade_date=trade_date.strftime("%Y%m%d"),
        iso_date=trade_date.isoformat(),
        year=trade_date.year,
        month=f"{trade_date.month:02d}",
        day=f"{trade_date.day:02d}",
        channel=channel,
    )
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if not candidate.is_relative_to(resolved_root):
        raise ValueError("stock-connect status path escaped delivery root")
    return candidate


def _read_delivery_file(
    path: Path,
    *,
    source_code: str,
    product_name: str,
    max_bytes: int,
) -> _FetchedObject:
    """有界读取官方网关落地对象；最终性另由同名 manifest 证明。"""
    if not path.is_file():
        raise _provider_error(
            ProviderErrorCode.UNAVAILABLE,
            f"{source_code} final delivery is unavailable",
            retryable=True,
        )
    payload = _read_bounded_file(path, max_bytes=max_bytes)
    if not payload:
        raise _provider_error(
            ProviderErrorCode.UNAVAILABLE,
            f"{source_code} final delivery is empty",
            retryable=True,
        )
    return _FetchedObject(
        payload=payload,
        content_type="application/octet-stream",
        # 本地 mtime 只用于运维审计，不具有官方 publication 语义。
        published_at=None,
        product_name=product_name,
        upstream_source=source_code,
    )


def _validate_status_gateway_manifest(
    path: Path,
    *,
    payload: bytes,
    profile: str,
    trade_date: date,
    channel: str,
    direction: str,
    max_manifest_bytes: int,
) -> None:
    """校验三类状态 landing 的生产者、版本、业务键、最终性和字节摘要。"""
    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    if not manifest_path.is_file():
        raise _provider_error(
            ProviderErrorCode.UNAVAILABLE,
            "stock-connect status gateway manifest is unavailable",
            retryable=True,
        )
    try:
        decoded = json.loads(_read_bounded_file(manifest_path, max_bytes=max_manifest_bytes))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("stock-connect status gateway manifest is invalid") from error
    common_keys = {
        "producer",
        "sourceProfile",
        "projectionProfile",
        "messageType",
        "marketId",
        "businessDate",
        "channel",
        "direction",
        "finality",
        "payloadSha256",
    }
    expected_keys = common_keys | (
        {"sequence"} if profile == "hkex-omdc-mmdh-msg80-v2.1" else set()
    )
    if not isinstance(decoded, dict) or set(decoded) != expected_keys:
        raise ValueError("stock-connect status gateway manifest fields are invalid")
    if profile == "hkex-omdc-mmdh-msg80-v2.1":
        expected = {
            "producer": "HKEX_OMDC_CAPTURE",
            "sourceProfile": "HKEX_OMDC_MMDH_MSG80_V2.1",
            "projectionProfile": "quant-v2.stock-connect-status-raw-final.v1",
            "messageType": "80",
            "marketId": "HKEX",
        }
        sequence = decoded.get("sequence")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= 0
            or sequence != _mmdh_final_sequence(payload)
        ):
            raise ValueError("OMD-C status manifest sequence is invalid")
    elif profile == "sse-is117-v1.09-is124-v3.50-gateway-v1":
        expected = {
            "producer": "SSE_MDGW_GATEWAY",
            "sourceProfile": "SSE_IS117_1.09_IS124_3.50",
            "projectionProfile": "quant-v2.stock-connect-status-gateway.v1",
            "messageType": "trdses04",
            "marketId": "SSE",
        }
    elif profile == "szse-step-binary-v1.17-msg390019-gateway-v1":
        expected = {
            "producer": "SZSE_STEP_GATEWAY",
            "sourceProfile": "SZSE_STEP_BINARY_1.17",
            "projectionProfile": "quant-v2.stock-connect-status-gateway.v1",
            "messageType": "390019",
        }
        if decoded.get("marketId") not in {"HKEX", "XHKG"}:
            raise ValueError("SZSE status gateway market identifier is invalid")
    else:
        raise ValueError("status gateway profile is unsupported")
    if any(decoded.get(key) != value for key, value in expected.items()):
        raise ValueError("stock-connect status gateway producer or version is invalid")
    if (
        decoded.get("businessDate") != trade_date.isoformat()
        or decoded.get("channel") != channel
        or decoded.get("direction") != direction
        or decoded.get("finality") != "END_OF_DAY_FINAL"
        or decoded.get("payloadSha256") != _sha256(payload)
    ):
        raise ValueError("stock-connect status gateway manifest does not match payload")


def _mmdh_final_sequence(payload: bytes) -> int:
    """扫描有界 MMDH frame 并返回最终 TCP sequence，供 final manifest 对账。"""
    offset = 0
    final_sequence = 0
    while offset < len(payload):
        if len(payload) - offset < 20:
            raise ValueError("OMD-C MMDH frame header is truncated")
        frame_size, _filler, sequence, _internal_sequence, _send_ns = struct.unpack_from(
            "<H2sIIQ", payload, offset
        )
        if frame_size < 20 or offset + frame_size > len(payload):
            raise ValueError("OMD-C MMDH frame length is invalid")
        if sequence == 0 or (final_sequence and sequence != final_sequence + 1):
            raise ValueError("OMD-C MMDH frame sequence is discontinuous")
        final_sequence = sequence
        offset += frame_size
    if final_sequence == 0:
        raise ValueError("OMD-C MMDH final sequence is unavailable")
    return final_sequence


def _read_bounded_file(path: Path, *, max_bytes: int) -> bytes:
    """先校验文件大小再有界读取，防止落地异常耗尽 worker 内存。"""
    size = path.stat().st_size
    if size <= 0 or size > max_bytes:
        raise ValueError("stock-connect delivery file size is invalid")
    with path.open("rb") as stream:
        payload = stream.read(max_bytes + 1)
    if len(payload) != size or len(payload) > max_bytes:
        raise ValueError("stock-connect delivery file length is invalid")
    return payload


def _unpack_delivery(
    payload: bytes,
    path: str,
    *,
    max_delivery_bytes: int,
    max_compression_ratio: int,
) -> tuple[bytes, str]:
    """有界解压单文件 licensed ZIP，拒绝路径穿越、超限和压缩炸弹。"""
    if not path.lower().endswith(".zip"):
        if len(payload) > max_delivery_bytes:
            raise ValueError("licensed delivery exceeds the approved byte limit")
        return payload, "text/csv"
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [
            info
            for info in archive.infolist()
            if not info.is_dir() and info.filename.lower().endswith((".csv", ".txt"))
        ]
        if len(members) != 1 or ".." in PurePosixPath(members[0].filename).parts:
            raise ValueError("licensed archive must contain one safe CSV object")
        member = members[0]
        if member.file_size <= 0 or member.file_size > max_delivery_bytes:
            raise ValueError("licensed archive member size is invalid")
        compressed_size = max(1, member.compress_size)
        if member.file_size > compressed_size * max_compression_ratio:
            raise ValueError("licensed archive compression ratio is unsafe")
        with archive.open(member, "r") as stream:
            unpacked = stream.read(max_delivery_bytes + 1)
        if len(unpacked) != member.file_size or len(unpacked) > max_delivery_bytes:
            raise ValueError("licensed archive member length is invalid")
        return unpacked, "text/csv"


def _batch(
    *,
    capability: str,
    normalized: Mapping[str, object],
    fetched: _FetchedObject,
    observed_at: datetime,
) -> ProviderBatch:
    """构造同时保留真实原始字节和规范 JSON 的中立批次。"""
    payload = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return ProviderBatch(
        provider_id=_PROVIDER_ID,
        capability=capability,
        payload=payload,
        raw_payload=fetched.payload,
        observed_at=observed_at,
        content_type="application/json",
        raw_content_type=fetched.content_type,
        upstream_source=fetched.upstream_source,
        adapter_version=_ADAPTER_VERSION,
        schema_fingerprint=_sha256(f"{capability}:{_ADAPTER_VERSION}".encode()),
    )


def _provider_error(code: ProviderErrorCode, message: str, *, retryable: bool) -> ProviderError:
    """构造不泄漏文件路径、账号或上游原文的中立错误。"""
    return ProviderError(code, message, retryable=retryable)


def _unavailable_status_evidence(
    *,
    channel: str,
    direction: str,
    trade_date: date,
    relative_path: str | None,
    reason: str,
    required: bool,
) -> dict[str, object]:
    """记录未通过最终性校验的状态目标，但不泄漏挂载根目录和底层异常。"""
    return {
        "deliveryKind": "CHANNEL_STATUS",
        "capability": CHANNEL_STATUS_CAPABILITY,
        "channel": channel,
        "direction": direction,
        "tradeDate": trade_date.isoformat(),
        "relativePath": relative_path,
        "required": required,
        "available": False,
        "byteSize": None,
        "payloadSha256": None,
        "sidecarSha256": None,
        "finality": None,
        "failureReason": reason,
    }


def _preflight_delivery_manifest(
    *,
    request: ProviderPreflightRequest,
    probe_dates: _StockConnectProbeDates,
    sftp_deliveries: tuple[dict[str, object], ...],
    status_deliveries: tuple[dict[str, object], ...],
    profile_manifest_sha256: str,
    calendar_manifest_sha256: str,
    sftp_delivery_manifest_root_hash: str,
    status_manifest_sha256: str,
    min_partitions_per_minute: int,
    delivery_expiry_safety_seconds: int,
) -> dict[str, object]:
    """构造不含抓取时间的确定性交付清单，并用规范 JSON 摘要冻结全部目标。"""
    available_until_values = [
        datetime.fromisoformat(str(item["availableUntil"]).replace("Z", "+00:00"))
        for item in sftp_deliveries
        if item.get("deliveryKind") == "DAILY_STATISTICS"
        and isinstance(item.get("availableUntil"), str)
    ]
    minimum_execution_window_seconds = (
        math.ceil(len(probe_dates.bundle_targets) / min_partitions_per_minute * 60)
        + delivery_expiry_safety_seconds
    )
    body: dict[str, object] = {
        "schema": _PREFLIGHT_MANIFEST_SCHEMA,
        "providerId": _PROVIDER_ID,
        "request": {
            "datasetCode": request.dataset_code,
            "mode": request.mode,
            "selector": dict(request.selector),
            "dateFrom": request.date_from,
            "dateTo": request.date_to,
            "observationDate": request.observation_date,
        },
        "profileManifestSha256": profile_manifest_sha256,
        "calendarManifestSha256": calendar_manifest_sha256,
        "sftpDeliveryManifestRootHash": sftp_delivery_manifest_root_hash,
        "statusManifestSha256": status_manifest_sha256,
        "availableUntil": (
            _timestamp(min(available_until_values)) if available_until_values else None
        ),
        "minimumExecutionWindowSeconds": minimum_execution_window_seconds,
        "calendarDeliveries": list(probe_dates.calendar_deliveries),
        "sftpDeliveries": list(sftp_deliveries),
        "statusDeliveries": list(status_deliveries),
        "bundleTargets": [
            {
                "channel": channel,
                "direction": direction,
                "tradeDate": trade_date.isoformat(),
            }
            for channel, direction, trade_date in probe_dates.bundle_targets
        ],
    }
    return {
        **body,
        "manifestHash": _canonical_json_sha256(body),
    }


def _request_from_preflight_manifest(
    evidence: Mapping[str, object],
    *,
    timeout_seconds: int,
) -> ProviderPreflightRequest:
    """验证冻结清单外壳和摘要，并恢复唯一可重放的只读探针请求。"""
    expected_keys = {
        "schema",
        "providerId",
        "request",
        "profileManifestSha256",
        "calendarManifestSha256",
        "sftpDeliveryManifestRootHash",
        "statusManifestSha256",
        "availableUntil",
        "minimumExecutionWindowSeconds",
        "calendarDeliveries",
        "sftpDeliveries",
        "statusDeliveries",
        "bundleTargets",
        "manifestHash",
    }
    if (
        set(evidence) != expected_keys
        or evidence.get("schema") != _PREFLIGHT_MANIFEST_SCHEMA
        or evidence.get("providerId") != _PROVIDER_ID
    ):
        raise ValueError("stock-connect preflight manifest envelope is invalid")
    manifest_hash = evidence.get("manifestHash")
    body = {key: value for key, value in evidence.items() if key != "manifestHash"}
    if (
        not isinstance(manifest_hash, str)
        or len(manifest_hash) != 64
        or _canonical_json_sha256(body) != manifest_hash
    ):
        raise ValueError("stock-connect preflight manifest hash is invalid")
    available_until = evidence.get("availableUntil")
    minimum_window = evidence.get("minimumExecutionWindowSeconds")
    if available_until is not None:
        if not isinstance(available_until, str):
            raise ValueError("stock-connect delivery availability is invalid")
        parsed_available_until = datetime.fromisoformat(available_until.replace("Z", "+00:00"))
        if parsed_available_until.tzinfo is None or parsed_available_until.utcoffset() is None:
            raise ValueError("stock-connect delivery availability has no timezone")
    if (
        not isinstance(minimum_window, int)
        or isinstance(minimum_window, bool)
        or minimum_window < 0
    ):
        raise ValueError("stock-connect minimum execution window is invalid")
    raw_request = evidence.get("request")
    if not isinstance(raw_request, Mapping) or set(raw_request) != {
        "datasetCode",
        "mode",
        "selector",
        "dateFrom",
        "dateTo",
        "observationDate",
    }:
        raise ValueError("stock-connect preflight manifest request is invalid")
    selector = raw_request.get("selector")
    if not isinstance(selector, Mapping):
        raise ValueError("stock-connect preflight manifest selector is invalid")
    scalar_fields = {key: raw_request.get(key) for key in ("dateFrom", "dateTo", "observationDate")}
    if any(value is not None and not isinstance(value, str) for value in scalar_fields.values()):
        raise ValueError("stock-connect preflight manifest date field is invalid")
    dataset_code = raw_request.get("datasetCode")
    mode = raw_request.get("mode")
    if not isinstance(dataset_code, str) or not isinstance(mode, str):
        raise ValueError("stock-connect preflight manifest operation is invalid")
    return ProviderPreflightRequest(
        dataset_code=dataset_code,
        mode=mode,
        selector=dict(selector),
        date_from=scalar_fields["dateFrom"],
        date_to=scalar_fields["dateTo"],
        observation_date=scalar_fields["observationDate"],
        timeout_seconds=timeout_seconds,
    )


def stock_connect_bundle_targets_from_evidence(
    evidence: Mapping[str, object],
) -> tuple[tuple[str, str, date], ...]:
    """从已复核清单读取排序且唯一的通道日包目标，拒绝任何隐式重新算日历。"""
    _request_from_preflight_manifest(evidence, timeout_seconds=5)
    raw_targets = evidence.get("bundleTargets")
    if not isinstance(raw_targets, list) or not 1 <= len(raw_targets) <= 50_000:
        raise ValueError("stock-connect frozen bundle target count is invalid")
    targets: list[tuple[str, str, date]] = []
    for raw in raw_targets:
        if not isinstance(raw, Mapping) or set(raw) != {
            "channel",
            "direction",
            "tradeDate",
        }:
            raise ValueError("stock-connect frozen bundle target is invalid")
        channel = raw.get("channel")
        direction = raw.get("direction")
        trade_date_value = raw.get("tradeDate")
        if (
            channel not in {"SH", "SZ"}
            or direction not in {"NORTHBOUND", "SOUTHBOUND"}
            or not isinstance(trade_date_value, str)
        ):
            raise ValueError("stock-connect frozen bundle target value is invalid")
        targets.append((str(channel), str(direction), date.fromisoformat(trade_date_value)))
    if targets != sorted(set(targets), key=lambda item: (item[2], item[0], item[1])):
        raise ValueError("stock-connect frozen bundle targets are duplicate or unordered")
    return tuple(targets)


def stock_connect_delivery_window_from_evidence(
    evidence: Mapping[str, object],
) -> tuple[datetime, int]:
    """读取全窗清单的强制交付截止和完成预算，缺值时禁止进入持久化受理。"""
    _request_from_preflight_manifest(evidence, timeout_seconds=5)
    available_until = evidence.get("availableUntil")
    minimum_window = evidence.get("minimumExecutionWindowSeconds")
    if not isinstance(available_until, str) or not isinstance(minimum_window, int):
        raise ValueError("stock-connect delivery window is unavailable")
    parsed = datetime.fromisoformat(available_until.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stock-connect delivery window has no timezone")
    return parsed, minimum_window


def stock_connect_delivery_manifest_days_from_evidence(
    evidence: Mapping[str, object],
) -> tuple[tuple[date, int, Mapping[str, object]], ...]:
    """把一次全窗预检证据拆成按交易日独立可校验的紧凑页面输入。"""
    _request_from_preflight_manifest(evidence, timeout_seconds=5)
    targets = stock_connect_bundle_targets_from_evidence(evidence)
    calendar_rows = _manifest_mapping_rows(evidence, "calendarDeliveries", maximum=1_000)
    sftp_rows = _manifest_mapping_rows(evidence, "sftpDeliveries", maximum=50_000)
    status_rows = _manifest_mapping_rows(evidence, "statusDeliveries", maximum=50_000)
    calendar_by_year: dict[int, Mapping[str, object]] = {}
    for item in calendar_rows:
        year = item.get("year")
        if not isinstance(year, int) or isinstance(year, bool) or year in calendar_by_year:
            raise ValueError("stock-connect calendar delivery directory is invalid")
        calendar_by_year[year] = item
    common = {
        key: evidence[key]
        for key in (
            "providerId",
            "profileManifestSha256",
            "calendarManifestSha256",
            "sftpDeliveryManifestRootHash",
            "statusManifestSha256",
        )
    }
    trade_dates = sorted(
        {trade_date for _channel, _direction, trade_date in targets},
        reverse=True,
    )
    days: list[tuple[date, int, Mapping[str, object]]] = []
    for trade_date in trade_dates:
        day_targets = [
            {
                "channel": channel,
                "direction": direction,
                "tradeDate": current.isoformat(),
            }
            for channel, direction, current in targets
            if current == trade_date
        ]
        required_calendar_years = {trade_date.year}
        if any(item["direction"] == "SOUTHBOUND" for item in day_targets):
            required_calendar_years.add(trade_date.year - 1)
        try:
            day_calendars = [
                dict(calendar_by_year[year]) for year in sorted(required_calendar_years)
            ]
        except KeyError as error:
            raise ValueError("stock-connect day is missing its frozen calendar delivery") from error
        day_sftp = [
            dict(item) for item in sftp_rows if item.get("tradeDate") == trade_date.isoformat()
        ]
        day_status = [
            dict(item) for item in status_rows if item.get("tradeDate") == trade_date.isoformat()
        ]
        if len(day_status) != len(day_targets) or not day_sftp:
            raise ValueError("stock-connect day delivery evidence is incomplete")
        day_evidence: dict[str, object] = {
            "schema": _DELIVERY_MANIFEST_DAY_SCHEMA,
            **common,
            "calendarDeliveries": day_calendars,
            "sftpDeliveries": day_sftp,
            "statusDeliveries": day_status,
            "bundleTargets": day_targets,
        }
        days.append((trade_date, len(day_targets), day_evidence))
    if sum(item[1] for item in days) != len(targets):
        raise ValueError("stock-connect day target count differs from full manifest")
    return tuple(days)


def stock_connect_preflight_evidence_from_delivery_page(
    page_evidence: Mapping[str, object],
) -> dict[str, object]:
    """把数据库中的一页按日证据恢复为 adapter 可批量复核的冻结清单。"""
    if (
        set(page_evidence) != {"schema", "days"}
        or page_evidence.get("schema") != _DELIVERY_MANIFEST_PAGE_SCHEMA
    ):
        raise ValueError("stock-connect delivery page envelope is invalid")
    raw_days = page_evidence.get("days")
    if not isinstance(raw_days, list) or not 1 <= len(raw_days) <= 20:
        raise ValueError("stock-connect delivery page days are invalid")
    common_keys = (
        "providerId",
        "profileManifestSha256",
        "calendarManifestSha256",
        "sftpDeliveryManifestRootHash",
        "statusManifestSha256",
    )
    common: dict[str, object] | None = None
    target_count = 0
    calendar_rows: list[Mapping[str, object]] = []
    sftp_rows: list[Mapping[str, object]] = []
    status_rows: list[Mapping[str, object]] = []
    target_rows: list[Mapping[str, object]] = []
    page_dates: list[date] = []
    for raw_day in raw_days:
        if not isinstance(raw_day, Mapping) or set(raw_day) != {
            "tradeDate",
            "targetCount",
            "evidence",
        }:
            raise ValueError("stock-connect delivery page day is invalid")
        trade_date_value = raw_day.get("tradeDate")
        raw_target_count = raw_day.get("targetCount")
        day_evidence = raw_day.get("evidence")
        if (
            not isinstance(trade_date_value, str)
            or not isinstance(raw_target_count, int)
            or isinstance(raw_target_count, bool)
            or not isinstance(day_evidence, Mapping)
        ):
            raise ValueError("stock-connect delivery page day value is invalid")
        expected_day_keys = {
            "schema",
            *common_keys,
            "calendarDeliveries",
            "sftpDeliveries",
            "statusDeliveries",
            "bundleTargets",
        }
        if (
            set(day_evidence) != expected_day_keys
            or day_evidence.get("schema") != _DELIVERY_MANIFEST_DAY_SCHEMA
        ):
            raise ValueError("stock-connect delivery day evidence is invalid")
        current_common = {key: day_evidence[key] for key in common_keys}
        if common is None:
            common = current_common
        elif current_common != common:
            raise ValueError("stock-connect delivery page common evidence drifted")
        page_date = date.fromisoformat(trade_date_value)
        page_dates.append(page_date)
        day_targets = _manifest_mapping_rows(day_evidence, "bundleTargets", maximum=4)
        if raw_target_count != len(day_targets) or any(
            item.get("tradeDate") != trade_date_value for item in day_targets
        ):
            raise ValueError("stock-connect delivery day target count differs")
        target_count += raw_target_count
        target_rows.extend(day_targets)
        calendar_rows.extend(_manifest_mapping_rows(day_evidence, "calendarDeliveries", maximum=2))
        sftp_rows.extend(_manifest_mapping_rows(day_evidence, "sftpDeliveries", maximum=8))
        status_rows.extend(_manifest_mapping_rows(day_evidence, "statusDeliveries", maximum=4))
    if common is None or page_dates != sorted(set(page_dates), reverse=True):
        raise ValueError("stock-connect delivery page dates are duplicate or unordered")
    calendars = _unique_manifest_rows(calendar_rows, keys=("year",))
    sftp = _unique_manifest_rows(
        sftp_rows,
        keys=("deliveryKind", "channel", "tradeDate", "issuedDate"),
    )
    statuses = _unique_manifest_rows(
        status_rows,
        keys=("channel", "direction", "tradeDate"),
    )
    targets = _unique_manifest_rows(
        target_rows,
        keys=("tradeDate", "channel", "direction"),
    )
    if len(targets) != target_count:
        raise ValueError("stock-connect delivery page target total differs")
    target_channels = {str(item["channel"]) for item in targets}
    target_directions = {str(item["direction"]) for item in targets}
    daily_deadlines = [
        datetime.fromisoformat(str(item["availableUntil"]).replace("Z", "+00:00"))
        for item in sftp
        if item.get("deliveryKind") == "DAILY_STATISTICS"
        and isinstance(item.get("availableUntil"), str)
    ]
    if not daily_deadlines:
        raise ValueError("stock-connect delivery page has no mandatory deadline")
    body: dict[str, object] = {
        "schema": _PREFLIGHT_MANIFEST_SCHEMA,
        **common,
        "request": {
            "datasetCode": "market.stock_connect.overview.bundle",
            "mode": "DATE_RANGE",
            "selector": {
                "kind": "STOCK_CONNECT",
                "operation": "MARKET",
                "channel": (
                    "ALL" if target_channels == {"SH", "SZ"} else next(iter(target_channels))
                ),
                "direction": (
                    None
                    if target_directions == {"NORTHBOUND", "SOUTHBOUND"}
                    else next(iter(target_directions))
                ),
            },
            "dateFrom": min(page_dates).isoformat(),
            "dateTo": max(page_dates).isoformat(),
            "observationDate": None,
        },
        "availableUntil": _timestamp(min(daily_deadlines)),
        # 页面复核不承担控制面的全窗容量门；全窗预算已由 manifest header 单独冻结。
        "minimumExecutionWindowSeconds": 0,
        "calendarDeliveries": calendars,
        "sftpDeliveries": sftp,
        "statusDeliveries": statuses,
        "bundleTargets": targets,
    }
    restored = {**body, "manifestHash": _canonical_json_sha256(body)}
    _request_from_preflight_manifest(restored, timeout_seconds=5)
    stock_connect_bundle_targets_from_evidence(restored)
    return restored


def _manifest_mapping_rows(
    evidence: Mapping[str, object],
    field: str,
    *,
    maximum: int,
) -> list[Mapping[str, object]]:
    """读取有界对象数组，不接受标量、空数组或超大来源证据。"""
    raw = evidence.get(field)
    if not isinstance(raw, list) or not 1 <= len(raw) <= maximum:
        raise ValueError(f"stock-connect {field} is invalid")
    if any(not isinstance(item, Mapping) for item in raw):
        raise ValueError(f"stock-connect {field} row is invalid")
    return [item for item in raw if isinstance(item, Mapping)]


def _unique_manifest_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    keys: tuple[str, ...],
) -> list[dict[str, object]]:
    """按业务键合并完全相同的跨日证据，冲突重复立即阻断页面复核。"""
    unique: dict[tuple[object, ...], dict[str, object]] = {}
    for item in rows:
        key = tuple(item.get(name) for name in keys)
        value = dict(item)
        existing = unique.get(key)
        if existing is not None and existing != value:
            raise ValueError("stock-connect delivery page contains conflicting evidence")
        unique[key] = value
    return [unique[key] for key in sorted(unique, key=lambda value: tuple(map(str, value)))]


def _batch_revalidation_request(
    evidence: Mapping[str, object],
    *,
    target_keys: tuple[str, ...],
    timeout_seconds: int,
) -> ProviderPreflightRequest:
    """把内部公平批次键还原为自动日期窗，无需用户手工拆分历史范围。"""
    frozen = stock_connect_bundle_targets_from_evidence(evidence)
    by_key = {
        f"stock-connect:{trade_date.isoformat()}:{channel}:{direction}": (
            channel,
            direction,
            trade_date,
        )
        for channel, direction, trade_date in frozen
    }
    if not target_keys or len(set(target_keys)) != len(target_keys):
        raise ValueError("stock-connect revalidation target keys are invalid")
    try:
        selected = tuple(by_key[key] for key in target_keys)
    except KeyError as error:
        raise ValueError(
            "stock-connect revalidation target is outside the frozen manifest"
        ) from error
    channels = {channel for channel, _direction, _trade_date in selected}
    directions = {direction for _channel, direction, _trade_date in selected}
    dates = {trade_date for _channel, _direction, trade_date in selected}
    return ProviderPreflightRequest(
        dataset_code="market.stock_connect.overview.bundle",
        mode="DATE_RANGE",
        selector={
            "kind": "STOCK_CONNECT",
            "operation": "MARKET",
            "channel": "ALL" if channels == {"SH", "SZ"} else next(iter(channels)),
            "direction": (
                None if directions == {"NORTHBOUND", "SOUTHBOUND"} else next(iter(directions))
            ),
        },
        date_from=min(dates).isoformat(),
        date_to=max(dates).isoformat(),
        observation_date=None,
        timeout_seconds=timeout_seconds,
    )


def _revalidated_evidence_matches(
    *,
    frozen: Mapping[str, object],
    current: Mapping[str, object],
    target_keys: tuple[str, ...] | None,
) -> bool:
    """比较全窗摘要或当前批次涉及的日历、SFTP 对象和状态 sidecar 版本。"""
    if target_keys is None:
        return current.get("manifestHash") == frozen.get("manifestHash")
    for field in (
        "profileManifestSha256",
        "calendarManifestSha256",
        "sftpDeliveryManifestRootHash",
        "statusManifestSha256",
    ):
        if current.get(field) != frozen.get(field):
            return False

    def index(
        raw: object,
        *,
        keys: tuple[str, ...],
    ) -> dict[tuple[object, ...], Mapping[str, object]] | None:
        """按确定性业务键建立交付索引，重复键立即判为清单损坏。"""
        if not isinstance(raw, list):
            return None
        result: dict[tuple[object, ...], Mapping[str, object]] = {}
        for item in raw:
            if not isinstance(item, Mapping):
                return None
            key = tuple(item.get(name) for name in keys)
            if key in result:
                return None
            result[key] = item
        return result

    specifications = (
        ("calendarDeliveries", ("year",)),
        (
            "sftpDeliveries",
            ("deliveryKind", "channel", "tradeDate", "issuedDate"),
        ),
        (
            "statusDeliveries",
            ("channel", "direction", "tradeDate"),
        ),
    )
    for field, keys in specifications:
        frozen_index = index(frozen.get(field), keys=keys)
        current_index = index(current.get(field), keys=keys)
        if frozen_index is None or current_index is None:
            return False
        if any(frozen_index.get(key) != value for key, value in current_index.items()):
            return False
    try:
        current_targets = {
            f"stock-connect:{trade_date.isoformat()}:{channel}:{direction}"
            for channel, direction, trade_date in stock_connect_bundle_targets_from_evidence(
                current
            )
        }
    except (TypeError, ValueError):
        return False
    return set(target_keys) <= current_targets


def _canonical_json_sha256(value: object) -> str:
    """对仅含 JSON 标量的内部证据生成跨进程稳定 SHA-256。"""
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _available_readiness_evidence(
    *,
    probe_dates: _StockConnectProbeDates,
    calendar_manifest_sha256: str,
    evidence_observed_at: datetime,
) -> dict[str, object]:
    """把已摘要验证的逐日官方判定封装为独立 readiness 证据。"""
    days = [dict(item) for item in probe_dates.readiness_calendar_days]
    if not days:
        raise ValueError("stock-connect readiness calendar evidence is empty")
    identity = {
        "calendarManifestSha256": calendar_manifest_sha256,
        "days": days,
    }
    return {
        "schema": _READINESS_EVIDENCE_SCHEMA,
        "calendarDataVersion": _canonical_json_sha256(identity),
        "calendarManifestSha256": calendar_manifest_sha256,
        "evidenceObservedAt": _timestamp(evidence_observed_at),
        "days": days,
    }


def _unavailable_readiness_evidence(
    *,
    scope: _StockConnectPreflightScope,
    calendar_manifest_sha256: str,
    evidence_observed_at: datetime,
) -> dict[str, object]:
    """在年度日历不可取得时冻结 UNKNOWN 全窗，禁止把缺记录猜成休市。"""
    days = [
        {
            "calendarDate": calendar_date.isoformat(),
            "channel": channel,
            "direction": direction,
            "calendarState": "UNKNOWN",
            "sourceFileSha256": None,
            "sourcePublicationAt": None,
            "publicationAvailability": "SOURCE_MISSING",
            "sourceObservedAt": None,
        }
        for channel in scope.channels
        for direction in scope.directions
        for calendar_date in _inclusive_dates(dict(scope.channel_starts)[channel], scope.end)
    ]
    days.sort(
        key=lambda item: (
            str(item["calendarDate"]),
            str(item["channel"]),
            str(item["direction"]),
        )
    )
    identity = {
        "calendarManifestSha256": calendar_manifest_sha256,
        "days": days,
    }
    return {
        "schema": _READINESS_EVIDENCE_SCHEMA,
        "calendarDataVersion": _canonical_json_sha256(identity),
        "calendarManifestSha256": calendar_manifest_sha256,
        "evidenceObservedAt": _timestamp(evidence_observed_at),
        "days": days,
    }


def _inclusive_dates(start: date, end: date) -> tuple[date, ...]:
    """返回包含端公历序列，供全量日历证明使用而不依赖工作日猜测。"""
    if start > end:
        raise ValueError("stock-connect readiness date window is inverted")
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _preflight_scope(
    request: ProviderPreflightRequest,
    *,
    anchor_date: date,
) -> _StockConnectPreflightScope:
    """把控制面已规范化请求冻结成 adapter 可验证的最小来源范围。"""
    if (
        request.dataset_code != "market.stock_connect.overview.bundle"
        or request.mode not in {"FULL", "INCREMENTAL", "DATE_RANGE"}
        or request.observation_date is not None
        or set(request.selector) != {"kind", "operation", "channel", "direction"}
        or request.selector.get("kind") != "STOCK_CONNECT"
        or request.selector.get("operation") != "MARKET"
    ):
        raise ValueError("stock-connect preflight request is invalid")
    channel_value = request.selector.get("channel")
    if channel_value not in {"ALL", "SH", "SZ"}:
        raise ValueError("stock-connect preflight channel is invalid")
    channels = ("SH", "SZ") if channel_value == "ALL" else (str(channel_value),)
    direction_value = request.selector.get("direction")
    if direction_value not in {"NORTHBOUND", "SOUTHBOUND", None}:
        raise ValueError("stock-connect preflight direction is invalid")
    directions = (
        ("NORTHBOUND", "SOUTHBOUND") if direction_value is None else (str(direction_value),)
    )
    if request.mode == "DATE_RANGE":
        if request.date_from is None or request.date_to is None:
            raise ValueError("stock-connect date range is incomplete")
        start = date.fromisoformat(request.date_from)
        end = date.fromisoformat(request.date_to)
        channel_starts = tuple((channel, start) for channel in channels)
    elif request.mode == "INCREMENTAL":
        end = anchor_date
        start = end - timedelta(days=31)
        channel_starts = tuple((channel, start) for channel in channels)
    else:
        end = anchor_date
        channel_starts = tuple(
            (channel, _BUNDLE_FIRST_ISSUE_DATES[channel]) for channel in channels
        )
        start = min(value for _channel, value in channel_starts)
    if start > end or end > anchor_date:
        raise ValueError("stock-connect preflight date window is invalid")
    if request.mode == "DATE_RANGE" and any(
        start < _BUNDLE_FIRST_ISSUE_DATES[channel] for channel in channels
    ):
        raise ValueError("stock-connect preflight starts before the product first issue date")
    return _StockConnectPreflightScope(
        channels=channels,
        directions=directions,
        channel_starts=channel_starts,
        end=end,
    )


def _deadline_remaining(deadline: float) -> float:
    """返回同一 preflight 总 deadline 的剩余秒数，耗尽时立即停止后续 I/O。"""
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("stock-connect preflight deadline exceeded")
    return remaining


def _unavailable_preflight_components(
    scope: _StockConnectPreflightScope,
    *,
    calendar_reason: str,
) -> list[ProviderPreflightComponent]:
    """日历失败时显式标记所有依赖组件，禁止把未执行探针当成成功。"""
    checks = [
        ProviderPreflightComponent(
            component="hkex-calendar-https",
            accepted=False,
            reason=calendar_reason,
        ),
        ProviderPreflightComponent(
            component="sftp-authentication",
            accepted=False,
            reason="DEPENDENCY_UNAVAILABLE",
        ),
    ]
    checks.extend(
        ProviderPreflightComponent(
            component=f"hkex-daily-statistics-{channel.lower()}",
            accepted=False,
            reason="DEPENDENCY_UNAVAILABLE",
        )
        for channel in scope.channels
    )
    if "SOUTHBOUND" in scope.directions:
        checks.append(
            ProviderPreflightComponent(
                component="hkex-securities-master",
                accepted=False,
                reason="DEPENDENCY_UNAVAILABLE",
            )
        )
    checks.extend(
        ProviderPreflightComponent(
            component=f"status-{channel.lower()}-{direction.lower()}",
            accepted=False,
            reason="DEPENDENCY_UNAVAILABLE",
        )
        for channel in scope.channels
        for direction in scope.directions
    )
    return checks


def _validate_official_mounts(config: OfficialStockConnectConfig) -> None:
    """在 adapter 注册前确认所有静态只读挂载存在，避免任务排队后才发现配置缺失。"""
    required_files = (
        config.sftp_private_key_path,
        config.sftp_known_hosts_path,
        config.securities_master_profile_manifest_path,
    )
    if any(not path.is_file() for path in required_files):
        raise ValueError("official stock-connect read-only file mount is unavailable")
    if not config.status_delivery_root.is_dir():
        raise ValueError("official stock-connect status landing mount is unavailable")


def _required_publication_at(fetched: _FetchedObject) -> datetime:
    """要求声明 `REPORTED` 的来源确实提供 publication 时间。"""
    if fetched.published_at is None:
        raise ValueError("official source publication timestamp is unavailable")
    return fetched.published_at


def _require_profile(actual: str, expected: str) -> None:
    """只允许冻结 schema profile，升级必须显式变更配置与 adapter。"""
    if actual != expected:
        raise ValueError("official delivery schema profile is not approved")


def _direction_from_text(value: str) -> str | None:
    """从官方英文方向文本识别北向或南向，不接受模糊包含。"""
    normalized = _normalize_header(value)
    if normalized in {"nb", "northbound", "northboundtrading"} or "northbound" in normalized:
        return "NORTHBOUND"
    if normalized in {"sb", "southbound", "southboundtrading"} or "southbound" in normalized:
        return "SOUTHBOUND"
    return None


def _normalize_header(value: str) -> str:
    """移除列头标点和单位后规范化英文标识。"""
    without_unit = re.sub(r"\([^)]*\)", "", value)
    return re.sub(r"[^a-z0-9]+", "", without_unit.lower())


def _normalize_security_code(value: str) -> str:
    """规范化交易所数字代码；保留 A 股和港股代码中的前导零。"""
    normalized = value.strip().upper()
    if normalized.startswith(("SH.", "SZ.", "HK.")):
        normalized = normalized.split(".", maxsplit=1)[1]
    if normalized.endswith(".0") and normalized[:-2].isdigit():
        normalized = normalized[:-2]
    return normalized if normalized.isdigit() else ""


def _unit_multiplier(header: str) -> Decimal:
    """从明确单位文本转换到 base currency；未写倍率时保持一，不猜测“亿元”。"""
    normalized = header.lower()
    if "million" in normalized or re.search(r"\bmn\b", normalized):
        return Decimal(1_000_000)
    if "thousand" in normalized:
        return Decimal(1_000)
    return Decimal(1)


def _scaled_decimal(value: str, multiplier: Decimal) -> str | None:
    """解析来源十进制并按明确列头倍率转成基础货币单位。"""
    normalized = _decimal_text(value)
    if normalized is None:
        return None
    return format(Decimal(normalized) * multiplier, "f")


def _decimal_text(value: str) -> str | None:
    """解析带千分位和货币符号的精确金额；破折号表示缺失而不是零。"""
    normalized = value.strip()
    if not normalized or normalized in {"-", "--", "N/A", "NA", "NULL"}:
        return None
    normalized = normalized.replace(",", "").replace("HK$", "").replace("RMB", "").strip()
    decimal_value = Decimal(normalized)
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ValueError("official amount must be finite and non-negative")
    return format(decimal_value, "f")


def _required_decimal(value: str | None, label: str) -> str:
    """读取必须存在的非负金额。"""
    normalized = _decimal_text(value or "")
    if normalized is None:
        raise ValueError(f"{label} is required")
    return normalized


def _optional_non_negative_int(value: str | None) -> int | None:
    """解析可选非负整数，禁止把小数笔数四舍五入。"""
    if value is None or not value.strip():
        return None
    normalized = value.replace(",", "").strip()
    if not normalized.isdigit():
        raise ValueError("official count is invalid")
    return int(normalized)


def _value(row: Sequence[str], index: int | None) -> str:
    """安全读取可选列位置，短扩展行返回空值。"""
    return "" if index is None or index >= len(row) else row[index].strip()


def _row_has_values(row: Sequence[str], header: Mapping[str, int]) -> bool:
    """判断当前行是否包含 header 管辖的数据列。"""
    return any(_value(row, header.get(field)) for field in ("turnover", "code", "rank"))


def _active_rank(item: Mapping[str, object]) -> int:
    """读取解析后活跃榜的正整数来源名次，拒绝布尔值和类型漂移。"""
    value = item.get("rankNo")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("HKEX active-security rank is invalid")
    return value


def _parse_flexible_date(value: str) -> date:
    """解析官方常见 ISO、YYYYMMDD 或 DD/MM/YYYY 日期。"""
    normalized = value.strip()
    for pattern in ("%Y-%m-%d", "%Y%m%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(normalized, pattern).date()
        except ValueError:
            continue
    raise ValueError("official date is invalid")


def _parse_status_time(value: str, *, trade_date: date) -> datetime:
    """解析具名落地时间，日期缺失时只与请求业务日期组合，不使用文件抓取日。"""
    normalized = value.strip()
    if not normalized:
        raise ValueError("status observed time is required")
    for pattern in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y%m%d-%H:%M:%S.%f",
        "%Y%m%d%H%M%S%f",
        "%H:%M:%S",
        "%H%M%S",
    ):
        try:
            parsed = datetime.strptime(normalized, pattern)
        except ValueError:
            continue
        if pattern in {"%H:%M:%S", "%H%M%S"}:
            parsed = datetime.combine(trade_date, parsed.time())
        if parsed.tzinfo is None:
            from zoneinfo import ZoneInfo

            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Hong_Kong"))
        return parsed.astimezone(UTC)
    raise ValueError("status observed time is invalid")


def _optional_bool(value: str) -> bool | None:
    """解析网关显式布尔开关；字段未交付时保持空。"""
    if not value.strip():
        return None
    normalized = value.strip().upper()
    if normalized in {"1", "Y", "YES", "TRUE", "OPEN", "ALLOWED"}:
        return True
    if normalized in {"0", "N", "NO", "FALSE", "CLOSED", "DENIED"}:
        return False
    raise ValueError("status order-acceptance flag is invalid")


def _http_datetime(value: str | None) -> datetime | None:
    """解析 HTTP Last-Modified；缺失或无效时由调用方 fail-closed。"""
    if value is None:
        return None
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(UTC)


def _sha256(value: bytes) -> str:
    """计算交付或 schema 的 SHA-256。"""
    return hashlib.sha256(value).hexdigest()


def _timestamp(value: datetime) -> str:
    """把带时区时间稳定输出为 UTC RFC 3339。"""
    if value.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
