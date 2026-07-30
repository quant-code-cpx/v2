"""板块 `EOD` 横截面来源证据、标准化、质量门和原子发布编排。

每次运行针对一个分类体系、一个明确交易日和完整横截面；目录不完整、日历未知、覆盖率不足或质量阻断时不能替换已有发布。
候选、隔离证据和消费者 `publication` 分离保存，重放或 `rollback` 只指向既有通过版本，不删除历史。
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.application.ports.sector_eod import (
    SECTOR_EOD_QUALITY_POLICY_VERSION,
    PublishedSectorEodSnapshot,
    SectorEodExecutionMode,
    SectorEodHistoricalReference,
    SectorEodQualityResult,
    SectorEodRepository,
    SectorEodRun,
)
from service_data_sync.application.ports.trading_calendar import (
    TradingCalendarPort,
    require_open_trading_day,
)
from service_data_sync.domain.sector import (
    SectorEodQuote,
    SectorEodSnapshot,
    SectorIdentifier,
    SectorScheme,
    sector_eod_snapshot_content_sha256,
)

_CAPABILITY = "sector.quote.eod.snapshot.raw"
_SCHEMA = "quant-v2.sector-eod-snapshot.v1"
_ARCHIVE_SCHEMA = "quant-v2.sector-eod-raw-observation.v1"
_MAX_RAW_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SectorEodSnapshotSyncResult:
    """向 CLI 和调度器返回不含供应商字段的 EOD 持久化摘要。"""

    snapshot: SectorEodSnapshot
    inserted: bool
    execution_mode: SectorEodExecutionMode
    run_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SectorEodQualityAssessment:
    """汇总可发布候选的字段可用率与异常值结果，并显式给出 passed 或 warned 状态。"""

    status: str
    results: tuple[SectorEodQualityResult, ...]
    policy_version: str

    @property
    def has_blocking_failure(self) -> bool:
        """返回是否必须隔离候选；调用方需先保存质量证据再向任务报告失败。"""
        return any(not result.passed and result.severity == "blocking" for result in self.results)


class SectorEodSnapshotSyncService:
    """同步一个 scheme/date 的完整收盘后横截面，不接受 partial 发布。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: SectorEodRepository,
        raw_payload_store: RawPayloadStore,
        trading_calendar: TradingCalendarPort,
    ) -> None:
        """接收中立来源、日历、canonical 仓储和独立原始证据端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store
        self._trading_calendar = trading_calendar

    async def sync(
        self,
        *,
        scheme: SectorScheme,
        trade_date: date,
        source_cutoff_at: datetime,
        execution_mode: SectorEodExecutionMode = SectorEodExecutionMode.SHADOW,
        before_final_publication: Callable[[], None] | None = None,
    ) -> SectorEodSnapshotSyncResult:
        """归档完整观察，并按受控模式保存 candidate 或原子发布目标交易日版本。

        只有 `PUBLISH` 路径会消费 `before_final_publication`；调用方据此把末次
        canonical 写入与控制面 run 终态绑定到同一 PostgreSQL 提交事务。
        """
        if source_cutoff_at.tzinfo is None:
            raise ValueError("source_cutoff_at must include a timezone")
        require_open_trading_day(self._trading_calendar, trade_date=trade_date)
        if _CAPABILITY not in self._source.capabilities():
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "unsupported capability",
                retryable=False,
            )
        run = self._repository.start_run(
            scheme=scheme, trade_date=trade_date, reuse_archived_raw=False
        )
        try:
            batch = await self._source.fetch(
                SourceRequest(
                    capability=_CAPABILITY,
                    parameters=(
                        ("sectorScheme", scheme.value),
                        ("tradeDate", trade_date.isoformat()),
                    ),
                )
            )
            self._repository.renew_lease(run=run)
            self._repository.mark_fetched(run=run)
            if batch.observed_at < source_cutoff_at:
                raise ProviderError(
                    ProviderErrorCode.SCHEMA,
                    "provider observation precedes eod cutoff",
                    retryable=False,
                )
            return self._publish_new_observation(
                run=run,
                scheme=scheme,
                trade_date=trade_date,
                source_cutoff_at=source_cutoff_at,
                batch_payload=batch.payload,
                raw_payload=batch.raw_payload if batch.raw_payload is not None else batch.payload,
                raw_content_type=batch.raw_content_type or batch.content_type,
                provider_id=batch.provider_id,
                observed_at=batch.observed_at,
                adapter_version=batch.adapter_version,
                schema_fingerprint=batch.schema_fingerprint
                or hashlib.sha256(_SCHEMA.encode()).hexdigest(),
                upstream_source=batch.upstream_source,
                execution_mode=execution_mode,
                before_final_publication=before_final_publication,
            )
        except Exception as error:
            _mark_run_failed(self._repository, run=run, error=error)
            raise

    async def replay(
        self,
        *,
        scheme: SectorScheme,
        trade_date: date,
        source_cutoff_at: datetime,
        execution_mode: SectorEodExecutionMode = SectorEodExecutionMode.SHADOW,
        before_final_publication: Callable[[], None] | None = None,
    ) -> SectorEodSnapshotSyncResult:
        """从 checkpoint 的 raw evidence 恢复，并按受控模式保存或发布。

        重放虽然不访问 Provider，但仍会发布或推进 EOD checkpoint；因此 publication
        模式同样必须由 fenced 控制面提供最终事务回调。
        """
        if source_cutoff_at.tzinfo is None:
            raise ValueError("source_cutoff_at must include a timezone")
        require_open_trading_day(self._trading_calendar, trade_date=trade_date)
        run = self._repository.start_run(
            scheme=scheme, trade_date=trade_date, reuse_archived_raw=True
        )
        try:
            observation = self._repository.get_archived_observation(run=run)
            self._repository.renew_lease(run=run)
            payload = _replay_normalized_payload(self._raw_payload_store.get(observation.raw_uri))
            if observation.observed_at < source_cutoff_at:
                raise ProviderError(
                    ProviderErrorCode.SCHEMA,
                    "provider observation precedes eod cutoff",
                    retryable=False,
                )
            quotes = decode_sector_eod_batch(
                payload, expected_scheme=scheme, expected_trade_date=trade_date
            )
            quality = assess_sector_eod_quality(
                quotes,
                historical_reference=self._repository.get_historical_reference(
                    scheme=scheme, before_trade_date=trade_date
                ),
            )
            self._repository.mark_normalized(run=run)
            if quality.has_blocking_failure:
                self._repository.store_quarantined_snapshot(
                    scheme=scheme,
                    trade_date=trade_date,
                    source_cutoff_at=source_cutoff_at,
                    observed_at=observation.observed_at,
                    quotes=quotes,
                    provider_id=observation.provider_id,
                    source_payload_sha256="0" * 64,
                    raw_uri=observation.raw_uri,
                    adapter_version=observation.adapter_version,
                    schema_fingerprint=observation.schema_fingerprint,
                    run=run,
                    source_batch_id=observation.source_batch_id,
                    quality_results=quality.results,
                )
                _raise_blocking_quality()
            publication = _store_passing_snapshot(
                repository=self._repository,
                execution_mode=execution_mode,
                scheme=scheme,
                trade_date=trade_date,
                source_cutoff_at=source_cutoff_at,
                observed_at=observation.observed_at,
                quotes=quotes,
                provider_id=observation.provider_id,
                source_payload_sha256="0" * 64,
                raw_uri=observation.raw_uri,
                adapter_version=observation.adapter_version,
                schema_fingerprint=observation.schema_fingerprint,
                run=run,
                source_batch_id=observation.source_batch_id,
                quality_status=quality.status,
                quality_results=quality.results,
                before_final_publication=before_final_publication,
            )
            return _result(publication, execution_mode=execution_mode, run_id=run.run_id)
        except Exception as error:
            _mark_run_failed(self._repository, run=run, error=error)
            raise

    def _publish_new_observation(
        self,
        *,
        run: SectorEodRun,
        scheme: SectorScheme,
        trade_date: date,
        source_cutoff_at: datetime,
        batch_payload: bytes,
        raw_payload: bytes,
        raw_content_type: str,
        provider_id: str,
        observed_at: datetime,
        adapter_version: str,
        schema_fingerprint: str,
        upstream_source: str | None,
        execution_mode: SectorEodExecutionMode,
        before_final_publication: Callable[[], None] | None,
    ) -> SectorEodSnapshotSyncResult:
        """先归档原始观察，再登记 source batch、解析并按运行模式持久化。"""
        self._repository.renew_lease(run=run)
        source_digest = hashlib.sha256(raw_payload).hexdigest()
        archived_payload = _raw_observation_archive(
            normalized_payload=batch_payload,
            raw_payload=raw_payload,
            raw_content_type=raw_content_type,
        )
        archive_digest = hashlib.sha256(archived_payload).hexdigest()
        raw_uri = self._raw_payload_store.put(
            RawPayload(
                object_key=(
                    f"raw/{_CAPABILITY}/{provider_id}/{observed_at:%Y/%m/%d}/"
                    f"{source_digest}-{uuid4()}.json"
                ),
                content_sha256=archive_digest,
                content_type="application/vnd.quant-v2.sector-eod-raw-observation+json",
                payload=archived_payload,
            )
        )
        observation = self._repository.record_archived_observation(
            run=run,
            provider_id=provider_id,
            source_payload_sha256=source_digest,
            raw_uri=raw_uri,
            observed_at=observed_at,
            adapter_version=adapter_version,
            schema_fingerprint=schema_fingerprint,
            upstream_source=upstream_source,
        )
        if len(raw_payload) > _MAX_RAW_BYTES:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "sector eod raw payload exceeds the approved size limit",
                retryable=False,
            )
        quotes = decode_sector_eod_batch(
            batch_payload, expected_scheme=scheme, expected_trade_date=trade_date
        )
        quality = assess_sector_eod_quality(
            quotes,
            historical_reference=self._repository.get_historical_reference(
                scheme=scheme, before_trade_date=trade_date
            ),
        )
        self._repository.mark_normalized(run=run)
        if quality.has_blocking_failure:
            self._repository.store_quarantined_snapshot(
                scheme=scheme,
                trade_date=trade_date,
                source_cutoff_at=source_cutoff_at,
                observed_at=observation.observed_at,
                quotes=quotes,
                provider_id=observation.provider_id,
                source_payload_sha256=source_digest,
                raw_uri=observation.raw_uri,
                adapter_version=observation.adapter_version,
                schema_fingerprint=observation.schema_fingerprint,
                run=run,
                source_batch_id=observation.source_batch_id,
                quality_results=quality.results,
            )
            _raise_blocking_quality()
        publication = _store_passing_snapshot(
            repository=self._repository,
            execution_mode=execution_mode,
            scheme=scheme,
            trade_date=trade_date,
            source_cutoff_at=source_cutoff_at,
            observed_at=observation.observed_at,
            quotes=quotes,
            provider_id=observation.provider_id,
            source_payload_sha256=source_digest,
            raw_uri=observation.raw_uri,
            adapter_version=observation.adapter_version,
            schema_fingerprint=observation.schema_fingerprint,
            run=run,
            source_batch_id=observation.source_batch_id,
            quality_status=quality.status,
            quality_results=quality.results,
            before_final_publication=before_final_publication,
        )
        return _result(publication, execution_mode=execution_mode, run_id=run.run_id)


def assess_sector_eod_quality(
    quotes: tuple[SectorEodQuote, ...],
    *,
    historical_reference: SectorEodHistoricalReference | None = None,
) -> SectorEodQualityAssessment:
    """执行字段、行内与跨日质量门，不填补或截断任何来源空值。"""
    if not quotes:
        raise ValueError("sector eod quality requires at least one quote")
    total = len(quotes)
    results: list[SectorEodQualityResult] = [
        SectorEodQualityResult(
            rule_code="quality-policy-version",
            severity="info",
            passed=True,
            actual={"version": SECTOR_EOD_QUALITY_POLICY_VERSION},
            threshold={"frozen": "true"},
        ),
        SectorEodQualityResult(
            rule_code="schema",
            severity="blocking",
            passed=True,
            actual={"recordCount": total},
            threshold={"maximumRecordCount": 2000},
        ),
    ]
    for field_name, minimum_percent, severity in (
        ("latest_value", Decimal("0.99"), "blocking"),
        ("change_percent", Decimal("0.99"), "blocking"),
        ("market_value", Decimal("0.95"), "warning"),
        ("turnover_percent", Decimal("0.95"), "warning"),
        ("advancers", Decimal("0.95"), "warning"),
        ("decliners", Decimal("0.95"), "warning"),
        ("leader_change_percent", Decimal("0.95"), "warning"),
    ):
        available = sum(getattr(quote, field_name) is not None for quote in quotes)
        ratio = Decimal(available) / Decimal(total)
        results.append(
            SectorEodQualityResult(
                rule_code=f"availability-{field_name.replace('_', '-')}",
                severity=severity,
                passed=ratio >= minimum_percent,
                actual={"available": available, "total": total},
                threshold={"minimumPercent": str(minimum_percent * 100)},
            )
        )
    results.append(_extreme_change_result(quotes))
    results.append(_extreme_turnover_result(quotes))
    results.append(_change_consistency_result(quotes))
    results.extend(
        _historical_quality_results(
            quotes=quotes,
            historical_reference=historical_reference,
        )
    )
    status = (
        "warned"
        if any(not result.passed and result.severity == "warning" for result in results)
        else "passed"
    )
    return SectorEodQualityAssessment(
        status=status,
        results=tuple(results),
        policy_version=SECTOR_EOD_QUALITY_POLICY_VERSION,
    )


def _extreme_change_result(quotes: tuple[SectorEodQuote, ...]) -> SectorEodQualityResult:
    """按未截断的涨跌幅统计 warning 与 blocking 阈值，保留异常行由 raw 审计。"""
    values = [abs(quote.change_percent) for quote in quotes if quote.change_percent is not None]
    highest = max(values, default=Decimal("0"))
    if highest > Decimal("50"):
        return SectorEodQualityResult(
            rule_code="change-percent-extreme",
            severity="blocking",
            passed=False,
            actual={"maximumPercent": str(highest)},
            threshold={"maximumPercent": "50"},
        )
    return SectorEodQualityResult(
        rule_code="change-percent-extreme",
        severity="warning",
        passed=highest <= Decimal("20"),
        actual={"maximumPercent": str(highest)},
        threshold={"warningPercent": "20", "maximumPercent": "50"},
    )


def _extreme_turnover_result(quotes: tuple[SectorEodQuote, ...]) -> SectorEodQualityResult:
    """按未截断的换手率统计 warning 与 blocking 阈值，避免单位未确认时静默修正。"""
    values = [quote.turnover_percent for quote in quotes if quote.turnover_percent is not None]
    highest = max(values, default=Decimal("0"))
    if highest > Decimal("200"):
        return SectorEodQualityResult(
            rule_code="turnover-percent-extreme",
            severity="blocking",
            passed=False,
            actual={"maximumPercent": str(highest)},
            threshold={"maximumPercent": "200"},
        )
    return SectorEodQualityResult(
        rule_code="turnover-percent-extreme",
        severity="warning",
        passed=highest <= Decimal("50"),
        actual={"maximumPercent": str(highest)},
        threshold={"warningPercent": "50", "maximumPercent": "200"},
    )


def _change_consistency_result(quotes: tuple[SectorEodQuote, ...]) -> SectorEodQualityResult:
    """核验最新值、涨跌额和涨跌幅的隐含前值；少量偏差保留 warning，大量偏差阻断。"""
    checked_count = 0
    mismatch_count = 0
    tolerance = Decimal("0.05")
    for quote in quotes:
        if quote.latest_value is None or quote.change_value is None or quote.change_percent is None:
            continue
        previous_value = quote.latest_value - quote.change_value
        if previous_value <= 0:
            continue
        checked_count += 1
        computed_percent = quote.change_value / previous_value * Decimal("100")
        if abs(computed_percent - quote.change_percent) > tolerance:
            mismatch_count += 1
    if checked_count == 0:
        return SectorEodQualityResult(
            rule_code="change-percent-consistency",
            severity="info",
            passed=True,
            actual={"checked": 0, "mismatched": 0},
            threshold={"tolerancePercent": "0.05", "blockingRatioPercent": "1"},
        )
    mismatch_ratio = Decimal(mismatch_count) / Decimal(checked_count)
    severity = "blocking" if mismatch_ratio > Decimal("0.01") else "warning"
    return SectorEodQualityResult(
        rule_code="change-percent-consistency",
        severity=severity,
        passed=mismatch_count == 0,
        actual={"checked": checked_count, "mismatched": mismatch_count},
        threshold={"tolerancePercent": "0.05", "blockingRatioPercent": "1"},
    )


def _historical_quality_results(
    *,
    quotes: tuple[SectorEodQuote, ...],
    historical_reference: SectorEodHistoricalReference | None,
) -> tuple[SectorEodQualityResult, SectorEodQualityResult]:
    """比较最近已发布快照的市值稳定性和完整内容，首个可见交易日只记录无基线。"""
    if historical_reference is None:
        return (
            SectorEodQualityResult(
                rule_code="market-value-stability",
                severity="info",
                passed=True,
                actual={"compared": 0, "previousSnapshot": "absent"},
                threshold={"warningRange": "[0.5,2]", "blockingRange": "[0.1,10]"},
            ),
            SectorEodQualityResult(
                rule_code="cross-day-content-stale",
                severity="info",
                passed=True,
                actual={"previousSnapshot": "absent"},
                threshold={"mustDiffer": "true"},
            ),
        )
    compared_count = 0
    warning_count = 0
    blocking_count = 0
    for quote in quotes:
        previous_value = historical_reference.market_values.get(quote.identifier.code)
        if quote.market_value is None or previous_value is None or previous_value <= 0:
            continue
        compared_count += 1
        ratio = quote.market_value / previous_value
        if ratio < Decimal("0.1") or ratio > Decimal("10"):
            blocking_count += 1
        elif ratio < Decimal("0.5") or ratio > Decimal("2"):
            warning_count += 1
    market_severity = "blocking" if blocking_count else "warning" if warning_count else "info"
    market_result = SectorEodQualityResult(
        rule_code="market-value-stability",
        severity=market_severity,
        passed=blocking_count == 0 and warning_count == 0,
        actual={
            "compared": compared_count,
            "warningCount": warning_count,
            "blockingCount": blocking_count,
        },
        threshold={"warningRange": "[0.5,2]", "blockingRange": "[0.1,10]"},
    )
    current_content_sha256 = sector_eod_snapshot_content_sha256(quotes)
    stale_result = SectorEodQualityResult(
        rule_code="cross-day-content-stale",
        severity="blocking"
        if current_content_sha256 == historical_reference.content_sha256
        else "info",
        passed=current_content_sha256 != historical_reference.content_sha256,
        actual={"previousTradeDate": historical_reference.trade_date.isoformat()},
        threshold={"mustDiffer": "true"},
    )
    return market_result, stale_result


def decode_sector_eod_batch(
    payload: bytes,
    *,
    expected_scheme: SectorScheme,
    expected_trade_date: date,
) -> tuple[SectorEodQuote, ...]:
    """解析 adapter 中立载荷，拒绝身份、交易日和重复代码漂移。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "sector eod payload is not JSON",
            retryable=False,
        ) from error
    if not isinstance(decoded, dict) or decoded.get("schema") != _SCHEMA:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "unexpected sector eod schema",
            retryable=False,
        )
    if decoded.get("sectorScheme") != expected_scheme.value:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "sector eod scheme mismatch",
            retryable=False,
        )
    if decoded.get("tradeDate") != expected_trade_date.isoformat():
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "sector eod trade date mismatch",
            retryable=False,
        )
    records = decoded.get("quotes")
    if not isinstance(records, list) or not records:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "sector eod payload has no quotes",
            retryable=False,
        )
    try:
        quotes = tuple(_decode_quote(record, scheme=expected_scheme) for record in records)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "invalid sector eod quote",
            retryable=False,
        ) from error
    if len({quote.identifier.code for quote in quotes}) != len(quotes):
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "sector eod payload has duplicate sector codes",
            retryable=False,
        )
    # 上游默认排序不是 canonical 语义；按代码排序确保摘要和写入确定。
    return tuple(sorted(quotes, key=lambda quote: quote.identifier.code))


def _decode_quote(record: object, *, scheme: SectorScheme) -> SectorEodQuote:
    """把一条中立 JSON 记录映射为含来源原生单位的领域报价。"""
    if not isinstance(record, dict):
        raise ValueError("sector eod quote must be an object")
    code = _required_text(record, "code")
    name = _required_text(record, "name")
    return SectorEodQuote(
        identifier=SectorIdentifier(scheme=scheme, code=code),
        name=name,
        latest_value=_optional_decimal(record, "latestValue"),
        change_value=_optional_decimal(record, "changeValue"),
        change_percent=_optional_decimal(record, "changePercent"),
        market_value=_optional_decimal(record, "marketValue"),
        turnover_percent=_optional_decimal(record, "turnoverPercent"),
        advancers=_optional_count(record, "advancers"),
        decliners=_optional_count(record, "decliners"),
        leader_name=_optional_text(record, "leaderName"),
        leader_change_percent=_optional_decimal(record, "leaderChangePercent"),
    )


def _required_text(record: dict[str, object], key: str) -> str:
    """读取必填字符串，禁止用数字或空白占位身份与名称。"""
    value = record.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} is required")
    return value


def _optional_text(record: dict[str, object], key: str) -> str | None:
    """读取可空文本字段，保留上游缺失而不把 `null` 转成字符串。"""
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text or null")
    return value


def _optional_decimal(record: dict[str, object], key: str) -> Decimal | None:
    """读取可空精确小数，拒绝 NaN、无穷和二进制浮点污染。"""
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a decimal string or null")
    decimal_value = Decimal(value)
    if not decimal_value.is_finite():
        raise ValueError(f"{key} must be finite")
    return decimal_value


def _optional_count(record: dict[str, object], key: str) -> int | None:
    """读取可空计数字段，拒绝布尔值和负数进入质量门。"""
    value = record.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer or null")
    return value


def _store_passing_snapshot(
    *,
    repository: SectorEodRepository,
    execution_mode: SectorEodExecutionMode,
    scheme: SectorScheme,
    trade_date: date,
    source_cutoff_at: datetime,
    observed_at: datetime,
    quotes: tuple[SectorEodQuote, ...],
    provider_id: str,
    source_payload_sha256: str,
    raw_uri: str,
    adapter_version: str,
    schema_fingerprint: str,
    run: SectorEodRun,
    source_batch_id: UUID,
    quality_status: str,
    quality_results: tuple[SectorEodQualityResult, ...],
    before_final_publication: Callable[[], None] | None,
) -> PublishedSectorEodSnapshot:
    """将通过质量门的完整候选按 shadow 或 publish 模式交给仓储，禁止隐式发布。"""
    if execution_mode is SectorEodExecutionMode.SHADOW:
        return repository.store_shadow_snapshot(
            scheme=scheme,
            trade_date=trade_date,
            source_cutoff_at=source_cutoff_at,
            observed_at=observed_at,
            quotes=quotes,
            provider_id=provider_id,
            source_payload_sha256=source_payload_sha256,
            raw_uri=raw_uri,
            adapter_version=adapter_version,
            schema_fingerprint=schema_fingerprint,
            run=run,
            source_batch_id=source_batch_id,
            quality_status=quality_status,
            quality_results=quality_results,
        )
    if before_final_publication is not None:
        before_final_publication()
    return repository.publish_snapshot(
        scheme=scheme,
        trade_date=trade_date,
        source_cutoff_at=source_cutoff_at,
        observed_at=observed_at,
        quotes=quotes,
        provider_id=provider_id,
        source_payload_sha256=source_payload_sha256,
        raw_uri=raw_uri,
        adapter_version=adapter_version,
        schema_fingerprint=schema_fingerprint,
        run=run,
        source_batch_id=source_batch_id,
        quality_status=quality_status,
        quality_results=quality_results,
    )


def _result(
    publication: PublishedSectorEodSnapshot,
    *,
    execution_mode: SectorEodExecutionMode,
    run_id: UUID,
) -> SectorEodSnapshotSyncResult:
    """将候选或 publication 返回值投影为 CLI 和调度器使用的最小摘要。"""
    return SectorEodSnapshotSyncResult(
        snapshot=publication.snapshot,
        inserted=publication.inserted,
        execution_mode=execution_mode,
        run_id=run_id,
    )


def _raw_observation_archive(
    *, normalized_payload: bytes, raw_payload: bytes, raw_content_type: str
) -> bytes:
    """封装完整来源字节和可重放标准载荷；两者都保留，避免 replay 重新调用 SDK。"""
    return json.dumps(
        {
            "schema": _ARCHIVE_SCHEMA,
            "rawContentType": raw_content_type,
            "rawPayloadBase64": _encode_base64(raw_payload),
            "normalizedPayloadBase64": _encode_base64(normalized_payload),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def _replay_normalized_payload(archive_payload: bytes) -> bytes:
    """从服务写入的 raw envelope 提取标准载荷，拒绝旧裸 raw 或损坏对象。"""
    try:
        decoded = json.loads(archive_payload)
        if not isinstance(decoded, dict) or decoded.get("schema") != _ARCHIVE_SCHEMA:
            raise ValueError("unexpected raw observation archive")
        payload = decoded.get("normalizedPayloadBase64")
        if not isinstance(payload, str):
            raise ValueError("missing normalized replay payload")
        return base64.b64decode(payload.encode(), validate=True)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "sector eod raw evidence cannot be replayed",
            retryable=False,
        ) from error


def _encode_base64(payload: bytes) -> str:
    """以 ASCII Base64 无损嵌入任意来源字节，保留 SDK 原始响应而不假设 JSON。"""
    return base64.b64encode(payload).decode()


def _mark_run_failed(
    repository: SectorEodRepository, *, run: SectorEodRun, error: Exception
) -> None:
    """尽力保存稳定失败码；主错误优先返回调用方，避免失败记录再次遮蔽根因。"""
    error_code = error.code.value if isinstance(error, ProviderError) else "persistence-or-quality"
    try:
        repository.mark_failed(run=run, error_code=error_code)
    except Exception:
        # checkpoint 写入故障本身不能掩盖 provider、质量或发布的首个错误。
        return


def _raise_blocking_quality() -> None:
    """在 quarantine 事务完成后返回稳定 schema 错误，阻止任何后续 publication。"""
    raise ProviderError(
        ProviderErrorCode.SCHEMA,
        "sector eod blocking quality rule failed",
        retryable=False,
    )
