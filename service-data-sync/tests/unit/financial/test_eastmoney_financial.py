"""AKShare 东财财务 adapter 的字段隔离与标准载荷测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from service_data_sync.application.ports.data_source import (
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.domain.equity import Exchange
from service_data_sync.infrastructure.providers.akshare import eastmoney_financial
from service_data_sync.infrastructure.providers.akshare.eastmoney_financial import (
    AkshareEastmoneyFinancialAdapter,
)


class FakeFrame:
    """提供 adapter 所需的最小 pandas 宽表表面，不引入真实来源网络请求。"""

    def __init__(self, records: list[dict[str, object]]) -> None:
        """保存确定性供应商行，并以首行键顺序模拟宽表列名。"""
        self._records = records
        self.columns = tuple(records[0]) if records else ()

    @property
    def empty(self) -> bool:
        """报告测试宽表是否为空，保持与 pandas DataFrame 同名语义。"""
        return not self._records

    def to_dict(self, *, orient: str) -> list[dict[str, object]]:
        """仅支持 adapter 使用的 records 导出模式。"""
        assert orient == "records"
        return self._records


def test_statement_adapter_archives_provider_wide_tables_and_emits_neutral_facts(
    monkeypatch,
) -> None:
    """三表宽表字段只能存在 raw 或 adapter 内部，应用载荷仅出现中立报告和事实结构。"""
    frame = FakeFrame(
        [
            {
                "REPORT_DATE": "2026-03-31",
                "REPORT_TYPE": "一季报",
                "NOTICE_DATE": "2026-04-28",
                "TOTAL_ASSETS": "123.45",
            }
        ]
    )
    calls: list[str] = []

    def statement(symbol: str) -> FakeFrame:
        """记录三表接口的供应商代码，并复用同一确定性宽表。"""
        calls.append(symbol)
        return frame

    monkeypatch.setattr(eastmoney_financial.ak, "stock_balance_sheet_by_report_em", statement)
    monkeypatch.setattr(eastmoney_financial.ak, "stock_profit_sheet_by_report_em", statement)
    monkeypatch.setattr(eastmoney_financial.ak, "stock_cash_flow_sheet_by_report_em", statement)

    batch = asyncio.run(
        AkshareEastmoneyFinancialAdapter(request_timeout_seconds=5).fetch(
            SourceRequest(
                capability="financial.statement.raw",
                parameters=(("exchange", "SSE"), ("symbol", "600519")),
            )
        )
    )

    payload = json.loads(batch.payload)
    raw = json.loads(batch.raw_payload or b"{}")
    assert calls == ["SH600519", "SH600519", "SH600519"]
    assert payload["schema"] == "quant-v2.financial-statement.v1"
    assert len(payload["statements"]) == 3
    assert payload["statements"][0]["reports"][0]["facts"][0]["code"].startswith(
        "statement.balance_sheet.total-assets"
    )
    assert raw["statements"][0]["columns"] == [
        "REPORT_DATE",
        "REPORT_TYPE",
        "NOTICE_DATE",
        "TOTAL_ASSETS",
    ]
    assert batch.schema_fingerprint


def test_metric_adapter_uses_eastmoney_symbol_and_neutral_provider_metric_code(
    monkeypatch,
) -> None:
    """主要指标接口必须使用其独立代码格式，输出不泄漏供应商表头给应用层。"""
    captured: dict[str, str] = {}

    def indicators(*, symbol: str, indicator: str) -> FakeFrame:
        """捕获 SDK 参数并返回一行可规范化指标宽表。"""
        captured.update({"symbol": symbol, "indicator": indicator})
        return FakeFrame(
            [
                {
                    "REPORT_DATE": "2026-03-31",
                    "净资产收益率": "12.50",
                }
            ]
        )

    monkeypatch.setattr(eastmoney_financial.ak, "stock_financial_analysis_indicator_em", indicators)

    batch = asyncio.run(
        AkshareEastmoneyFinancialAdapter(request_timeout_seconds=5).fetch(
            SourceRequest(
                capability="financial.metric.raw",
                parameters=(("exchange", "SSE"), ("symbol", "600519")),
            )
        )
    )

    payload = json.loads(batch.payload)
    assert captured == {"symbol": "600519.SH", "indicator": "按报告期"}
    assert payload["metrics"][0]["metric"]["code"].startswith("provider_metric.field-")
    assert payload["metrics"][0]["metric"]["value"] == "12.50"


def test_valuation_adapter_projects_only_contractual_five_metrics(monkeypatch) -> None:
    """历史估值宽表仅投影合同承诺的市值、PE、PB 和市现率五项指标。"""
    captured: list[str] = []

    def valuations(*, symbol: str) -> FakeFrame:
        """记录请求证券代码并返回一条包含合同字段的估值宽表。"""
        captured.append(symbol)
        return FakeFrame(
            [
                {
                    "数据日期": "2026-07-27",
                    "总市值": "1000000000",
                    "PE(TTM)": "18.25",
                    "PE(静)": "20.5",
                    "市净率": "4.12",
                    "市现率": "16.3",
                    "PEG": "1.2",
                }
            ]
        )

    monkeypatch.setattr(eastmoney_financial.ak, "stock_value_em", valuations)

    batch = asyncio.run(
        AkshareEastmoneyFinancialAdapter(request_timeout_seconds=5).fetch(
            SourceRequest(
                capability="financial.valuation.raw",
                parameters=(("exchange", "SSE"), ("symbol", "600519")),
            )
        )
    )

    payload = json.loads(batch.payload)
    assert captured == ["600519"]
    assert [item["code"] for item in payload["valuations"]] == [
        "market_cap",
        "pe_ttm",
        "pe_static",
        "pb",
        "pcf",
    ]
    assert payload["valuations"][0]["currency"] == "CNY"
    assert payload["valuations"][1]["currencyNullReason"] == "NOT_APPLICABLE"


def test_adapter_rejects_unsupported_or_incomplete_identity_requests() -> None:
    """未声明能力、缺失交易所和非法六位代码都应在 AKShare egress 前失败。"""
    adapter = AkshareEastmoneyFinancialAdapter(request_timeout_seconds=5)
    requests = (
        SourceRequest(capability="financial.unknown"),
        SourceRequest(
            capability="financial.statement.raw",
            parameters=(("symbol", "600519"),),
        ),
        SourceRequest(
            capability="financial.statement.raw",
            parameters=(("exchange", "SSE"), ("symbol", "60051X")),
        ),
    )

    for request in requests:
        with pytest.raises(ProviderError) as captured:
            asyncio.run(adapter.fetch(request))
        assert captured.value.code is ProviderErrorCode.INVALID_REQUEST
        assert captured.value.retryable is False


def test_adapter_maps_timeout_to_retryable_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AKShare 墙钟超时必须保留为可重试 unavailable，不能误判为 schema 漂移。"""

    async def timeout(*arguments: object, **keywords: object) -> object:
        """模拟线程池中的 SDK 调用超过受控墙钟期限。"""
        del arguments, keywords
        raise TimeoutError

    monkeypatch.setattr(eastmoney_financial.asyncio, "to_thread", timeout)

    with pytest.raises(ProviderError) as captured:
        asyncio.run(
            AkshareEastmoneyFinancialAdapter(request_timeout_seconds=5).fetch(
                SourceRequest(
                    capability="financial.statement.raw",
                    parameters=(("exchange", "SSE"), ("symbol", "600519")),
                )
            )
        )

    assert captured.value.code is ProviderErrorCode.UNAVAILABLE
    assert captured.value.retryable is True


def test_adapter_maps_unknown_sdk_failure_but_preserves_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未知 SDK 异常可重试，已分类的 schema 错误则必须原样穿透。"""

    async def sdk_failure(*arguments: object, **keywords: object) -> object:
        """模拟 SDK 抛出尚未分类的运行时失败。"""
        del arguments, keywords
        raise RuntimeError("upstream reset")

    monkeypatch.setattr(eastmoney_financial.asyncio, "to_thread", sdk_failure)
    adapter = AkshareEastmoneyFinancialAdapter(request_timeout_seconds=5)
    request = SourceRequest(
        capability="financial.metric.raw",
        parameters=(("exchange", "SSE"), ("symbol", "600519")),
    )

    with pytest.raises(ProviderError) as captured:
        asyncio.run(adapter.fetch(request))
    assert captured.value.code is ProviderErrorCode.UNAVAILABLE
    assert captured.value.retryable is True

    expected = ProviderError(ProviderErrorCode.SCHEMA, "empty", retryable=False)

    async def classified_failure(*arguments: object, **keywords: object) -> object:
        """模拟同步抓取函数已将空响应识别为不可重试 schema 失败。"""
        del arguments, keywords
        raise expected

    monkeypatch.setattr(eastmoney_financial.asyncio, "to_thread", classified_failure)
    with pytest.raises(ProviderError) as preserved:
        asyncio.run(adapter.fetch(request))
    assert preserved.value is expected


def test_each_capability_rejects_empty_provider_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """三表、指标和估值任一空宽表都不是合法空发布，必须进入 schema 隔离。"""
    empty = FakeFrame([])

    def empty_statement(*, symbol: str) -> FakeFrame:
        """返回空三表宽表并保留函数签名。"""
        del symbol
        return empty

    def empty_metric(*, symbol: str, indicator: str) -> FakeFrame:
        """返回空主要指标宽表并保留函数签名。"""
        del symbol, indicator
        return empty

    def empty_valuation(*, symbol: str) -> FakeFrame:
        """返回空估值宽表并保留函数签名。"""
        del symbol
        return empty

    monkeypatch.setattr(
        eastmoney_financial.ak,
        "stock_balance_sheet_by_report_em",
        empty_statement,
    )
    with pytest.raises(ProviderError, match="empty BALANCE_SHEET"):
        eastmoney_financial._statement_payload(
            exchange=Exchange.SSE,
            symbol="600519",
            provider_symbol="SH600519",
        )

    monkeypatch.setattr(
        eastmoney_financial.ak,
        "stock_financial_analysis_indicator_em",
        empty_metric,
    )
    with pytest.raises(ProviderError, match="empty financial indicators"):
        eastmoney_financial._metric_payload(
            exchange=Exchange.SSE,
            symbol="600519",
            provider_symbol="SH600519",
        )

    monkeypatch.setattr(eastmoney_financial.ak, "stock_value_em", empty_valuation)
    with pytest.raises(ProviderError, match="empty valuation history"):
        eastmoney_financial._valuation_payload(exchange=Exchange.SSE, symbol="600519")


def test_statement_normalization_preserves_scope_currency_audit_and_time_semantics() -> None:
    """报表范围、币种、审计和带时区更新时间只按来源明确证据映射。"""
    records: list[dict[str, object | None]] = [
        {
            "REPORT_DATE": "2026-03-31T00:00:00",
            "REPORT_TYPE": "合并报表",
            "CURRENCY": "人民币",
            "NOTICE_DATE": "2026-04-28",
            "UPDATE_DATE": "2026-04-28T08:00:00+08:00",
            "OPINION_TYPE": "标准无保留",
            "EPS_BASIC": "1.25",
            "TEXT_NOTE": "不进入事实",
            "EMPTY_FIELD": None,
        },
        {
            "REPORTDATE": "2025-12-31",
            "REPORT_TYPE": "母公司报表",
            "CURRENCY": "--",
            "NOTICE_DATE": None,
            "UPDATE_DATE": "2026-03-01T08:00:00",
            "OPINION_TYPE": None,
            "ROE_RATE": "12.5",
        },
    ]

    reports = eastmoney_financial._normalize_statement_records("INCOME_STATEMENT", records)

    assert reports[0]["periodBasis"] == "YEAR_TO_DATE"
    assert reports[0]["statementScope"] == "CONSOLIDATED"
    assert reports[0]["currency"] == "CNY"
    assert reports[0]["currencyNullReason"] is None
    assert reports[0]["auditStatus"] == "AUDITED"
    assert reports[0]["providerUpdateAt"] == "2026-04-28T08:00:00+08:00"
    facts = reports[0]["facts"]
    assert isinstance(facts, list)
    assert [fact["label"] for fact in facts] == ["EPS_BASIC", "EMPTY_FIELD"]
    assert facts[0]["valueDomain"] == "per_share"
    assert facts[1]["nullReason"] == "UPSTREAM_NULL"
    assert reports[1]["statementScope"] == "PARENT"
    assert reports[1]["currencyNullReason"] == "UNKNOWN_SOURCE"
    assert reports[1]["providerUpdateAt"] is None


def test_provider_temporal_helpers_reject_missing_invalid_and_naive_values() -> None:
    """缺失或非法报告日期必须隔离，无时区更新时间必须保持未知。"""
    with pytest.raises(ProviderError, match="no date"):
        eastmoney_financial._required_date({"REPORT_DATE": None}, "REPORT_DATE")
    with pytest.raises(ProviderError, match="date is invalid"):
        eastmoney_financial._optional_date("2026-02-30")
    with pytest.raises(ProviderError, match="timestamp is invalid"):
        eastmoney_financial._optional_timestamp("not-a-time")

    assert eastmoney_financial._optional_timestamp("2026-03-01T08:00:00") is None
    assert eastmoney_financial._date_text(date(2026, 3, 31)) == "2026-03-31"
    assert eastmoney_financial._date_text(None) is None


def test_financial_field_and_scalar_normalization_is_deterministic() -> None:
    """字段代码、数值域、pandas 标量和 JSON 指纹必须跨重跑保持确定性。"""

    class Scalar:
        """模拟带 `item()` 的 NumPy 标量。"""

        def item(self) -> Decimal:
            """返回精确十进制值供 adapter JSON 规范化。"""
            return Decimal("12.50")

    assert eastmoney_financial._field_code("TOTAL_ASSETS", max_length=30) == "total-assets"
    assert eastmoney_financial._field_code("净利润", max_length=30).startswith("field-")
    with pytest.raises(ValueError, match="suffix limit"):
        eastmoney_financial._field_code("TOTAL", max_length=21)
    assert eastmoney_financial._value_domain("BASIC_EPS") == "per_share"
    assert eastmoney_financial._value_domain("NET_PROFIT_YOY") == "ratio"
    assert eastmoney_financial._value_domain("TOTAL_ASSETS") == "other"
    assert eastmoney_financial._currency("rmb") == "CNY"
    assert eastmoney_financial._currency("USD") is None
    assert eastmoney_financial._json_value(Scalar()) == "12.50"
    assert eastmoney_financial._json_value(float("nan")) is None
    assert eastmoney_financial._json_value(datetime(2026, 3, 31, tzinfo=UTC)) == (
        "2026-03-31T00:00:00+00:00"
    )
    first = eastmoney_financial._schema_fingerprint(
        {
            "capability": "financial.statement.raw",
            "statements": [
                {"statementType": "BALANCE_SHEET", "columns": ["B", "A"]},
                "ignored",
            ],
        }
    )
    second = eastmoney_financial._schema_fingerprint(
        {
            "capability": "financial.statement.raw",
            "statements": [
                {"statementType": "BALANCE_SHEET", "columns": ["B", "A"]},
                "ignored",
            ],
        }
    )
    assert first == second
