"""AKShare 港通市场统计 research 应用服务单元测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from uuid import uuid4

import pytest

from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    SourceRequest,
)
from service_data_sync.application.ports.stock_connect_market_stat_research import (
    StockConnectMarketStatResearchRecord,
    StockConnectMarketStatResearchSourceObservation,
    StoredStockConnectMarketStatResearchBatch,
)
from service_data_sync.application.stock_connect_research.market_stat_sync import (
    StockConnectMarketStatResearchSyncService,
    decode_stock_connect_market_stat_research_batch,
)
from service_data_sync.domain.stock_connect import StockConnectChannel

_CAPABILITY = "market.stock_connect.market_stat.reported"


class FakeSource:
    """提供固定标准批次或异常，验证应用层只通过 provider-neutral 端口请求。"""

    provider_id = "fixture-akshare"

    def __init__(self, batch: ProviderBatch | Exception) -> None:
        """保存本次 fetch 结果并初始化请求记录。"""
        self._batch = batch
        self.requests: list[SourceRequest] = []

    def capabilities(self) -> frozenset[str]:
        """声明唯一已验证的港通市场统计 capability。"""
        return frozenset({_CAPABILITY})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """记录中立请求，再返回固定批次或重抛来源异常。"""
        self.requests.append(request)
        if isinstance(self._batch, Exception):
            raise self._batch
        return self._batch


class RecordingRepository:
    """记录 research 应用服务交给持久化端口的对象，不依赖 PostgreSQL。"""

    def __init__(self) -> None:
        """初始化空调用列表。"""
        self.calls: list[dict[str, object]] = []

    def record_market_statistics(
        self, **kwargs: object
    ) -> StoredStockConnectMarketStatResearchBatch:
        """记录 research 写入参数并返回不含 publication 的固定结果。"""
        self.calls.append(kwargs)
        records = cast(tuple[StockConnectMarketStatResearchRecord, ...], kwargs["records"])
        return StoredStockConnectMarketStatResearchBatch(
            research_batch_id=uuid4(),
            source_batch_id=uuid4(),
            inserted_count=len(records),
            quality_status="passed",
        )


class RecordingEvidenceStore:
    """记录失败证据端口调用，验证成功路径不固化任何对象。"""

    def __init__(self) -> None:
        """初始化批次、失败摘要、持久化和清理计数器。"""
        self.batches: list[ProviderBatch] = []
        self.summaries: list[tuple[bytes, str, str | None]] = []
        self.persisted: list[Exception] = []
        self.discard_count = 0

    def stage_batch(self, batch: ProviderBatch) -> None:
        """记录已返回批次的暂存请求。"""
        self.batches.append(batch)

    def stage_failure_summary(
        self,
        payload: bytes,
        content_type: str,
        *,
        capability: str | None = None,
    ) -> None:
        """记录不含供应商原文的失败摘要。"""
        self.summaries.append((payload, content_type, capability))

    def persist_failure(self, error: Exception) -> str:
        """记录失败固化请求并返回私有清单 URI。"""
        self.persisted.append(error)
        return "s3://fixture/failures/manifest.json"

    def discard(self) -> None:
        """记录本次暂存区已释放。"""
        self.discard_count += 1


def test_decoder_accepts_real_probe_shape_without_trade_count_or_etf_fields() -> None:
    """真实 P0 标准记录可缺总成交额和逐字段状态，不能被强制填入官方字段。"""
    records = decode_stock_connect_market_stat_research_batch(
        _payload(
            [
                {
                    "tradeDate": "2026-07-31",
                    "buyAmount": "123.45",
                    "sellAmount": "100.00",
                    "turnoverAmount": None,
                    "netBuyAmount": "23.45",
                    "quotaBalance": "4976.55",
                    "currency": "CNY",
                    "availabilityStatus": "COMPLETE",
                }
            ]
        ),
        channel=StockConnectChannel("SH", "NORTHBOUND"),
    )

    assert records == (
        StockConnectMarketStatResearchRecord(
            trade_date=date(2026, 7, 31),
            buy_amount=Decimal("123.45"),
            sell_amount=Decimal("100.00"),
            turnover_amount=None,
            net_buy_amount=Decimal("23.45"),
            quota_balance=Decimal("4976.55"),
            currency="CNY",
            availability_status="COMPLETE",
            field_availability=None,
        ),
    )


def test_decoder_keeps_all_non_date_fields_optional_for_research() -> None:
    """来源未提供金额、币种或状态时研究链路保留空值并由质量层告警。"""
    records = decode_stock_connect_market_stat_research_batch(
        _payload([{"tradeDate": "2026-07-31"}]),
        channel=StockConnectChannel("SH", "NORTHBOUND"),
    )

    assert records[0].buy_amount is None
    assert records[0].currency is None
    assert records[0].availability_status is None
    assert records[0].field_availability is None


@pytest.mark.parametrize("field", ("tradeCount", "etfTurnoverAmount"))
def test_decoder_rejects_official_only_fields_not_emitted_by_real_akshare_probe(field: str) -> None:
    """research 标准合同禁止混入当前 AKShare 不提供的官方字段。"""
    record: dict[str, object] = {"tradeDate": "2026-07-31", field: "1"}

    with pytest.raises(ProviderError, match="record is invalid"):
        decode_stock_connect_market_stat_research_batch(
            _payload([record]),
            channel=StockConnectChannel("SH", "NORTHBOUND"),
        )


def test_sync_records_digest_only_source_and_writes_no_success_evidence() -> None:
    """成功路径只交出 unretained digest，不调用 failure manifest 固化。"""
    batch = _batch(_payload([{"tradeDate": "2026-07-31"}]))
    source = FakeSource(batch)
    repository = RecordingRepository()
    evidence = RecordingEvidenceStore()

    result = asyncio.run(
        StockConnectMarketStatResearchSyncService(
            source=source,
            repository=repository,
            failure_evidence_store=evidence,
        ).sync(
            channel=StockConnectChannel("SH", "NORTHBOUND"),
            start=date(2026, 7, 31),
            end=date(2026, 7, 31),
        )
    )

    stored_source = cast(
        StockConnectMarketStatResearchSourceObservation,
        repository.calls[0]["source"],
    )
    assert result.capability == _CAPABILITY
    assert source.requests == [
        SourceRequest(
            capability=_CAPABILITY,
            parameters=(
                ("channel", "SH"),
                ("direction", "NORTHBOUND"),
                ("start", "2026-07-31"),
                ("end", "2026-07-31"),
            ),
        )
    ]
    assert stored_source.raw_uri == f"unretained://sha256/{stored_source.raw_payload_sha256}"
    assert stored_source.normalized_uri == (
        f"unretained://sha256/{stored_source.normalized_payload_sha256}"
    )
    assert evidence.batches == [batch]
    assert evidence.persisted == []
    assert evidence.summaries == []
    assert evidence.discard_count == 1


def test_schema_failure_persists_a_sanitized_evidence_manifest_and_skips_repository() -> None:
    """解码失败必须留失败证据，且不能生成半完成 research 观察。"""
    batch = _batch(_payload([{"tradeDate": "2026-07-31", "tradeCount": "1"}]))
    repository = RecordingRepository()
    evidence = RecordingEvidenceStore()

    with pytest.raises(ProviderError, match="record is invalid"):
        asyncio.run(
            StockConnectMarketStatResearchSyncService(
                source=FakeSource(batch),
                repository=repository,
                failure_evidence_store=evidence,
            ).sync(
                channel=StockConnectChannel("SH", "NORTHBOUND"),
                start=date(2026, 7, 31),
                end=date(2026, 7, 31),
            )
        )

    assert repository.calls == []
    assert evidence.batches == [batch]
    assert evidence.persisted and isinstance(evidence.persisted[0], ProviderError)
    assert evidence.discard_count == 1
    summary = json.loads(evidence.summaries[-1][0])
    assert summary == {
        "capability": _CAPABILITY,
        "channel": "SH",
        "direction": "NORTHBOUND",
        "start": "2026-07-31",
        "end": "2026-07-31",
        "errorType": "ProviderError",
        "providerErrorCode": "schema",
    }


def _payload(records: list[dict[str, object]]) -> bytes:
    """构造当前 AKShare adapter 已冻结的标准市场统计 JSON。"""
    return json.dumps(
        {
            "schema": "quant-v2.stock-connect-market-daily.v1",
            "channel": "SH",
            "direction": "NORTHBOUND",
            "valueKind": "REPORTED",
            "records": records,
        },
        separators=(",", ":"),
    ).encode()


def _batch(payload: bytes) -> ProviderBatch:
    """构造带 raw 与标准载荷、真实上游身份和稳定 schema 指纹的来源批次。"""
    return ProviderBatch(
        provider_id="fixture-akshare",
        capability=_CAPABILITY,
        payload=payload,
        raw_payload=b'{"fixture":"raw"}',
        observed_at=datetime(2026, 8, 1, 8, tzinfo=UTC),
        content_type="application/vnd.quant-v2.stock-connect-market-daily.v1+json",
        raw_content_type="application/json",
        upstream_source="eastmoney.stock-connect",
        adapter_version="fixture-akshare-market-stat-v1",
        schema_fingerprint=hashlib.sha256(b"fixture-market-stat").hexdigest(),
    )
