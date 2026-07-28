"""板块 EOD 横截面应用编排、原始证据与 schema 质量门测试。"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    SourceRequest,
)
from service_data_sync.application.ports.market_data import RawPayload
from service_data_sync.application.ports.sector_eod import (
    ArchivedSectorEodObservation,
    PublishedSectorEodSnapshot,
    QueuedSectorEodRun,
    RankedSectorEodQuote,
    SectorEodExecutionMode,
    SectorEodHistoricalReference,
    SectorEodRun,
)
from service_data_sync.application.ports.trading_calendar import TradingCalendarUnavailableError
from service_data_sync.application.sector.eod_snapshot_sync import (
    SectorEodSnapshotSyncService,
    assess_sector_eod_quality,
    decode_sector_eod_batch,
)
from service_data_sync.domain.sector import (
    SectorEodFinality,
    SectorEodSnapshot,
    SectorIdentifier,
    SectorScheme,
    sector_eod_snapshot_content_sha256,
)


class FakeSource:
    """返回一个已标准化 EOD 批次，避免测试访问 SDK 或网络。"""

    provider_id = "test-eod"

    def capabilities(self) -> frozenset[str]:
        """仅声明本用例测试的完整横截面能力。"""
        return frozenset({"sector.quote.eod.snapshot.raw"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """断言应用只传 scheme/date 中立参数，并返回可发布候选。"""
        assert request.parameters == (
            ("sectorScheme", "eastmoney.industry"),
            ("tradeDate", "2026-07-27"),
        )
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=_payload(),
            raw_payload=b'{"raw":true}',
            raw_content_type="application/json",
            observed_at=datetime(2026, 7, 27, 8, 20, tzinfo=UTC),
            adapter_version="test-v1",
            schema_fingerprint="a" * 64,
        )


class UnsupportedSource(FakeSource):
    """不声明 EOD 能力的来源替身，用于验证应用层提前拒绝路径。"""

    def capabilities(self) -> frozenset[str]:
        """返回空集合，模拟未获准的 provider 注册表。"""
        return frozenset()


class EarlyObservationSource(FakeSource):
    """返回早于策略截点的来源观察，用于验证时间门与失败 checkpoint。"""

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """复用合法标准载荷但伪造早于截点的完成时刻。"""
        batch = await super().fetch(request)
        return ProviderBatch(
            provider_id=batch.provider_id,
            capability=batch.capability,
            payload=batch.payload,
            raw_payload=batch.raw_payload,
            raw_content_type=batch.raw_content_type,
            observed_at=datetime(2026, 7, 27, 8, 10, tzinfo=UTC),
            adapter_version=batch.adapter_version,
            schema_fingerprint=batch.schema_fingerprint,
        )


class BlockingQualitySource(FakeSource):
    """返回可解析但超过涨跌阈值的候选，用于验证 quarantine 不会遗漏质量证据。"""

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """只变更涨跌幅字段，保持来源、raw 和其余中立合同不变。"""
        batch = await super().fetch(request)
        payload = json.loads(batch.payload)
        payload["quotes"][0]["changePercent"] = "51"
        return replace(batch, payload=json.dumps(payload, separators=(",", ":")).encode())


class OpenTradingCalendar:
    """将测试目标日标记为已发布开市日，隔离外部权威日历依赖。"""

    def is_open(self, *, trade_date: date) -> bool | None:
        """只接受本文件固定交易日，避免测试不小心依赖工作日推断。"""
        assert trade_date == date(2026, 7, 27)
        return True


class UnknownTradingCalendar:
    """模拟权威日历尚未发布目标日，验证同步不会访问来源。"""

    def is_open(self, *, trade_date: date) -> bool | None:
        """返回未知，要求应用层在获取 EOD 前安全停止。"""
        del trade_date
        return None


class FakeStore:
    """记录原始对象写入，证明 canonical 发布前已形成不可变证据。"""

    def __init__(self) -> None:
        """初始化空的归档调用列表。"""
        self.payloads: list[RawPayload] = []
        self._objects: dict[str, bytes] = {}

    def put(self, payload: RawPayload) -> str:
        """保存传入证据并返回服务私有 S3 URI。"""
        self.payloads.append(payload)
        uri = f"s3://test/{payload.object_key}"
        self._objects[uri] = payload.payload
        return uri

    def get(self, uri: str) -> bytes:
        """按受控 URI 返回已经归档的对象字节。"""
        return self._objects[uri]


class FakeRepository:
    """记录应用层提交的标准报价，不连接 PostgreSQL。"""

    def __init__(self) -> None:
        """初始化用于断言发布参数的空记录。"""
        self.arguments: dict[str, object] | None = None
        self.quarantined_arguments: dict[str, object] | None = None
        self.run: SectorEodRun | None = None
        self.observation: ArchivedSectorEodObservation | None = None
        self.historical_reference: SectorEodHistoricalReference | None = None
        self.renewed_runs: list[SectorEodRun] = []

    def start_run(
        self, *, scheme: SectorScheme, trade_date: date, reuse_archived_raw: bool
    ) -> SectorEodRun:
        """返回固定运行租约；replay 必须先有本替身登记的 raw observation。"""
        if reuse_archived_raw and self.observation is None:
            raise ValueError("missing archived observation")
        self.run = SectorEodRun(uuid4(), uuid4(), scheme, trade_date)
        return self.run

    def record_archived_observation(self, **kwargs: object) -> ArchivedSectorEodObservation:
        """保存 raw URI 与来源元数据，模拟 source batch 已在 checkpoint 中登记。"""
        run = kwargs["run"]
        assert isinstance(run, SectorEodRun)
        observed_at = kwargs["observed_at"]
        assert isinstance(observed_at, datetime)
        self.observation = ArchivedSectorEodObservation(
            source_batch_id=uuid4(),
            raw_uri=str(kwargs["raw_uri"]),
            provider_id=str(kwargs["provider_id"]),
            observed_at=observed_at,
            adapter_version=str(kwargs["adapter_version"]),
            schema_fingerprint=str(kwargs["schema_fingerprint"]),
        )
        return self.observation

    def get_archived_observation(self, *, run: SectorEodRun) -> ArchivedSectorEodObservation:
        """返回前次归档观察，验证 replay 不访问来源。"""
        assert self.run == run
        assert self.observation is not None
        return self.observation

    def has_archived_observation(self, *, scheme: SectorScheme, trade_date: date) -> bool:
        """返回本替身是否登记过 raw，供 worker 任务恢复测试复用。"""
        assert scheme is SectorScheme.EASTMONEY_INDUSTRY
        assert trade_date == date(2026, 7, 27)
        return self.observation is not None

    def get_historical_reference(
        self, *, scheme: SectorScheme, before_trade_date: date
    ) -> SectorEodHistoricalReference | None:
        """返回测试预置的只读跨日质量参考，并断言调用方不改变目标分区。"""
        assert scheme is SectorScheme.EASTMONEY_INDUSTRY
        assert before_trade_date == date(2026, 7, 27)
        return self.historical_reference

    def mark_normalized(self, *, run: SectorEodRun) -> None:
        """断言标准化只发生在当前租约内。"""
        assert self.run == run

    def mark_fetched(self, *, run: SectorEodRun) -> None:
        """断言来源返回后的阶段仍由当前租约持有者推进。"""
        assert self.run == run

    def renew_lease(self, *, run: SectorEodRun) -> None:
        """记录租约续约调用，验证长步骤前不依赖初始五分钟租约。"""
        assert self.run == run
        self.renewed_runs.append(run)

    def requeue_expired_leases(self, *, now: datetime) -> int:
        """返回零，应用同步测试不运行独立 reaper 路径。"""
        del now
        return 0

    def list_queued_runs(self) -> tuple[QueuedSectorEodRun, ...]:
        """返回空队列，应用同步测试不经过 worker reaper 投递路径。"""
        return ()

    def mark_failed(self, *, run: SectorEodRun, error_code: str) -> None:
        """记录测试中的失败 checkpoint，避免同步错误被替身遗漏。"""
        assert self.run == run
        assert error_code

    def publish_snapshot(self, **kwargs: object) -> PublishedSectorEodSnapshot:
        """保存发布输入并返回一个已发布不可变版本。"""
        self.arguments = kwargs
        return PublishedSectorEodSnapshot(
            snapshot=SectorEodSnapshot(
                snapshot_id=uuid4(),
                data_version=uuid4(),
                scheme=SectorScheme.EASTMONEY_INDUSTRY,
                trade_date=date(2026, 7, 27),
                source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
                observed_at=datetime(2026, 7, 27, 8, 20, tzinfo=UTC),
                finality=SectorEodFinality.POST_CLOSE_OBSERVATION,
                quality_status="passed",
                published_at=datetime(2026, 7, 27, 8, 20, tzinfo=UTC),
            ),
            inserted=True,
        )

    def store_shadow_snapshot(self, **kwargs: object) -> PublishedSectorEodSnapshot:
        """保存 shadow candidate 输入，并返回没有 publication 时间的持久化版本。"""
        self.arguments = kwargs
        return PublishedSectorEodSnapshot(
            snapshot=SectorEodSnapshot(
                snapshot_id=uuid4(),
                data_version=uuid4(),
                scheme=SectorScheme.EASTMONEY_INDUSTRY,
                trade_date=date(2026, 7, 27),
                source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
                observed_at=datetime(2026, 7, 27, 8, 20, tzinfo=UTC),
                finality=SectorEodFinality.POST_CLOSE_OBSERVATION,
                quality_status="passed",
                published_at=None,
            ),
            inserted=True,
        )

    def store_quarantined_snapshot(self, **kwargs: object) -> None:
        """记录阻断候选参数，证明应用层先保存质量证据再报告任务失败。"""
        self.quarantined_arguments = kwargs

    def get_published_snapshot(
        self, *, scheme: SectorScheme, trade_date: date | None
    ) -> SectorEodSnapshot | None:
        """返回空值；同步编排测试不经过 EOD 读取路径。"""
        del scheme, trade_date
        return None

    def rollback_published_snapshot(
        self, *, scheme: SectorScheme, trade_date: date, revision: int
    ) -> SectorEodSnapshot:
        """禁止同步编排测试误走运维 rollback 路径，保持替身职责单一。"""
        del scheme, trade_date, revision
        raise AssertionError("sync application must not rollback publication")

    def list_ranked_quotes(self, **_kwargs: object) -> tuple[RankedSectorEodQuote, ...]:
        """返回空排行；同步编排测试不经过 EOD 查询路径。"""
        return ()

    def get_snapshot_quote(self, **_kwargs: object) -> RankedSectorEodQuote | None:
        """返回空单资源；同步编排测试不经过 EOD 查询路径。"""
        return None


def test_sync_archives_raw_before_publishing_normalized_eod_snapshot() -> None:
    """EOD 应用层必须先归档原始批次，再提交 provider-neutral 领域报价。"""
    repository = FakeRepository()
    raw_store = FakeStore()

    result = asyncio.run(
        SectorEodSnapshotSyncService(
            source=FakeSource(),
            repository=repository,
            raw_payload_store=raw_store,
            trading_calendar=OpenTradingCalendar(),
        ).sync(
            scheme=SectorScheme.EASTMONEY_INDUSTRY,
            trade_date=date(2026, 7, 27),
            source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
        )
    )

    assert result.inserted is True
    assert result.execution_mode is SectorEodExecutionMode.SHADOW
    assert result.snapshot.published_at is None
    archive = json.loads(raw_store.payloads[0].payload)
    assert base64.b64decode(archive["rawPayloadBase64"]) == b'{"raw":true}'
    assert repository.arguments is not None
    quotes = repository.arguments["quotes"]
    raw_uri = repository.arguments["raw_uri"]
    assert isinstance(quotes, tuple)
    assert quotes[0].identifier.code == "BK0475"
    assert isinstance(raw_uri, str)
    assert raw_uri.startswith("s3://test/raw/")
    assert len(repository.renewed_runs) >= 2


def test_sync_uses_a_distinct_raw_object_for_each_equal_content_observation() -> None:
    """相同来源内容的两次观察也必须保留两个 S3 证据对象，不能互相覆盖。"""
    repository = FakeRepository()
    raw_store = FakeStore()
    service = SectorEodSnapshotSyncService(
        source=FakeSource(),
        repository=repository,
        raw_payload_store=raw_store,
        trading_calendar=OpenTradingCalendar(),
    )

    for _ in range(2):
        asyncio.run(
            service.sync(
                scheme=SectorScheme.EASTMONEY_INDUSTRY,
                trade_date=date(2026, 7, 27),
                source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
            )
        )

    assert len(raw_store.payloads) == 2
    assert raw_store.payloads[0].object_key != raw_store.payloads[1].object_key


def test_replay_uses_archived_standard_payload_without_calling_provider() -> None:
    """DB 故障后的 replay 必须只读取 checkpoint raw，并重用原观察时刻和 source batch。"""
    repository = FakeRepository()
    raw_store = FakeStore()
    service = SectorEodSnapshotSyncService(
        source=FakeSource(),
        repository=repository,
        raw_payload_store=raw_store,
        trading_calendar=OpenTradingCalendar(),
    )

    asyncio.run(
        service.sync(
            scheme=SectorScheme.EASTMONEY_INDUSTRY,
            trade_date=date(2026, 7, 27),
            source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
        )
    )
    result = asyncio.run(
        service.replay(
            scheme=SectorScheme.EASTMONEY_INDUSTRY,
            trade_date=date(2026, 7, 27),
            source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
        )
    )

    assert result.snapshot.observed_at == datetime(2026, 7, 27, 8, 20, tzinfo=UTC)
    assert repository.arguments is not None
    assert repository.observation is not None
    assert repository.arguments["source_batch_id"] == repository.observation.source_batch_id


def test_quality_marks_optional_metric_availability_as_warned_without_dropping_rows() -> None:
    """可选可排序字段低于 95% 只产生 warned，来源行与 `null` 仍保留在快照中。"""
    quotes = decode_sector_eod_batch(
        _payload(),
        expected_scheme=SectorScheme.EASTMONEY_INDUSTRY,
        expected_trade_date=date(2026, 7, 27),
    )

    assessment = assess_sector_eod_quality((replace(quotes[0], market_value=None),))

    assert assessment.status == "warned"
    assert assessment.policy_version == "sector-eod-shadow-v1"
    assert any(
        result.rule_code == "availability-market-value" and not result.passed
        for result in assessment.results
    )


def test_quality_quarantines_extreme_change_without_clamping_source_value() -> None:
    """绝对涨跌幅超过 50 必须阻断发布，不能把异常值截断为看似正常的数字。"""
    quotes = decode_sector_eod_batch(
        _payload(),
        expected_scheme=SectorScheme.EASTMONEY_INDUSTRY,
        expected_trade_date=date(2026, 7, 27),
    )

    assessment = assess_sector_eod_quality((replace(quotes[0], change_percent=Decimal("51")),))

    assert assessment.has_blocking_failure


def test_quality_quarantines_extreme_turnover_and_rejects_empty_candidate() -> None:
    """换手率超过 200 与空候选都必须阻断，不能降级为 warned 或空快照。"""
    quotes = decode_sector_eod_batch(
        _payload(),
        expected_scheme=SectorScheme.EASTMONEY_INDUSTRY,
        expected_trade_date=date(2026, 7, 27),
    )

    assessment = assess_sector_eod_quality((replace(quotes[0], turnover_percent=Decimal("201")),))

    assert assessment.has_blocking_failure
    with pytest.raises(ValueError, match="at least one"):
        assess_sector_eod_quality(())


def test_quality_warns_when_one_percent_or_fewer_change_rows_are_inconsistent() -> None:
    """涨跌幅隐含前值偏差不超过 1% 行数时保留 warned 快照，不静默修正供应商数值。"""
    quote = decode_sector_eod_batch(
        _payload(),
        expected_scheme=SectorScheme.EASTMONEY_INDUSTRY,
        expected_trade_date=date(2026, 7, 27),
    )[0]
    quotes = tuple(
        replace(
            quote,
            identifier=SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, f"BK{index:04d}"),
            change_percent=Decimal("1.2") if index == 0 else quote.change_percent,
        )
        for index in range(100)
    )

    assessment = assess_sector_eod_quality(quotes)

    assert assessment.status == "warned"
    assert any(
        result.rule_code == "change-percent-consistency"
        and result.severity == "warning"
        and not result.passed
        for result in assessment.results
    )


def test_quality_quarantines_material_change_and_market_value_history_breaks() -> None:
    """超过 1% 的涨跌一致性偏差或超过十倍市值跳变均必须阻断 cross-day 发布。"""
    quote = decode_sector_eod_batch(
        _payload(),
        expected_scheme=SectorScheme.EASTMONEY_INDUSTRY,
        expected_trade_date=date(2026, 7, 27),
    )[0]
    historical_reference = SectorEodHistoricalReference(
        trade_date=date(2026, 7, 26),
        content_sha256=b"a" * 32,
        market_values={quote.identifier.code: Decimal("90000")},
    )

    change_assessment = assess_sector_eod_quality(
        (replace(quote, change_percent=Decimal("1.2")),),
        historical_reference=historical_reference,
    )
    market_assessment = assess_sector_eod_quality(
        (quote,), historical_reference=historical_reference
    )

    assert change_assessment.has_blocking_failure
    assert market_assessment.has_blocking_failure


def test_quality_quarantines_cross_day_identical_content() -> None:
    """与最近已发布交易日完整摘要完全相同时必须隔离，避免上游 stale 响应伪装为新日快照。"""
    quotes = decode_sector_eod_batch(
        _payload(),
        expected_scheme=SectorScheme.EASTMONEY_INDUSTRY,
        expected_trade_date=date(2026, 7, 27),
    )
    historical_reference = SectorEodHistoricalReference(
        trade_date=date(2026, 7, 26),
        content_sha256=sector_eod_snapshot_content_sha256(quotes),
        market_values={quotes[0].identifier.code: quotes[0].market_value},
    )

    assessment = assess_sector_eod_quality(quotes, historical_reference=historical_reference)

    assert assessment.has_blocking_failure


def test_sync_stores_blocking_quality_candidate_before_marking_run_failed() -> None:
    """阻断质量不能只写错误码；完整 candidate、报价与规则证据必须先进入 quarantine 事务。"""
    repository = FakeRepository()

    with pytest.raises(ProviderError, match="blocking quality"):
        asyncio.run(
            SectorEodSnapshotSyncService(
                source=BlockingQualitySource(),
                repository=repository,
                raw_payload_store=FakeStore(),
                trading_calendar=OpenTradingCalendar(),
            ).sync(
                scheme=SectorScheme.EASTMONEY_INDUSTRY,
                trade_date=date(2026, 7, 27),
                source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
            )
        )

    assert repository.arguments is None
    assert repository.quarantined_arguments is not None
    assert repository.quarantined_arguments["quality_results"]


def test_sync_fails_before_raw_archive_when_provider_observation_precedes_cutoff() -> None:
    """来源完成时刻早于策略截点时不得归档或发布，并应让当前 run 进入失败路径。"""
    raw_store = FakeStore()

    with pytest.raises(ProviderError, match="precedes"):
        asyncio.run(
            SectorEodSnapshotSyncService(
                source=EarlyObservationSource(),
                repository=FakeRepository(),
                raw_payload_store=raw_store,
                trading_calendar=OpenTradingCalendar(),
            ).sync(
                scheme=SectorScheme.EASTMONEY_INDUSTRY,
                trade_date=date(2026, 7, 27),
                source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
            )
        )

    assert raw_store.payloads == []


def test_replay_rejects_tampered_raw_envelope_without_calling_provider() -> None:
    """损坏 raw envelope 必须隔离为 schema 失败，不能回退到二次 provider 请求。"""
    repository = FakeRepository()
    raw_store = FakeStore()
    service = SectorEodSnapshotSyncService(
        source=FakeSource(),
        repository=repository,
        raw_payload_store=raw_store,
        trading_calendar=OpenTradingCalendar(),
    )
    asyncio.run(
        service.sync(
            scheme=SectorScheme.EASTMONEY_INDUSTRY,
            trade_date=date(2026, 7, 27),
            source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
        )
    )
    assert repository.observation is not None
    raw_store._objects[repository.observation.raw_uri] = b"{}"

    with pytest.raises(ProviderError, match="cannot be replayed"):
        asyncio.run(
            service.replay(
                scheme=SectorScheme.EASTMONEY_INDUSTRY,
                trade_date=date(2026, 7, 27),
                source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
            )
        )


def test_decoder_rejects_duplicate_codes_before_repository_quality_gate() -> None:
    """重复 scheme/code 必须在标准化边界隔离，不能依赖数据库唯一错误判断质量。"""
    decoded = json.loads(_payload())
    decoded["quotes"].append(decoded["quotes"][0])

    with pytest.raises(ProviderError, match="duplicate"):
        decode_sector_eod_batch(
            json.dumps(decoded).encode(),
            expected_scheme=SectorScheme.EASTMONEY_INDUSTRY,
            expected_trade_date=date(2026, 7, 27),
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"not-json", "not JSON"),
        (b'{"schema":"wrong"}', "unexpected"),
        (
            b'{"schema":"quant-v2.sector-eod-snapshot.v1",'
            b'"sectorScheme":"eastmoney.concept","tradeDate":"2026-07-27","quotes":[]}',
            "scheme mismatch",
        ),
        (
            b'{"schema":"quant-v2.sector-eod-snapshot.v1",'
            b'"sectorScheme":"eastmoney.industry","tradeDate":"2026-07-26","quotes":[]}',
            "trade date mismatch",
        ),
        (
            b'{"schema":"quant-v2.sector-eod-snapshot.v1",'
            b'"sectorScheme":"eastmoney.industry","tradeDate":"2026-07-27","quotes":[]}',
            "no quotes",
        ),
    ],
)
def test_decoder_rejects_schema_and_identity_drift(payload: bytes, message: str) -> None:
    """解析边界必须把损坏载荷、错误 scheme/date 与空表统一隔离为不可重试 schema 错误。"""
    with pytest.raises(ProviderError, match=message):
        decode_sector_eod_batch(
            payload,
            expected_scheme=SectorScheme.EASTMONEY_INDUSTRY,
            expected_trade_date=date(2026, 7, 27),
        )


def test_sync_rejects_unavailable_capability_without_archiving() -> None:
    """来源未声明 EOD capability 时应用层不得发起请求、归档或 canonical 写入。"""
    raw_store = FakeStore()

    with pytest.raises(ProviderError, match="unsupported"):
        asyncio.run(
            SectorEodSnapshotSyncService(
                source=UnsupportedSource(),
                repository=FakeRepository(),
                raw_payload_store=raw_store,
                trading_calendar=OpenTradingCalendar(),
            ).sync(
                scheme=SectorScheme.EASTMONEY_INDUSTRY,
                trade_date=date(2026, 7, 27),
                source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
            )
        )

    assert raw_store.payloads == []


def test_sync_rejects_unknown_trading_day_without_calling_provider() -> None:
    """权威日历缺少目标日时，EOD 不能按工作日猜测或写入 raw。"""
    raw_store = FakeStore()

    with pytest.raises(TradingCalendarUnavailableError, match="calendar"):
        asyncio.run(
            SectorEodSnapshotSyncService(
                source=FakeSource(),
                repository=FakeRepository(),
                raw_payload_store=raw_store,
                trading_calendar=UnknownTradingCalendar(),
            ).sync(
                scheme=SectorScheme.EASTMONEY_INDUSTRY,
                trade_date=date(2026, 7, 27),
                source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
            )
        )

    assert raw_store.payloads == []


def _payload() -> bytes:
    """生成一个完整最小中立 EOD 载荷，所有数值保持合同要求的十进制字符串。"""
    return json.dumps(
        {
            "schema": "quant-v2.sector-eod-snapshot.v1",
            "sectorScheme": "eastmoney.industry",
            "tradeDate": "2026-07-27",
            "quotes": [
                {
                    "code": "BK0475",
                    "name": "证券",
                    "latestValue": "1000",
                    "changeValue": "10",
                    "changePercent": "1",
                    "marketValue": "1000000",
                    "turnoverPercent": "3",
                    "advancers": 10,
                    "decliners": 3,
                    "leaderName": "示例证券",
                    "leaderChangePercent": "5",
                }
            ],
        },
        separators=(",", ":"),
    ).encode()
