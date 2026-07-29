"""公司公告与业绩 P0 标准载荷的边界测试。"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from service_data_sync.application.corporate_events.sync import decode_corporate_events_batch
from service_data_sync.application.ports.data_source import ProviderError


def test_decoder_keeps_date_only_document_and_guidance_range_without_backfill() -> None:
    """日期级公告须使用保守可用时刻，预告单边/区间均不能被当前聚合值补齐。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.corporate-earnings-events.v1",
            "documents": [
                {
                    "sourceDocumentId": "1224164626",
                    "securityCode": "SZSE.000656",
                    "title": "业绩预告",
                    "category": "EARNINGS_GUIDANCE",
                    "officialUrl": "https://www.cninfo.com.cn/notice/1224164626",
                    "announcedOn": "2025-07-15",
                    "sourceVisibleAt": None,
                    "visibleTimePrecision": "DATE_ONLY",
                    "publicUsableAt": "2025-07-16T09:30:00+08:00",
                    "contentSha256": "a" * 64,
                }
            ],
            "guidanceMetrics": [
                {
                    "sourceDocumentId": "1224164626",
                    "securityCode": "SZSE.000656",
                    "reportPeriod": "2025-06-30",
                    "guidanceType": "INCREASE",
                    "metricCode": "NET_PROFIT",
                    "amountLow": "120000000",
                    "amountHigh": "160000000",
                    "yoyLow": None,
                    "yoyHigh": None,
                    "priorPeriodValue": None,
                    "currency": "CNY",
                }
            ],
            "expressMetrics": [],
        }
    ).encode()

    documents, guidance_metrics, express_metrics = decode_corporate_events_batch(payload)

    assert documents[0].source_visible_at is None
    assert documents[0].visible_time_precision == "DATE_ONLY"
    assert guidance_metrics[0].amount_low == Decimal("120000000")
    assert guidance_metrics[0].amount_high == Decimal("160000000")
    assert express_metrics == ()


def test_decoder_rejects_unreferenced_metric_and_future_field() -> None:
    """结构化指标必须绑定同批官方文档，供应商事后收益列不得被静默丢弃。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.corporate-earnings-events.v1",
            "documents": [
                {
                    "sourceDocumentId": "A-1",
                    "securityCode": "SSE.600000",
                    "title": "快报",
                    "category": "EARNINGS_EXPRESS",
                    "officialUrl": "https://example.test/A-1",
                    "announcedOn": "2026-03-01",
                    "sourceVisibleAt": "2026-03-01T18:00:00+08:00",
                    "visibleTimePrecision": "EXACT",
                    "publicUsableAt": "2026-03-01T18:00:00+08:00",
                    "contentSha256": "b" * 64,
                    "futureReturn": "0.4",
                }
            ],
            "guidanceMetrics": [],
            "expressMetrics": [],
        }
    ).encode()

    with pytest.raises(ProviderError, match="value is invalid"):
        decode_corporate_events_batch(payload)


def test_decoder_accepts_a_legal_empty_disclosure_window() -> None:
    """窗口内没有业绩公告时返回三个空集合，调用方不创建空发布。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.corporate-earnings-events.v1",
            "documents": [],
            "guidanceMetrics": [],
            "expressMetrics": [],
        }
    ).encode()

    assert decode_corporate_events_batch(payload) == ((), (), ())
