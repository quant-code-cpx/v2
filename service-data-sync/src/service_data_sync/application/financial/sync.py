"""财务来源批次的 raw 归档、标准解码与分能力发布编排。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.financial_sync import (
    FinancialFactInput,
    FinancialMetricInput,
    FinancialPublicationResult,
    FinancialReportInput,
    FinancialSourceObservation,
    FinancialSyncRepository,
    FinancialValuationInput,
)
from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.domain.equity import Exchange

_STATEMENT_CAPABILITY = "financial.statement.raw"
_METRIC_CAPABILITY = "financial.metric.raw"
_VALUATION_CAPABILITY = "financial.valuation.raw"
_SCHEMAS = {
    _STATEMENT_CAPABILITY: "quant-v2.financial-statement.v1",
    _METRIC_CAPABILITY: "quant-v2.financial-provider-metric.v1",
    _VALUATION_CAPABILITY: "quant-v2.financial-valuation.v1",
}


@dataclass(frozen=True, slots=True)
class FinancialSyncResult:
    """描述一次证券财务同步三个独立消费者能力的发布结果。"""

    reports: FinancialPublicationResult
    provider_metrics: FinancialPublicationResult
    valuations: FinancialPublicationResult


class FinancialSyncService:
    """协调一个证券的三表、供应商指标和估值同步，三个能力不混用同一发布版本。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: FinancialSyncRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收唯一 provider-neutral 来源、财务持久化端口和不可变 raw evidence 存储。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync_security(self, *, exchange: Exchange, symbol: str) -> FinancialSyncResult:
        """下载并发布一只证券的三类财务数据；任一能力失败时不伪造部分成功。"""
        _validate_symbol(symbol)
        required = (_STATEMENT_CAPABILITY, _METRIC_CAPABILITY, _VALUATION_CAPABILITY)
        if not set(required).issubset(self._source.capabilities()):
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "provider does not support all financial capabilities",
                retryable=False,
            )
        reports_batch = await self._fetch(
            exchange=exchange, symbol=symbol, capability=_STATEMENT_CAPABILITY
        )
        reports_source = self._archive(reports_batch)
        reports = _decode_reports(reports_batch.payload, exchange=exchange, symbol=symbol)
        metrics_batch = await self._fetch(
            exchange=exchange, symbol=symbol, capability=_METRIC_CAPABILITY
        )
        metrics_source = self._archive(metrics_batch)
        metrics = _decode_metrics(metrics_batch.payload, exchange=exchange, symbol=symbol)
        valuations_batch = await self._fetch(
            exchange=exchange, symbol=symbol, capability=_VALUATION_CAPABILITY
        )
        valuations_source = self._archive(valuations_batch)
        valuations = _decode_valuations(valuations_batch.payload, exchange=exchange, symbol=symbol)
        return FinancialSyncResult(
            reports=self._repository.publish_reports(
                exchange=exchange,
                symbol=symbol,
                reports=reports,
                source=reports_source,
            ),
            provider_metrics=self._repository.publish_provider_metrics(
                exchange=exchange,
                symbol=symbol,
                metrics=metrics,
                source=metrics_source,
            ),
            valuations=self._repository.publish_valuations(
                exchange=exchange,
                symbol=symbol,
                valuations=valuations,
                source=valuations_source,
            ),
        )

    async def _fetch(self, *, exchange: Exchange, symbol: str, capability: str) -> ProviderBatch:
        """通过来源端口请求一个明确能力，应用层不知晓任何 SDK、URL 或供应商字段。"""
        return await self._source.fetch(
            SourceRequest(
                capability=capability,
                parameters=(("exchange", exchange.value), ("symbol", symbol)),
            )
        )

    def _archive(self, batch: ProviderBatch) -> FinancialSourceObservation:
        """先持久化原始证据，再将其摘要和血缘交给 canonical 写入事务。"""
        raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
        raw_digest = hashlib.sha256(raw_payload).hexdigest()
        raw_uri = self._raw_payload_store.put(
            RawPayload(
                object_key=(
                    f"raw/{batch.capability}/{batch.provider_id}/{batch.observed_at:%Y/%m/%d}/"
                    f"{raw_digest}.json"
                ),
                content_sha256=raw_digest,
                content_type=batch.raw_content_type or batch.content_type,
                payload=raw_payload,
            )
        )
        return FinancialSourceObservation(
            provider_id=batch.provider_id,
            capability=batch.capability,
            source_payload_sha256=raw_digest,
            raw_uri=raw_uri,
            observed_at=batch.observed_at,
            upstream_source=batch.upstream_source or batch.provider_id,
            adapter_version=batch.adapter_version,
            schema_fingerprint=batch.schema_fingerprint
            or hashlib.sha256(batch.capability.encode()).hexdigest(),
        )


def _decode_reports(
    payload: bytes, *, exchange: Exchange, symbol: str
) -> tuple[FinancialReportInput, ...]:
    """验证 adapter 标准报表 JSON，并构造不含供应商列名的三表输入。"""
    decoded = _payload(payload, capability=_STATEMENT_CAPABILITY, exchange=exchange, symbol=symbol)
    statements = decoded.get("statements")
    if not isinstance(statements, list) or len(statements) != 3:
        raise _schema_error("statement payload must contain three statements")
    reports: list[FinancialReportInput] = []
    for statement in statements:
        if not isinstance(statement, dict):
            raise _schema_error("statement entry is invalid")
        statement_type = _required_choice(
            statement, "statementType", {"BALANCE_SHEET", "INCOME_STATEMENT", "CASH_FLOW_STATEMENT"}
        )
        entries = statement.get("reports")
        if not isinstance(entries, list) or not entries:
            raise _schema_error("statement has no reports")
        reports.extend(_decode_report(entry, statement_type=statement_type) for entry in entries)
    logical_keys = {
        (
            report.statement_type,
            report.report_period,
            report.period_basis,
            report.statement_scope,
            report.currency,
            report.report_type,
        )
        for report in reports
    }
    if len(logical_keys) != len(reports):
        raise _schema_error("statement payload has duplicate report identities")
    return tuple(sorted(reports, key=lambda report: (report.report_period, report.statement_type)))


def _decode_report(entry: object, *, statement_type: str) -> FinancialReportInput:
    """解码一份中立报表头和事实集合，并验证空值、币种和单位的受控组合。"""
    if not isinstance(entry, dict):
        raise _schema_error("report is invalid")
    currency = _optional_text(entry.get("currency"))
    currency_null_reason = _optional_text(entry.get("currencyNullReason"))
    _validate_currency(currency, currency_null_reason)
    facts = entry.get("facts")
    if not isinstance(facts, list) or not facts:
        raise _schema_error("report has no facts")
    return FinancialReportInput(
        statement_type=statement_type,  # type: ignore[arg-type]  # 前置封闭集合已验证该字面量。
        report_period=_date(entry, "reportPeriod"),
        period_basis=_required_choice(
            entry, "periodBasis", {"POINT_IN_TIME", "YEAR_TO_DATE", "SINGLE_QUARTER", "TTM"}
        ),  # type: ignore[arg-type]  # 前置封闭集合已验证该字面量。
        statement_scope=_required_choice(
            entry, "statementScope", {"CONSOLIDATED", "PARENT", "UNKNOWN"}
        ),  # type: ignore[arg-type]  # 前置封闭集合已验证该字面量。
        currency=currency,
        currency_null_reason=currency_null_reason,
        report_type=_required_text(entry, "reportType"),
        announcement_date=_optional_date(entry.get("announcementDate")),
        provider_update_at=_optional_timestamp(entry.get("providerUpdateAt")),
        audit_status=_required_choice(entry, "auditStatus", {"AUDITED", "UNAUDITED", "UNKNOWN"}),
        facts=tuple(_decode_fact(fact) for fact in facts),
    )


def _decode_metrics(
    payload: bytes, *, exchange: Exchange, symbol: str
) -> tuple[FinancialMetricInput, ...]:
    """验证 adapter 标准指标 JSON；供应商计算指标永不回写为三表行项目。"""
    decoded = _payload(payload, capability=_METRIC_CAPABILITY, exchange=exchange, symbol=symbol)
    entries = decoded.get("metrics")
    if not isinstance(entries, list) or not entries:
        raise _schema_error("metric payload has no metrics")
    metrics: list[FinancialMetricInput] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("metric"), dict):
            raise _schema_error("metric entry is invalid")
        currency = _optional_text(entry.get("currency"))
        currency_null_reason = _optional_text(entry.get("currencyNullReason"))
        _validate_currency(currency, currency_null_reason)
        fact = _decode_fact(entry["metric"])
        if fact.value is None:
            raise _schema_error("provider metric value must not be null")
        metrics.append(
            FinancialMetricInput(
                code=fact.code,
                label=fact.label,
                report_period=_date(entry, "reportPeriod"),
                period_basis=_required_choice(
                    entry,
                    "periodBasis",
                    {"POINT_IN_TIME", "YEAR_TO_DATE", "SINGLE_QUARTER", "TTM"},
                ),  # type: ignore[arg-type]  # 前置封闭集合已验证该字面量。
                statement_scope=_required_choice(
                    entry, "statementScope", {"CONSOLIDATED", "PARENT", "UNKNOWN"}
                ),  # type: ignore[arg-type]  # 前置封闭集合已验证该字面量。
                value=fact.value,
                value_domain=fact.value_domain,
                unit=fact.canonical_unit,
                currency=currency,
                currency_null_reason=currency_null_reason,
            )
        )
    return tuple(sorted(metrics, key=lambda metric: (metric.report_period, metric.code)))


def _decode_valuations(
    payload: bytes, *, exchange: Exchange, symbol: str
) -> tuple[FinancialValuationInput, ...]:
    """验证标准历史估值 JSON，并只接受当前合同中的五个稳定指标代码。"""
    decoded = _payload(payload, capability=_VALUATION_CAPABILITY, exchange=exchange, symbol=symbol)
    entries = decoded.get("valuations")
    if not isinstance(entries, list) or not entries:
        raise _schema_error("valuation payload has no valuations")
    valuations: list[FinancialValuationInput] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise _schema_error("valuation entry is invalid")
        currency = _optional_text(entry.get("currency"))
        currency_null_reason = _optional_text(entry.get("currencyNullReason"))
        _validate_currency(currency, currency_null_reason)
        code = _required_choice(entry, "code", {"market_cap", "pe_ttm", "pe_static", "pb", "pcf"})
        valuations.append(
            FinancialValuationInput(
                code=code,
                label=_required_text(entry, "label"),
                observation_date=_date(entry, "observationDate"),
                value=_decimal(entry, "value"),
                value_domain=_required_choice(
                    entry, "valueDomain", {"monetary", "ratio", "count", "per_share", "other"}
                ),
                unit=_required_text(entry, "unit"),
                currency=currency,
                currency_null_reason=currency_null_reason,
            )
        )
    logical_keys = {(valuation.observation_date, valuation.code) for valuation in valuations}
    if len(logical_keys) != len(valuations):
        raise _schema_error("valuation payload has duplicate observations")
    return tuple(
        sorted(valuations, key=lambda valuation: (valuation.observation_date, valuation.code))
    )


def _decode_fact(value: object) -> FinancialFactInput:
    """解码一个 adapter 已治理的中立事实，禁止上游空值在应用层变成零值。"""
    if not isinstance(value, dict):
        raise _schema_error("financial fact is invalid")
    decimal_text = value.get("value")
    amount = None if decimal_text is None else _decimal(value, "value")
    null_reason = _optional_text(value.get("nullReason"))
    if (amount is None) == (null_reason is None):
        raise _schema_error("financial fact value and null reason are inconsistent")
    currency = _optional_text(value.get("currency"))
    currency_null_reason = _optional_text(value.get("currencyNullReason"))
    _validate_currency(currency, currency_null_reason)
    return FinancialFactInput(
        code=_required_text(value, "code"),
        label=_required_text(value, "label"),
        value=amount,
        null_reason=null_reason,
        value_domain=_required_choice(
            value, "valueDomain", {"monetary", "ratio", "count", "per_share", "other"}
        ),
        original_unit=_required_text(value, "originalUnit"),
        canonical_unit=_required_text(value, "canonicalUnit"),
        scale_factor=_decimal(value, "scaleFactor"),
        sign_convention=_required_text(value, "signConvention"),
        currency=currency,
        currency_null_reason=currency_null_reason,
    )


def _payload(
    payload: bytes, *, capability: str, exchange: Exchange, symbol: str
) -> dict[str, object]:
    """读取并验证标准 JSON 外壳，阻止 adapter identity 或 capability 串线。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _schema_error("financial payload is not JSON") from error
    if (
        not isinstance(decoded, dict)
        or decoded.get("schema") != _SCHEMAS[capability]
        or decoded.get("exchange") != exchange.value
        or decoded.get("symbol") != symbol
    ):
        raise _schema_error("financial payload identity or schema mismatch")
    return decoded


def _validate_symbol(symbol: str) -> None:
    """拒绝不能安全定位 A 股证券的调用方输入。"""
    if len(symbol) != 6 or not symbol.isdigit():
        raise ValueError("symbol must be a six-digit A-share code")


def _required_choice(value: dict[str, object], key: str, choices: set[str]) -> str:
    """读取枚举字段并拒绝未知值，避免 schema 漂移静默进入 canonical。"""
    result = _required_text(value, key)
    if result not in choices:
        raise _schema_error(f"{key} is invalid")
    return result


def _required_text(value: dict[str, object], key: str) -> str:
    """读取非空标准字符串字段，字段缺失被视为 adapter schema 违约。"""
    result = _optional_text(value.get(key))
    if result is None:
        raise _schema_error(f"{key} is required")
    return result


def _optional_text(value: object) -> str | None:
    """将 JSON 标量投影为去空白文本，禁止对象和数组伪装成业务字段。"""
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def _date(value: dict[str, object], key: str) -> date:
    """解析严格 ISO 日期字段；未知或模糊日期不能进入双时态逻辑键。"""
    try:
        return date.fromisoformat(_required_text(value, key))
    except ValueError as error:
        raise _schema_error(f"{key} is not an ISO date") from error


def _optional_date(value: object) -> date | None:
    """解析可空 ISO 日期，空值保留为未知而非回填报告期。"""
    text = _optional_text(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise _schema_error("optional date is invalid") from error


def _optional_timestamp(value: object) -> datetime | None:
    """解析可空且带时区的 ISO 时间，禁止将来源日期臆定为观察时刻。"""
    text = _optional_text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise _schema_error("optional timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise _schema_error("optional timestamp must include timezone")
    return parsed


def _decimal(value: dict[str, object], key: str) -> Decimal:
    """从十进制文本解析精确值，拒绝二进制浮点、NaN 与无限值。"""
    try:
        parsed = Decimal(_required_text(value, key))
    except (InvalidOperation, ValueError) as error:
        raise _schema_error(f"{key} is not a decimal") from error
    if not parsed.is_finite():
        raise _schema_error(f"{key} must be finite")
    return parsed


def _validate_currency(currency: str | None, null_reason: str | None) -> None:
    """保持币种与空值原因成对出现，保证数据库约束在应用层已成立。"""
    if (currency is None) == (null_reason is None):
        raise _schema_error("currency and currency null reason are inconsistent")
    if currency is not None and len(currency) != 3:
        raise _schema_error("currency must be an ISO code")


def _schema_error(detail: str) -> ProviderError:
    """统一构造不可重试的标准载荷错误，提示任务将来源观测隔离。"""
    return ProviderError(ProviderErrorCode.SCHEMA, detail, retryable=False)
