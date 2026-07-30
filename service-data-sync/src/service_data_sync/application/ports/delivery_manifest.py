"""定义大规模来源交付清单的不可变分页合同与完整性算法。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID

DELIVERY_MANIFEST_SCHEMA = "quant-v2.delivery-manifest.v1"
DELIVERY_MANIFEST_PAGE_SCHEMA = "quant-v2.delivery-manifest-page.v1"
MAX_DELIVERY_MANIFEST_TRADE_DATES_PER_PAGE = 20
MAX_DELIVERY_MANIFEST_TARGETS_PER_PAGE = 256

type DeliveryManifestStatus = Literal["ELIGIBLE", "REJECTED"]


class DeliveryManifestIntegrityError(RuntimeError):
    """表示清单或页面与其冻结摘要不一致，调用方必须停止执行。"""


class DeliveryManifestUnavailable(RuntimeError):
    """表示清单不存在、被拒绝、已过期或剩余可用窗口不足。"""


@dataclass(frozen=True, slots=True)
class DeliveryManifestTradeDate:
    """保存一个交易日的完整交付证据及该日业务目标数。"""

    trade_date: date
    target_count: int
    evidence: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class DeliveryManifestPage:
    """保存最多二十个交易日、二百五十六个目标的不可变证据页。"""

    page_no: int
    date_from: date
    date_to: date
    trade_date_count: int
    target_count: int
    evidence: Mapping[str, object]
    page_hash: str


@dataclass(frozen=True, slots=True)
class DeliveryManifestPageDescriptor:
    """保存无需读取页面正文即可校验根摘要的有序页面元数据。"""

    page_no: int
    date_from: date
    date_to: date
    trade_date_count: int
    target_count: int
    page_hash: str


@dataclass(frozen=True, slots=True)
class ImmutableDeliveryManifest:
    """保存一次预检最终冻结的 header、页面和有序根摘要。"""

    manifest_id: UUID
    dataset_code: str
    provider_id: str
    request_hash: str
    status: DeliveryManifestStatus
    available_until: datetime
    minimum_remaining_seconds: int
    target_count: int
    page_count: int
    root_hash: str
    pages: tuple[DeliveryManifestPage, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DeliveryManifestReference:
    """保存控制面和执行器传递所需的小型稳定引用。"""

    manifest_id: UUID
    root_hash: str
    status: DeliveryManifestStatus
    target_count: int
    page_count: int
    available_until: datetime
    minimum_remaining_seconds: int


class DeliveryManifestRepository(Protocol):
    """负责不可变清单的原子持久化、可用性校验与按页读取。"""

    def persist(self, manifest: ImmutableDeliveryManifest) -> DeliveryManifestReference:
        """原子写入 header 和全部页面；同一 ID 仅允许完全相同的幂等重放。"""
        ...

    def require_available(
        self,
        *,
        manifest_id: UUID,
        expected_root_hash: str,
        observed_at: datetime,
        required_remaining_seconds: int = 0,
    ) -> DeliveryManifestReference:
        """要求清单可受理且剩余窗口满足 header 与调用方中的较大值。"""
        ...

    def list_page_descriptors(
        self,
        *,
        manifest_id: UUID,
        expected_root_hash: str,
        observed_at: datetime,
    ) -> tuple[DeliveryManifestPageDescriptor, ...]:
        """返回有序页面目录，并复核 header 根摘要和到期时间。"""
        ...

    def load_page(
        self,
        *,
        manifest_id: UUID,
        expected_root_hash: str,
        page_no: int,
        observed_at: datetime,
    ) -> DeliveryManifestPage:
        """读取一页并同时复核页面摘要、全局根摘要和到期时间。"""
        ...

    def load_pages_for_audit(
        self,
        *,
        manifest_id: UUID,
        expected_root_hash: str,
    ) -> tuple[DeliveryManifestPage, ...]:
        """在交付到期后仍可只读复核不可变 header、目录和全部页面摘要。"""
        ...


def paginate_delivery_manifest_days(
    days: Sequence[DeliveryManifestTradeDate],
) -> tuple[DeliveryManifestPage, ...]:
    """按最新交易日优先分页，永不拆散同一日且同时限制日期数和目标数。"""
    ordered = sorted(days, key=lambda item: item.trade_date, reverse=True)
    if not ordered:
        return ()
    if len({item.trade_date for item in ordered}) != len(ordered):
        raise ValueError("delivery manifest trade dates must be unique")
    if any(
        item.target_count < 1 or item.target_count > MAX_DELIVERY_MANIFEST_TARGETS_PER_PAGE
        for item in ordered
    ):
        raise ValueError("delivery manifest day target count is outside approved bounds")

    pages: list[DeliveryManifestPage] = []
    current: list[DeliveryManifestTradeDate] = []
    current_targets = 0
    for item in ordered:
        exceeds_dates = len(current) >= MAX_DELIVERY_MANIFEST_TRADE_DATES_PER_PAGE
        exceeds_targets = (
            bool(current)
            and current_targets + item.target_count > MAX_DELIVERY_MANIFEST_TARGETS_PER_PAGE
        )
        if exceeds_dates or exceeds_targets:
            pages.append(_delivery_manifest_page(len(pages), current))
            current = []
            current_targets = 0
        current.append(item)
        current_targets += item.target_count
    if current:
        pages.append(_delivery_manifest_page(len(pages), current))
    return tuple(pages)


def build_immutable_delivery_manifest(
    *,
    manifest_id: UUID,
    dataset_code: str,
    provider_id: str,
    request_hash: str,
    status: DeliveryManifestStatus,
    available_until: datetime,
    minimum_remaining_seconds: int,
    created_at: datetime,
    days: Sequence[DeliveryManifestTradeDate] = (),
) -> ImmutableDeliveryManifest:
    """构造可直接原子持久化的完整清单，并冻结页面与 header 根摘要。"""
    _validate_header_scalars(
        dataset_code=dataset_code,
        provider_id=provider_id,
        request_hash=request_hash,
        status=status,
        available_until=available_until,
        minimum_remaining_seconds=minimum_remaining_seconds,
        created_at=created_at,
    )
    pages = paginate_delivery_manifest_days(days)
    if status == "ELIGIBLE" and not pages:
        raise ValueError("eligible delivery manifest must contain at least one page")
    if status == "REJECTED" and pages:
        raise ValueError("rejected delivery manifest must not retain executable pages")
    descriptors = tuple(delivery_manifest_page_descriptor(page) for page in pages)
    target_count = sum(page.target_count for page in pages)
    root_hash = delivery_manifest_root_hash(
        dataset_code=dataset_code,
        provider_id=provider_id,
        request_hash=request_hash,
        status=status,
        available_until=available_until,
        minimum_remaining_seconds=minimum_remaining_seconds,
        target_count=target_count,
        descriptors=descriptors,
    )
    manifest = ImmutableDeliveryManifest(
        manifest_id=manifest_id,
        dataset_code=dataset_code,
        provider_id=provider_id,
        request_hash=request_hash,
        status=status,
        available_until=available_until,
        minimum_remaining_seconds=minimum_remaining_seconds,
        target_count=target_count,
        page_count=len(pages),
        root_hash=root_hash,
        pages=pages,
        created_at=created_at,
    )
    verify_immutable_delivery_manifest(manifest)
    return manifest


def delivery_manifest_page_descriptor(
    page: DeliveryManifestPage,
) -> DeliveryManifestPageDescriptor:
    """把已冻结页面投影为不含正文的根摘要目录项。"""
    return DeliveryManifestPageDescriptor(
        page_no=page.page_no,
        date_from=page.date_from,
        date_to=page.date_to,
        trade_date_count=page.trade_date_count,
        target_count=page.target_count,
        page_hash=page.page_hash,
    )


def delivery_manifest_page_hash(page: DeliveryManifestPage) -> str:
    """根据页面序号、边界、计数和完整正文计算规范 SHA-256。"""
    return canonical_json_sha256(
        {
            "pageNo": page.page_no,
            "dateFrom": page.date_from.isoformat(),
            "dateTo": page.date_to.isoformat(),
            "tradeDateCount": page.trade_date_count,
            "targetCount": page.target_count,
            "evidence": dict(page.evidence),
        }
    )


def delivery_manifest_root_hash(
    *,
    dataset_code: str,
    provider_id: str,
    request_hash: str,
    status: DeliveryManifestStatus,
    available_until: datetime,
    minimum_remaining_seconds: int,
    target_count: int,
    descriptors: Sequence[DeliveryManifestPageDescriptor],
) -> str:
    """根据 header 语义和有序页面摘要计算全清单规范 SHA-256。"""
    return canonical_json_sha256(
        {
            "schema": DELIVERY_MANIFEST_SCHEMA,
            "datasetCode": dataset_code,
            "providerId": provider_id,
            "requestHash": request_hash,
            "status": status,
            "availableUntil": _timestamp(available_until),
            "minimumRemainingSeconds": minimum_remaining_seconds,
            "targetCount": target_count,
            "pageCount": len(descriptors),
            "pages": [
                {
                    "pageNo": item.page_no,
                    "dateFrom": item.date_from.isoformat(),
                    "dateTo": item.date_to.isoformat(),
                    "tradeDateCount": item.trade_date_count,
                    "targetCount": item.target_count,
                    "pageHash": item.page_hash,
                }
                for item in descriptors
            ],
        }
    )


def verify_delivery_manifest_page(page: DeliveryManifestPage) -> None:
    """复核页面边界、容量和摘要，任何差异均视为不可恢复的完整性错误。"""
    if (
        page.page_no < 0
        or page.date_from > page.date_to
        or not 1 <= page.trade_date_count <= MAX_DELIVERY_MANIFEST_TRADE_DATES_PER_PAGE
        or not 1 <= page.target_count <= MAX_DELIVERY_MANIFEST_TARGETS_PER_PAGE
        or len(page.page_hash) != 64
        or delivery_manifest_page_hash(page) != page.page_hash
    ):
        raise DeliveryManifestIntegrityError("delivery manifest page integrity check failed")


def verify_immutable_delivery_manifest(manifest: ImmutableDeliveryManifest) -> None:
    """复核 header、页面顺序、总计数和根摘要，拒绝部分或重排后的清单。"""
    _validate_header_scalars(
        dataset_code=manifest.dataset_code,
        provider_id=manifest.provider_id,
        request_hash=manifest.request_hash,
        status=manifest.status,
        available_until=manifest.available_until,
        minimum_remaining_seconds=manifest.minimum_remaining_seconds,
        created_at=manifest.created_at,
    )
    if manifest.page_count != len(manifest.pages):
        raise DeliveryManifestIntegrityError("delivery manifest page count does not match")
    if manifest.status == "ELIGIBLE" and not manifest.pages:
        raise DeliveryManifestIntegrityError("eligible delivery manifest has no pages")
    if manifest.status == "REJECTED" and manifest.pages:
        raise DeliveryManifestIntegrityError("rejected delivery manifest contains pages")
    expected_page_numbers = list(range(len(manifest.pages)))
    if [page.page_no for page in manifest.pages] != expected_page_numbers:
        raise DeliveryManifestIntegrityError("delivery manifest pages are not contiguous")
    previous_date_from: date | None = None
    for page in manifest.pages:
        verify_delivery_manifest_page(page)
        if previous_date_from is not None and page.date_to >= previous_date_from:
            raise DeliveryManifestIntegrityError("delivery manifest page dates overlap")
        previous_date_from = page.date_from
    if manifest.target_count != sum(page.target_count for page in manifest.pages):
        raise DeliveryManifestIntegrityError("delivery manifest target count does not match")
    descriptors = tuple(delivery_manifest_page_descriptor(page) for page in manifest.pages)
    expected_root = delivery_manifest_root_hash(
        dataset_code=manifest.dataset_code,
        provider_id=manifest.provider_id,
        request_hash=manifest.request_hash,
        status=manifest.status,
        available_until=manifest.available_until,
        minimum_remaining_seconds=manifest.minimum_remaining_seconds,
        target_count=manifest.target_count,
        descriptors=descriptors,
    )
    if len(manifest.root_hash) != 64 or manifest.root_hash != expected_root:
        raise DeliveryManifestIntegrityError("delivery manifest root hash does not match")


def canonical_json_sha256(value: object) -> str:
    """对仅含 JSON 值的对象生成跨进程稳定的 SHA-256。"""
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def delivery_manifest_page_byte_size(page: DeliveryManifestPage) -> int:
    """返回页面持久化前的规范 UTF-8 字节数，供容量门和压力测试使用。"""
    return len(
        _canonical_json_bytes(
            {
                "pageNo": page.page_no,
                "dateFrom": page.date_from.isoformat(),
                "dateTo": page.date_to.isoformat(),
                "tradeDateCount": page.trade_date_count,
                "targetCount": page.target_count,
                "pageHash": page.page_hash,
                "evidence": dict(page.evidence),
            }
        )
    )


def _delivery_manifest_page(
    page_no: int,
    days: Sequence[DeliveryManifestTradeDate],
) -> DeliveryManifestPage:
    """把连续完整交易日冻结为一页，并立即计算正文摘要。"""
    evidence = {
        "schema": DELIVERY_MANIFEST_PAGE_SCHEMA,
        "days": [
            {
                "tradeDate": item.trade_date.isoformat(),
                "targetCount": item.target_count,
                "evidence": dict(item.evidence),
            }
            for item in days
        ],
    }
    page_without_hash = DeliveryManifestPage(
        page_no=page_no,
        date_from=min(item.trade_date for item in days),
        date_to=max(item.trade_date for item in days),
        trade_date_count=len(days),
        target_count=sum(item.target_count for item in days),
        evidence=evidence,
        page_hash="",
    )
    page = DeliveryManifestPage(
        page_no=page_without_hash.page_no,
        date_from=page_without_hash.date_from,
        date_to=page_without_hash.date_to,
        trade_date_count=page_without_hash.trade_date_count,
        target_count=page_without_hash.target_count,
        evidence=page_without_hash.evidence,
        page_hash=delivery_manifest_page_hash(page_without_hash),
    )
    verify_delivery_manifest_page(page)
    return page


def _validate_header_scalars(
    *,
    dataset_code: str,
    provider_id: str,
    request_hash: str,
    status: DeliveryManifestStatus,
    available_until: datetime,
    minimum_remaining_seconds: int,
    created_at: datetime,
) -> None:
    """校验 header 标量、时区和最小剩余窗口，避免写入不可受理的清单。"""
    if not dataset_code.strip() or len(dataset_code) > 160:
        raise ValueError("delivery manifest dataset code is invalid")
    if not provider_id.strip() or len(provider_id) > 128:
        raise ValueError("delivery manifest provider id is invalid")
    if len(request_hash) != 64 or any(char not in "0123456789abcdef" for char in request_hash):
        raise ValueError("delivery manifest request hash is invalid")
    if status not in {"ELIGIBLE", "REJECTED"}:
        raise ValueError("delivery manifest status is invalid")
    _timestamp(created_at)
    _timestamp(available_until)
    if minimum_remaining_seconds < 0:
        raise ValueError("delivery manifest minimum remaining window is invalid")
    if available_until < created_at + timedelta(seconds=minimum_remaining_seconds):
        raise ValueError("delivery manifest availability window is too short")


def _timestamp(value: datetime) -> str:
    """把有时区时间规范为 UTC `Z` 表示，不接受含糊本地时间。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("delivery manifest timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json_bytes(value: object) -> bytes:
    """生成禁止 NaN 且键序稳定的紧凑 JSON 字节。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
