"""财务 raw 归档、标准解码与分能力发布编排测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from service_data_sync.application.financial import sync as financial_sync
from service_data_sync.application.financial.sync import FinancialSyncService
from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.financial_sync import (
    FinancialPublicationResult,
    FinancialSourceObservation,
)
from service_data_sync.application.ports.market_data import RawPayload
from service_data_sync.domain.equity import Exchange


class FakeFinancialSource:
    """按能力返回标准财务载荷，不包含任何 SDK、HTTP 或供应商字段。"""

    provider_id = "fake-financial"

    def capabilities(self) -> frozenset[str]:
        """声明同步服务需要的三项原始财务能力。"""
        return frozenset(
            {
                "financial.statement.raw",
                "financial.metric.raw",
                "financial.valuation.raw",
            }
        )

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """按请求能力回放确定性 payload 与不同 raw evidence 字节。"""
        payload = _payloads()[request.capability]
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=json.dumps(payload, separators=(",", ":")).encode(),
            raw_payload=f'{{"raw":"{request.capability}"}}'.encode(),
            raw_content_type="application/json",
            observed_at=datetime(2026, 7, 28, 8, tzinfo=UTC),
            upstream_source="test.financial",
            adapter_version="test-v1",
            schema_fingerprint="a" * 64,
        )


class IncompleteFinancialSource(FakeFinancialSource):
    """只声明三表能力，用于证明同步服务拒绝部分能力来源。"""

    def capabilities(self) -> frozenset[str]:
        """故意省略指标和估值能力。"""
        return frozenset({"financial.statement.raw"})


class FakeRawPayloadStore:
    """记录发布前归档的原始证据，并返回确定性的对象存储 URI。"""

    def __init__(self) -> None:
        """初始化空证据列表。"""
        self.payloads: list[RawPayload] = []

    def put(self, payload: RawPayload) -> str:
        """收集不可变 raw 载荷，确认标准解码不会取代来源证据。"""
        self.payloads.append(payload)
        return f"s3://raw/{len(self.payloads)}"

    def get(self, uri: str) -> bytes:
        """当前同步用例不需要 replay；读取表示调用路径越界。"""
        raise AssertionError(f"unexpected raw replay: {uri}")


class FakeFinancialRepository:
    """捕获三个 canonical 发布调用，并返回互不相同的能力版本。"""

    def __init__(self) -> None:
        """初始化按能力索引的发布调用记录。"""
        self.calls: dict[str, dict[str, object]] = {}

    def publish_reports(self, **kwargs: object) -> FinancialPublicationResult:
        """记录三表输入及其已经归档的来源观察。"""
        self.calls["reports"] = kwargs
        return _result("financial.report", "1")

    def publish_provider_metrics(self, **kwargs: object) -> FinancialPublicationResult:
        """记录供应商指标输入及其已经归档的来源观察。"""
        self.calls["metrics"] = kwargs
        return _result("financial.provider-metric", "2")

    def publish_valuations(self, **kwargs: object) -> FinancialPublicationResult:
        """记录估值输入及其已经归档的来源观察。"""
        self.calls["valuations"] = kwargs
        return _result("financial.valuation", "3")


def test_sync_archives_each_raw_batch_before_publishing_three_capabilities() -> None:
    """每项能力先存 raw evidence，再解码并发布为彼此独立的消费者 dataVersion。"""
    source = FakeFinancialSource()
    raw_store = FakeRawPayloadStore()
    repository = FakeFinancialRepository()

    result = asyncio.run(
        FinancialSyncService(
            source=source,
            repository=repository,
            raw_payload_store=raw_store,
        ).sync_security(exchange=Exchange.SSE, symbol="600519")
    )

    assert [payload.payload for payload in raw_store.payloads] == [
        b'{"raw":"financial.statement.raw"}',
        b'{"raw":"financial.metric.raw"}',
        b'{"raw":"financial.valuation.raw"}',
    ]
    assert result.reports.capability == "financial.report"
    assert result.provider_metrics.capability == "financial.provider-metric"
    assert result.valuations.capability == "financial.valuation"
    reports = repository.calls["reports"]["reports"]
    metrics = repository.calls["metrics"]["metrics"]
    valuations = repository.calls["valuations"]["valuations"]
    assert isinstance(reports, tuple) and len(reports) == 3
    assert isinstance(metrics, tuple) and metrics[0].code == "provider_metric.net_income"
    assert isinstance(valuations, tuple) and valuations[0].code == "pe_ttm"
    report_source = cast(FinancialSourceObservation, repository.calls["reports"]["source"])
    metric_source = cast(FinancialSourceObservation, repository.calls["metrics"]["source"])
    valuation_source = cast(FinancialSourceObservation, repository.calls["valuations"]["source"])
    assert report_source.raw_uri == "s3://raw/1"
    assert metric_source.raw_uri == "s3://raw/2"
    assert valuation_source.raw_uri == "s3://raw/3"


def test_sync_rejects_invalid_identity_before_any_provider_egress() -> None:
    """非法证券代码必须在能力探测和来源访问前失败，避免错误身份留下 raw evidence。"""
    source = FakeFinancialSource()
    raw_store = FakeRawPayloadStore()

    with pytest.raises(ValueError, match="six-digit"):
        asyncio.run(
            FinancialSyncService(
                source=source,
                repository=FakeFinancialRepository(),
                raw_payload_store=raw_store,
            ).sync_security(exchange=Exchange.SSE, symbol="60051X")
        )

    assert raw_store.payloads == []


def test_sync_rejects_incomplete_provider_capability_set() -> None:
    """三项能力必须来自完整声明的单一来源，缺少任一能力都不能产生部分发布。"""
    source = IncompleteFinancialSource()
    raw_store = FakeRawPayloadStore()

    with pytest.raises(ProviderError) as captured:
        asyncio.run(
            FinancialSyncService(
                source=source,
                repository=FakeFinancialRepository(),
                raw_payload_store=raw_store,
            ).sync_security(exchange=Exchange.SSE, symbol="600519")
        )

    assert captured.value.code is ProviderErrorCode.INVALID_REQUEST
    assert captured.value.retryable is False
    assert raw_store.payloads == []


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"{", "not JSON"),
        (
            json.dumps(
                {
                    "schema": "quant-v2.financial-statement.v1",
                    "exchange": "SZSE",
                    "symbol": "600519",
                }
            ).encode(),
            "identity or schema mismatch",
        ),
    ],
)
def test_report_decoder_rejects_invalid_envelope(payload: bytes, message: str) -> None:
    """损坏 JSON 或身份串线必须以不可重试 schema 错误隔离。"""
    with pytest.raises(ProviderError, match=message) as captured:
        financial_sync._decode_reports(payload, exchange=Exchange.SSE, symbol="600519")

    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert captured.value.retryable is False


def test_report_decoder_rejects_duplicate_logical_report_identity() -> None:
    """同一报表逻辑键重复会让双时态 revision 含义不确定，必须整批拒绝。"""
    payload = _payloads()["financial.statement.raw"]
    statements = cast(list[dict[str, object]], payload["statements"])
    reports = cast(list[object], statements[0]["reports"])
    reports.append(reports[0])

    with pytest.raises(ProviderError, match="duplicate report identities"):
        financial_sync._decode_reports(
            json.dumps(payload).encode(),
            exchange=Exchange.SSE,
            symbol="600519",
        )


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("statements", [], "three statements"),
        ("statements", [None, None, None], "statement entry is invalid"),
    ],
)
def test_report_decoder_requires_three_well_formed_statements(
    field: str,
    replacement: object,
    message: str,
) -> None:
    """三表集合必须完整且每项为对象，防止部分来源被误判为完整发布。"""
    payload = _payloads()["financial.statement.raw"]
    payload[field] = replacement

    with pytest.raises(ProviderError, match=message):
        financial_sync._decode_reports(
            json.dumps(payload).encode(),
            exchange=Exchange.SSE,
            symbol="600519",
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"value": None, "nullReason": None}, "value and null reason"),
        ({"value": "1", "nullReason": "UPSTREAM_NULL"}, "value and null reason"),
        ({"currency": None, "currencyNullReason": None}, "currency and currency null reason"),
        ({"currency": "CN", "currencyNullReason": None}, "ISO code"),
        ({"value": "Infinity"}, "must be finite"),
        ({"valueDomain": "money"}, "valueDomain is invalid"),
    ],
)
def test_report_decoder_enforces_fact_null_currency_and_numeric_contract(
    updates: dict[str, object],
    message: str,
) -> None:
    """事实空值、币种和精确数值组合必须满足 canonical 约束后才能发布。"""
    payload = _payloads()["financial.statement.raw"]
    statements = cast(list[dict[str, object]], payload["statements"])
    reports = cast(list[dict[str, object]], statements[0]["reports"])
    facts = cast(list[dict[str, object]], reports[0]["facts"])
    facts[0].update(updates)

    with pytest.raises(ProviderError, match=message):
        financial_sync._decode_reports(
            json.dumps(payload).encode(),
            exchange=Exchange.SSE,
            symbol="600519",
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("reportPeriod", "2026/03/31", "not an ISO date"),
        ("announcementDate", "2026/04/31", "optional date is invalid"),
        ("providerUpdateAt", "2026-04-28 08:00:00", "must include timezone"),
        ("providerUpdateAt", "not-a-time", "optional timestamp is invalid"),
    ],
)
def test_report_decoder_rejects_ambiguous_or_invalid_temporal_fields(
    field: str,
    value: object,
    message: str,
) -> None:
    """报告期与来源更新时间不得依赖模糊日期或无时区时间。"""
    payload = _payloads()["financial.statement.raw"]
    statements = cast(list[dict[str, object]], payload["statements"])
    reports = cast(list[dict[str, object]], statements[0]["reports"])
    reports[0][field] = value

    with pytest.raises(ProviderError, match=message):
        financial_sync._decode_reports(
            json.dumps(payload).encode(),
            exchange=Exchange.SSE,
            symbol="600519",
        )


def test_metric_decoder_rejects_null_provider_metric_and_invalid_entry() -> None:
    """供应商派生指标不能以空事实发布，非对象行也不能越过 adapter 契约。"""
    payload = _payloads()["financial.metric.raw"]
    entries = cast(list[dict[str, object]], payload["metrics"])
    metric = cast(dict[str, object], entries[0]["metric"])
    metric.update({"value": None, "nullReason": "UPSTREAM_NULL"})

    with pytest.raises(ProviderError, match="must not be null"):
        financial_sync._decode_metrics(
            json.dumps(payload).encode(),
            exchange=Exchange.SSE,
            symbol="600519",
        )

    payload["metrics"] = [None]
    with pytest.raises(ProviderError, match="metric entry is invalid"):
        financial_sync._decode_metrics(
            json.dumps(payload).encode(),
            exchange=Exchange.SSE,
            symbol="600519",
        )


def test_valuation_decoder_rejects_duplicate_observation_and_unknown_code() -> None:
    """估值逻辑键必须唯一且指标代码处于公开合同封闭集合。"""
    payload = _payloads()["financial.valuation.raw"]
    entries = cast(list[dict[str, object]], payload["valuations"])
    entries.append(dict(entries[0]))

    with pytest.raises(ProviderError, match="duplicate observations"):
        financial_sync._decode_valuations(
            json.dumps(payload).encode(),
            exchange=Exchange.SSE,
            symbol="600519",
        )

    entries[:] = [{**entries[0], "code": "ps"}]
    with pytest.raises(ProviderError, match="code is invalid"):
        financial_sync._decode_valuations(
            json.dumps(payload).encode(),
            exchange=Exchange.SSE,
            symbol="600519",
        )


def _payloads() -> dict[str, dict[str, object]]:
    """构造三项可验证中立 payload，覆盖报表、指标和估值的最小成功路径。"""
    fact = {
        "code": "statement.fact",
        "label": "测试字段",
        "value": "1.25",
        "nullReason": None,
        "valueDomain": "other",
        "originalUnit": "source_unknown",
        "canonicalUnit": "source_unknown",
        "scaleFactor": "1",
        "signConvention": "provider_as_reported",
        "currency": None,
        "currencyNullReason": "UNKNOWN_SOURCE",
    }
    return {
        "financial.statement.raw": {
            "schema": "quant-v2.financial-statement.v1",
            "exchange": "SSE",
            "symbol": "600519",
            "statements": [
                {
                    "statementType": "BALANCE_SHEET",
                    "reports": [
                        {
                            "reportPeriod": "2026-03-31",
                            "periodBasis": "POINT_IN_TIME",
                            "statementScope": "UNKNOWN",
                            "currency": None,
                            "currencyNullReason": "UNKNOWN_SOURCE",
                            "reportType": "UNKNOWN",
                            "announcementDate": None,
                            "providerUpdateAt": None,
                            "auditStatus": "UNKNOWN",
                            "facts": [fact],
                        }
                    ],
                },
                {
                    "statementType": "INCOME_STATEMENT",
                    "reports": [
                        {
                            "reportPeriod": "2026-03-31",
                            "periodBasis": "YEAR_TO_DATE",
                            "statementScope": "UNKNOWN",
                            "currency": None,
                            "currencyNullReason": "UNKNOWN_SOURCE",
                            "reportType": "UNKNOWN",
                            "announcementDate": None,
                            "providerUpdateAt": None,
                            "auditStatus": "UNKNOWN",
                            "facts": [fact],
                        }
                    ],
                },
                {
                    "statementType": "CASH_FLOW_STATEMENT",
                    "reports": [
                        {
                            "reportPeriod": "2026-03-31",
                            "periodBasis": "YEAR_TO_DATE",
                            "statementScope": "UNKNOWN",
                            "currency": None,
                            "currencyNullReason": "UNKNOWN_SOURCE",
                            "reportType": "UNKNOWN",
                            "announcementDate": None,
                            "providerUpdateAt": None,
                            "auditStatus": "UNKNOWN",
                            "facts": [fact],
                        }
                    ],
                },
            ],
        },
        "financial.metric.raw": {
            "schema": "quant-v2.financial-provider-metric.v1",
            "exchange": "SSE",
            "symbol": "600519",
            "metrics": [
                {
                    "reportPeriod": "2026-03-31",
                    "periodBasis": "YEAR_TO_DATE",
                    "statementScope": "UNKNOWN",
                    "currency": None,
                    "currencyNullReason": "UNKNOWN_SOURCE",
                    "metric": {
                        **fact,
                        "code": "provider_metric.net_income",
                        "label": "净利润",
                    },
                }
            ],
        },
        "financial.valuation.raw": {
            "schema": "quant-v2.financial-valuation.v1",
            "exchange": "SSE",
            "symbol": "600519",
            "valuations": [
                {
                    "observationDate": "2026-07-27",
                    "code": "pe_ttm",
                    "label": "市盈率",
                    "value": "18.25",
                    "valueDomain": "ratio",
                    "unit": "ratio",
                    "currency": None,
                    "currencyNullReason": "NOT_APPLICABLE",
                }
            ],
        },
    }


def _result(capability: str, suffix: str) -> FinancialPublicationResult:
    """构造独立能力发布结果，验证用例不依赖真实数据库 UUID 生成。"""
    return FinancialPublicationResult(
        capability=capability,  # type: ignore[arg-type]  # 调用处只传入端口已定义的三项能力。
        data_version=UUID(f"10000000-0000-4000-8000-00000000000{suffix}"),
        inserted_count=1,
        unchanged_count=0,
    )
