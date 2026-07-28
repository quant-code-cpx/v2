"""板块三周期修订、发布和读取 SQL 编排的单元测试。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import Engine

from service_data_sync.domain.sector import (
    SectorBar,
    SectorCatalogEntry,
    SectorIdentifier,
    SectorPeriod,
    SectorScheme,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.sector_market_data_repository import (
    SqlAlchemySectorMarketDataRepository,
    _bar_content_hash,
)


class FakeResult:
    """模拟仓储使用的 SQLAlchemy 映射结果读取接口。"""

    def __init__(self, value: object) -> None:
        """保存一个排队的单行、可选行或多行响应。"""
        self._value = value

    def mappings(self) -> FakeResult:
        """排队数据已经是映射形字典，因此返回当前替身。"""
        return self

    def scalar_one(self) -> object:
        """模拟 ORM-enabled `RETURNING` 的单一标量结果。"""
        if isinstance(self._value, dict):
            return self._value["source_batch_id"]
        return self._value

    def one_or_none(self) -> object:
        """为允许无匹配的 SELECT 返回可选响应。"""
        return self._value

    def one(self) -> object:
        """为必须返回一行的 INSERT 或 SELECT 提供响应。"""
        assert self._value is not None
        return self._value

    def all(self) -> list[object]:
        """为有界读取返回排队的多行响应。"""
        assert isinstance(self._value, list)
        return self._value


class FakeConnection:
    """顺序回放 SQL 响应并保存语句文本，避免启动真实数据库。"""

    def __init__(self, responses: list[object]) -> None:
        """初始化共享的确定性响应队列。"""
        self._responses = responses
        self.statements: list[str] = []
        self.parameters: list[object] = []

    def execute(self, statement: object, parameters: object = None) -> FakeResult:
        """记录 SQL 文本并在结果读取时提供下一项响应。"""
        self.statements.append(str(statement))
        self.parameters.append(parameters)
        response = self._responses.pop(0) if self._responses else None
        return FakeResult(response)


class FakeEngine:
    """提供仓储所需的事务与只读连接上下文。"""

    def __init__(self, responses: list[object]) -> None:
        """创建由 begin 和 connect 共用的替身连接。"""
        self.connection = FakeConnection(responses)

    @contextmanager
    def begin(self) -> Iterator[FakeConnection]:
        """产出无需提交外部状态的事务形连接。"""
        yield self.connection

    @contextmanager
    def connect(self) -> Iterator[FakeConnection]:
        """产出无需建立套接字的读取形连接。"""
        yield self.connection


def test_repository_writes_weekly_table_and_advances_weekly_publication() -> None:
    """周线只写周线物理表与周线发布数据集，不触碰日线表。"""
    sector_id = uuid4()
    engine = FakeEngine([_sector_row(sector_id), {"source_batch_id": uuid4()}, None, None, None])
    repository = _repository(engine)

    publication = repository.publish_bars(
        identifier=SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, "BK0475"),
        period=SectorPeriod.WEEK_1,
        bars=(_bar(),),
        provider_id="test-provider",
        source_payload_sha256="a" * 64,
        raw_uri="s3://test/raw.json",
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    statements = "\n".join(engine.connection.statements)
    assert publication.inserted_count == 1
    assert "sector_weekly_bar" in statements
    assert "sector_daily_bar" not in statements
    assert "INSERT INTO source_batch" in statements


def test_repository_keeps_current_monthly_publication_when_values_are_unchanged() -> None:
    """同值月线重放只保留现有当前发布，避免制造伪数据版本。"""
    sector_id = uuid4()
    bar = _bar()
    data_version = uuid4()
    engine = FakeEngine(
        [
            _sector_row(sector_id),
            {"source_batch_id": uuid4()},
            {"revision": 1, "content_sha256": _bar_content_hash(bar, is_final=True)},
            {"data_version": data_version},
        ]
    )
    repository = _repository(engine)

    publication = repository.publish_bars(
        identifier=SectorIdentifier(SectorScheme.EASTMONEY_CONCEPT, "BK0475"),
        period=SectorPeriod.MONTH_1,
        bars=(bar,),
        provider_id="test-provider",
        source_payload_sha256="b" * 64,
        raw_uri="s3://test/raw.json",
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    assert publication.data_version == data_version
    assert publication.inserted_count == 0
    assert publication.unchanged_count == 1


def test_repository_reads_current_daily_bar_from_daily_table() -> None:
    """内部读取依据请求周期选择日线表并映射为标准领域行。"""
    sector_id = uuid4()
    engine = FakeEngine(
        [
            [
                {
                    "period_end": date(2026, 6, 30),
                    "open_price": Decimal("10"),
                    "high_price": Decimal("11"),
                    "low_price": Decimal("9"),
                    "close_price": Decimal("10.5"),
                    "volume_value": Decimal("1000"),
                    "volume_unit": "provider_native",
                    "amount_cny": Decimal("10500"),
                    "amplitude_percent": Decimal("20"),
                    "change_percent": Decimal("5"),
                    "change_amount": Decimal("0.5"),
                    "turnover_percent": Decimal("3"),
                    "revision": 1,
                    "is_final": True,
                }
            ]
        ]
    )
    repository = _repository(engine)

    bars = repository.list_bars(
        sector_id=sector_id,
        period=SectorPeriod.DAY_1,
        start=date(2026, 6, 1),
        end=date(2026, 6, 30),
    )

    assert bars[0][0].close_price == Decimal("10.5")
    assert bars[0][1:] == (1, True)
    assert "sector_daily_bar" in engine.connection.statements[0]


def test_repository_publishes_catalog_activates_pending_sector_and_reads_publication() -> None:
    """目录发布应保留占位 UUID、激活名称并提供稳定当前版本读取。"""
    data_version = uuid4()
    published_at = datetime(2026, 7, 1, tzinfo=UTC)
    engine = FakeEngine(
        [
            {"source_batch_id": uuid4()},
            {"sector_key": 1, "name": None, "status": "PENDING"},
            None,
            None,
            None,
            {"data_version": data_version, "published_at": published_at},
        ]
    )
    repository = _repository(engine)

    publication = repository.publish_catalog(
        scheme=SectorScheme.EASTMONEY_INDUSTRY,
        entries=(
            SectorCatalogEntry(SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, "BK0475"), "证券"),
        ),
        provider_id="test-provider",
        source_payload_sha256="c" * 64,
        raw_uri="s3://test/catalog.json",
        observed_at=published_at,
    )
    current = repository.get_current_publication(
        dataset="sector.catalog.raw", partition_key="eastmoney.industry"
    )

    statements = "\n".join(engine.connection.statements)
    assert publication.inserted_count == 1
    assert "SET name = :name, status = 'ACTIVE'" in statements
    assert current is not None
    assert current.data_version == data_version


def test_repository_reads_active_catalog_by_identifier_and_stable_cursor() -> None:
    """内部目录读取必须选择 ACTIVE 状态，并以代码 UUID 顺序接受完整游标。"""
    sector_id = uuid4()
    identifier = SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, "BK0475")
    by_identifier_engine = FakeEngine([_sector_row(sector_id)])
    paged_engine = FakeEngine([[_sector_row(sector_id)]])

    stored = _repository(by_identifier_engine).get_sector_by_identifier(identifier)
    page = _repository(paged_engine).list_active_sectors(
        scheme=SectorScheme.EASTMONEY_INDUSTRY,
        query="证",
        after_code="BK0400",
        after_sector_id=uuid4(),
        limit=2,
    )

    assert stored is not None
    assert stored.identifier == identifier
    assert page[0].status == "ACTIVE"
    assert "status = 'ACTIVE'" in paged_engine.connection.statements[0]


def _repository(engine: FakeEngine) -> SqlAlchemySectorMarketDataRepository:
    """使用类型转换后的替身引擎构造仓储，不加载运行时配置。"""
    return SqlAlchemySectorMarketDataRepository(DatabaseClient(engine=cast(Engine, engine)))


def _sector_row(sector_id: UUID) -> dict[str, Any]:
    """返回最小标准板块身份查询行。"""
    return {
        "sector_key": 1,
        "sector_id": sector_id,
        "scheme": "eastmoney.industry",
        "sector_code": "BK0475",
        "name": "证券",
        "status": "ACTIVE",
    }


def _bar() -> SectorBar:
    """构造一条有效的板块行情，供各物理周期测试复用。"""
    return SectorBar(
        period_end=date(2026, 6, 30),
        open_price=Decimal("10"),
        high_price=Decimal("11"),
        low_price=Decimal("9"),
        close_price=Decimal("10.5"),
        volume_value=Decimal("1000"),
        volume_unit="provider_native",
        amount_cny=Decimal("10500"),
        amplitude_percent=Decimal("20"),
        change_percent=Decimal("5"),
        change_amount=Decimal("0.5"),
        turnover_percent=Decimal("3"),
    )
