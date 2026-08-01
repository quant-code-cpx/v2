"""板块成分同步用例的冻结分区、原始证据、失败隔离和 release 回归测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import cast
from uuid import uuid4

import pytest

from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.market_data import RawPayload
from service_data_sync.application.ports.sector_market_data import StoredSector
from service_data_sync.application.ports.sector_membership import (
    PublishedSectorMembershipRelease,
    PublishedSectorMembershipSnapshot,
    SectorMembershipRepository,
    SectorMembershipRun,
)
from service_data_sync.application.sector.membership_sync import (
    SectorMembershipSyncService,
    decode_sector_membership_batch,
)
from service_data_sync.domain.sector import (
    SectorIdentifier,
    SectorMembershipCandidate,
    SectorScheme,
)


class FakeRawPayloadStore:
    """记录对象存储调用，验证 raw 必须先于 canonical 发布。"""

    def __init__(self) -> None:
        """初始化空 raw 归档集合。"""
        self.payloads: list[RawPayload] = []

    def put(self, payload: RawPayload) -> str:
        """保存输入并返回确定性测试 URI。"""
        self.payloads.append(payload)
        return f"s3://test/{payload.object_key}"

    def get(self, uri: str) -> bytes:
        """成员快照未在本测试替身覆盖 raw replay；调用应立即失败。"""
        raise AssertionError(f"unexpected raw replay read: {uri}")


class FakeRepository:
    """提供固定 ACTIVE 集合并记录用例产生的 canonical 发布。"""

    def __init__(self, raw_store: FakeRawPayloadStore) -> None:
        """构造两个已激活行业板块和空发布记录。"""
        self.raw_store = raw_store
        self.sectors = (_sector("BK0001", "银行", 1), _sector("BK0002", "证券", 2))
        self.list_calls = 0
        self.snapshots: list[StoredSector] = []
        self.release_calls: list[tuple[SectorScheme, date]] = []
        self.completed: list[StoredSector] = []
        self.failed: list[StoredSector] = []
        self.candidate_batches: list[tuple[str, ...]] = []
        self.started_sectors: tuple[StoredSector, ...] | None = None
        self.finished_status: str | None = None

    def start_run(
        self,
        *,
        scheme: SectorScheme,
        observation_date: date,
        sectors: tuple[StoredSector, ...],
    ) -> SectorMembershipRun:
        """返回确定性 run 身份，验证应用先冻结分区后再访问来源。"""
        assert sectors and all(sector in self.sectors for sector in sectors)
        self.started_sectors = sectors
        return SectorMembershipRun(uuid4(), scheme, observation_date)

    def mark_partition_completed(
        self,
        *,
        run: SectorMembershipRun,
        sector: StoredSector,
        publication: PublishedSectorMembershipSnapshot,
    ) -> None:
        """记录完成分区和快照，以验证 checkpoint 发生在 release 前。"""
        del run
        assert publication.complete is True
        self.completed.append(sector)

    def mark_partition_failed(
        self,
        *,
        run: SectorMembershipRun,
        sector: StoredSector,
        error_code: str,
    ) -> None:
        """记录来源失败分区，不让失败冒充完整 checkpoint。"""
        del run
        assert error_code == ProviderErrorCode.UNAVAILABLE.value
        self.failed.append(sector)

    def finish_run(self, *, run: SectorMembershipRun, status: str) -> None:
        """记录 scheme run 终态，供 partial 行为断言。"""
        del run
        self.finished_status = status

    def list_active_sectors(self, *, scheme: SectorScheme) -> tuple[StoredSector, ...]:
        """返回本次 run 的稳定分区集合，重复读取将被测试发现。"""
        assert scheme is SectorScheme.EASTMONEY_INDUSTRY
        self.list_calls += 1
        return self.sectors

    def publish_snapshot(self, **kwargs: object) -> PublishedSectorMembershipSnapshot:
        """确认 raw 已归档后记录冻结板块，并模拟一个完整可发布快照。"""
        assert self.raw_store.payloads
        sector = kwargs["sector"]
        assert isinstance(sector, StoredSector)
        candidates = cast(tuple[SectorMembershipCandidate, ...], kwargs["candidates"])
        self.snapshots.append(sector)
        self.candidate_batches.append(tuple(candidate.source_symbol for candidate in candidates))
        return PublishedSectorMembershipSnapshot(
            snapshot_id=uuid4(),
            observed_at=datetime(2026, 7, 27, tzinfo=UTC),
            complete=True,
            inserted_interval_count=1,
            closed_interval_count=0,
            pending_count=0,
            quarantine_count=0,
        )

    def publish_release(
        self,
        *,
        scheme: SectorScheme,
        observation_date: date,
        before_final_publication: Callable[[], None] | None = None,
    ) -> PublishedSectorMembershipRelease:
        """记录 reducer 调用并返回固定 release，证明单板块失败不会跳过汇总。"""
        self.release_calls.append((scheme, observation_date))
        if before_final_publication is not None:
            before_final_publication()
        return PublishedSectorMembershipRelease(
            release_id=uuid4(),
            data_version=uuid4(),
            quality_status="passed",
            fresh_sector_count=2,
            carried_forward_sector_count=0,
            published_at=datetime(2026, 7, 27, tzinfo=UTC),
        )


class FakeSource:
    """按请求板块返回版本化中立当前成员集合。"""

    provider_id = "fake-sector-membership"

    def capabilities(self) -> frozenset[str]:
        """仅声明成分快照能力，阻止用例调用错误来源。"""
        return frozenset({"sector.membership.snapshot.raw"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """构造与请求严格绑定的中立 JSON 和独立 raw 证据。"""
        parameters = dict(request.parameters)
        code = parameters["sector"]
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=(
                "{"
                '"schema":"quant-v2.sector-membership-snapshot.v1",'
                '"sectorScheme":"eastmoney.industry",'
                f'"sector":"{code}",'
                '"members":[{"sourceSymbol":"600000","sourceName":"浦发银行"}]}'
            ).encode(),
            raw_payload=b'{"provider":true}',
            raw_content_type="application/json",
            observed_at=datetime(2026, 7, 27, tzinfo=UTC),
        )


class PartiallyFailingSource(FakeSource):
    """让一个板块来源失败，验证它不能妨碍其他分区完成和 release 尝试。"""

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """仅为第二个分区返回可重试来源故障。"""
        if dict(request.parameters)["sector"] == "BK0002":
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE, "source is unavailable", retryable=False
            )
        return await super().fetch(request)


class OneRetrySource(FakeSource):
    """第一次请求可重试断连、第二次成功，验证外层只追加一次来源重排。"""

    def __init__(self) -> None:
        """初始化零调用计数。"""
        self.calls = 0

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """首个调用抛出 UNAVAILABLE，随后委托确定性成功来源。"""
        self.calls += 1
        if self.calls == 1:
            raise ProviderError(ProviderErrorCode.UNAVAILABLE, "temporary outage", retryable=True)
        return await super().fetch(request)


class BShareMixedSource(FakeSource):
    """返回混有 B 股的原始成员响应，验证 A 股边界只过滤已知 B 股代码。"""

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """构造包含深沪 B 股和其它六位代码的同一份标准、原始载荷。"""
        code = dict(request.parameters)["sector"]
        payload = (
            "{"
            '"schema":"quant-v2.sector-membership-snapshot.v1",'
            '"sectorScheme":"eastmoney.industry",'
            f'"sector":"{code}",'
            '"members":['
            '{"sourceSymbol":"600000","sourceName":"浦发银行"},'
            '{"sourceSymbol":"200001","sourceName":"深市B股"},'
            '{"sourceSymbol":"201001","sourceName":"深市B股二类"},'
            '{"sourceSymbol":"900001","sourceName":"沪市B股"},'
            '{"sourceSymbol":"123456","sourceName":"未知六位代码"}'
            "]}"
        ).encode()
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=payload,
            raw_payload=payload,
            raw_content_type="application/json",
            observed_at=datetime(2026, 7, 27, tzinfo=UTC),
        )


def test_sync_freezes_active_sectors_archives_raw_and_publishes_release() -> None:
    """同步应只读取一次 ACTIVE 集合，并按该集合完成 raw→快照→release 顺序。"""
    raw_store = FakeRawPayloadStore()
    repository = FakeRepository(raw_store)

    result = asyncio.run(
        SectorMembershipSyncService(
            source=FakeSource(),
            repository=cast(SectorMembershipRepository, repository),
            raw_payload_store=raw_store,
        ).sync_scheme(
            scheme=SectorScheme.EASTMONEY_INDUSTRY,
            observation_date=date(2026, 7, 27),
        )
    )

    assert repository.list_calls == 1
    assert [sector.identifier.code for sector in repository.snapshots] == ["BK0001", "BK0002"]
    assert len(raw_store.payloads) == 2
    assert result.failures == ()
    assert result.release is not None
    assert [sector.identifier.code for sector in repository.completed] == ["BK0001", "BK0002"]
    assert repository.finished_status == "succeeded"


def test_sync_selects_one_active_sector_and_arms_terminal_only_for_release() -> None:
    """SECTOR 重试只访问目标分区，且 reducer 真正返回 release 时才传播终态回调。"""
    raw_store = FakeRawPayloadStore()
    repository = FakeRepository(raw_store)
    timeline: list[str] = []

    def arm_terminal() -> None:
        """记录 release 仓储在最终事务中武装控制面终态。"""
        timeline.append("arm")

    result = asyncio.run(
        SectorMembershipSyncService(
            source=FakeSource(),
            repository=cast(SectorMembershipRepository, repository),
            raw_payload_store=raw_store,
        ).sync_scheme(
            scheme=SectorScheme.EASTMONEY_INDUSTRY,
            observation_date=date(2026, 7, 27),
            sector_codes=("BK0002",),
            before_final_publication=arm_terminal,
        )
    )

    assert [item.identifier.code for item in result.items] == ["BK0002"]
    assert [sector.identifier.code for sector in repository.snapshots] == ["BK0002"]
    assert repository.started_sectors == (repository.sectors[1],)
    assert timeline == ["arm"]


def test_sync_isolates_provider_failure_and_still_runs_release_reducer() -> None:
    """来源失败只能形成 partial 结果，不能关闭别的板块关系或跳过旧 release 沿用。"""
    raw_store = FakeRawPayloadStore()
    repository = FakeRepository(raw_store)

    result = asyncio.run(
        SectorMembershipSyncService(
            source=PartiallyFailingSource(),
            repository=cast(SectorMembershipRepository, repository),
            raw_payload_store=raw_store,
        ).sync_scheme(
            scheme=SectorScheme.EASTMONEY_INDUSTRY,
            observation_date=date(2026, 7, 27),
        )
    )

    assert [item.identifier.code for item in result.items] == ["BK0001"]
    assert [(failure.identifier.code, failure.code) for failure in result.failures] == [
        ("BK0002", ProviderErrorCode.UNAVAILABLE)
    ]
    assert repository.release_calls == [(SectorScheme.EASTMONEY_INDUSTRY, date(2026, 7, 27))]
    assert [sector.identifier.code for sector in repository.failed] == ["BK0002"]
    assert repository.finished_status == "partial"


def test_sync_retries_one_unavailable_request_before_marking_partition_failed() -> None:
    """可重试 UNAVAILABLE 只能额外请求一次；成功后必须保存完整 checkpoint 而非失败记录。"""
    raw_store = FakeRawPayloadStore()
    repository = FakeRepository(raw_store)
    source = OneRetrySource()

    result = asyncio.run(
        SectorMembershipSyncService(
            source=source,
            repository=cast(SectorMembershipRepository, repository),
            raw_payload_store=raw_store,
            retry_delay_seconds=0,
        ).sync_scheme(
            scheme=SectorScheme.EASTMONEY_INDUSTRY,
            observation_date=date(2026, 7, 27),
        )
    )

    assert source.calls == 3
    assert result.failures == ()
    assert repository.failed == []


def test_decode_rejects_duplicate_source_symbols() -> None:
    """重复来源代码必须在 canonical 写入前被拒绝，避免一份不完整分页关闭区间。"""
    payload = (
        b'{"schema":"quant-v2.sector-membership-snapshot.v1",'
        b'"sectorScheme":"eastmoney.industry","sector":"BK0001",'
        b'"members":[{"sourceSymbol":"600000","sourceName":"\xe6\xb5\xa6\xe5\x8f\x91"},'
        b'{"sourceSymbol":"600000","sourceName":"\xe6\xb5\xa6\xe5\x8f\x91"}]}'
    )

    with pytest.raises(ProviderError, match="duplicate"):
        decode_sector_membership_batch(
            payload,
            identifier=SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, "BK0001"),
        )


def test_decode_excludes_only_explicit_b_share_prefixes() -> None:
    """`200`、`201`、`900` 仅在 A 股候选边界排除，其它未知六位代码必须保留。"""
    payload = (
        b'{"schema":"quant-v2.sector-membership-snapshot.v1",'
        b'"sectorScheme":"eastmoney.industry","sector":"BK0001",'
        b'"members":[{"sourceSymbol":"600000","sourceName":"A"},'
        b'{"sourceSymbol":"200001","sourceName":"B1"},'
        b'{"sourceSymbol":"201001","sourceName":"B2"},'
        b'{"sourceSymbol":"900001","sourceName":"B3"},'
        b'{"sourceSymbol":"123456","sourceName":"unknown"}]}'
    )

    candidates, _ = decode_sector_membership_batch(
        payload,
        identifier=SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, "BK0001"),
    )

    assert [candidate.source_symbol for candidate in candidates] == ["123456", "600000"]


def test_sync_archives_original_b_share_rows_before_a_share_filter() -> None:
    """B 股只退出 A 股 canonical；原始响应仍完整归档，且 release 阈值未被改写。"""
    raw_store = FakeRawPayloadStore()
    repository = FakeRepository(raw_store)

    result = asyncio.run(
        SectorMembershipSyncService(
            source=BShareMixedSource(),
            repository=cast(SectorMembershipRepository, repository),
            raw_payload_store=raw_store,
        ).sync_scheme(
            scheme=SectorScheme.EASTMONEY_INDUSTRY,
            observation_date=date(2026, 7, 27),
            sector_codes=("BK0001",),
        )
    )

    assert repository.candidate_batches == [("123456", "600000")]
    assert len(raw_store.payloads) == 1
    assert b'"sourceSymbol":"200001"' in raw_store.payloads[0].payload
    assert b'"sourceSymbol":"201001"' in raw_store.payloads[0].payload
    assert b'"sourceSymbol":"900001"' in raw_store.payloads[0].payload
    assert result.release is not None


def _sector(code: str, name: str, key: int) -> StoredSector:
    """构造已发布目录中可写成分快照的 ACTIVE 板块。"""
    return StoredSector(
        sector_key=key,
        sector_id=uuid4(),
        identifier=SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, code),
        name=name,
        status="ACTIVE",
    )
