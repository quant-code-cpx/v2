"""以 PostgreSQL 原子保存并逐页复核不可变来源交付清单。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from service_data_sync.application.ports.delivery_manifest import (
    DELIVERY_MANIFEST_SCHEMA,
    DeliveryManifestIntegrityError,
    DeliveryManifestPage,
    DeliveryManifestPageDescriptor,
    DeliveryManifestReference,
    DeliveryManifestStatus,
    DeliveryManifestUnavailable,
    ImmutableDeliveryManifest,
    delivery_manifest_page_descriptor,
    delivery_manifest_root_hash,
    verify_delivery_manifest_page,
    verify_immutable_delivery_manifest,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.delivery_manifest import (
    DataOperationDeliveryManifest,
    DataOperationDeliveryManifestPage,
)


class SqlAlchemyDeliveryManifestRepository:
    """通过短事务持久化 header/page，并在每次读取时重新验证摘要链。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存服务自有数据库连接，不跨调用复用会话。"""
        self._database = database

    def persist(self, manifest: ImmutableDeliveryManifest) -> DeliveryManifestReference:
        """原子插入完整清单；相同 UUID 只允许逐页完全一致的幂等重放。"""
        verify_immutable_delivery_manifest(manifest)
        with self._database.transaction() as session:
            existing = session.get(DataOperationDeliveryManifest, manifest.manifest_id)
            if existing is not None:
                reference, descriptors = self._verified_header_and_descriptors(
                    session,
                    manifest_id=manifest.manifest_id,
                    expected_root_hash=manifest.root_hash,
                )
                if descriptors != tuple(
                    delivery_manifest_page_descriptor(page) for page in manifest.pages
                ):
                    raise DeliveryManifestIntegrityError(
                        "persisted delivery manifest page directory differs from replay"
                    )
                for page in manifest.pages:
                    if (
                        self._load_verified_page(
                            session,
                            manifest_id=manifest.manifest_id,
                            page_no=page.page_no,
                            descriptors=descriptors,
                        )
                        != page
                    ):
                        raise DeliveryManifestIntegrityError(
                            "persisted delivery manifest page differs from replay"
                        )
                return reference
            session.add(
                DataOperationDeliveryManifest(
                    manifest_id=manifest.manifest_id,
                    schema_version=DELIVERY_MANIFEST_SCHEMA,
                    dataset_code=manifest.dataset_code,
                    provider_id=manifest.provider_id,
                    request_hash=manifest.request_hash,
                    root_hash=manifest.root_hash,
                    status=manifest.status,
                    target_count=manifest.target_count,
                    page_count=manifest.page_count,
                    available_until=manifest.available_until,
                    minimum_remaining_seconds=manifest.minimum_remaining_seconds,
                    created_at=manifest.created_at,
                )
            )
            session.add_all(
                [
                    DataOperationDeliveryManifestPage(
                        manifest_id=manifest.manifest_id,
                        page_no=page.page_no,
                        date_from=page.date_from,
                        date_to=page.date_to,
                        trade_date_count=page.trade_date_count,
                        target_count=page.target_count,
                        page_hash=page.page_hash,
                        evidence_json=dict(page.evidence),
                        created_at=manifest.created_at,
                    )
                    for page in manifest.pages
                ]
            )
            session.flush()
            return _reference_from_header(manifest)

    def require_available(
        self,
        *,
        manifest_id: UUID,
        expected_root_hash: str,
        observed_at: datetime,
        required_remaining_seconds: int = 0,
    ) -> DeliveryManifestReference:
        """复核摘要、状态和受理窗口，拒绝到期或剩余时间不足的清单。"""
        _require_aware(observed_at)
        if required_remaining_seconds < 0:
            raise ValueError("required delivery manifest remaining window is invalid")
        with self._database.session() as session:
            reference, _descriptors = self._verified_header_and_descriptors(
                session,
                manifest_id=manifest_id,
                expected_root_hash=expected_root_hash,
            )
            if reference.status != "ELIGIBLE":
                raise DeliveryManifestUnavailable("delivery manifest is not eligible")
            if reference.available_until <= observed_at:
                raise DeliveryManifestUnavailable("delivery manifest has expired")
            required = max(
                required_remaining_seconds,
                reference.minimum_remaining_seconds,
            )
            if reference.available_until < observed_at + timedelta(seconds=required):
                raise DeliveryManifestUnavailable(
                    "delivery manifest remaining availability window is insufficient"
                )
            return reference

    def list_page_descriptors(
        self,
        *,
        manifest_id: UUID,
        expected_root_hash: str,
        observed_at: datetime,
    ) -> tuple[DeliveryManifestPageDescriptor, ...]:
        """返回有序页目录；执行期只要求清单尚未到绝对截止时间。"""
        _require_aware(observed_at)
        with self._database.session() as session:
            reference, descriptors = self._verified_header_and_descriptors(
                session,
                manifest_id=manifest_id,
                expected_root_hash=expected_root_hash,
            )
            _require_executable(reference, observed_at=observed_at)
            return descriptors

    def load_page(
        self,
        *,
        manifest_id: UUID,
        expected_root_hash: str,
        page_no: int,
        observed_at: datetime,
    ) -> DeliveryManifestPage:
        """仅读取所需正文页，同时用轻量目录重新计算整份根摘要。"""
        _require_aware(observed_at)
        if page_no < 0:
            raise ValueError("delivery manifest page number is invalid")
        with self._database.session() as session:
            reference, descriptors = self._verified_header_and_descriptors(
                session,
                manifest_id=manifest_id,
                expected_root_hash=expected_root_hash,
            )
            _require_executable(reference, observed_at=observed_at)
            return self._load_verified_page(
                session,
                manifest_id=manifest_id,
                page_no=page_no,
                descriptors=descriptors,
            )

    def load_pages_for_audit(
        self,
        *,
        manifest_id: UUID,
        expected_root_hash: str,
    ) -> tuple[DeliveryManifestPage, ...]:
        """不受 entitlement 到期影响地复核并读取全部不可变页面，专供完成后 coverage 审计。"""
        with self._database.session() as session:
            reference, descriptors = self._verified_header_and_descriptors(
                session,
                manifest_id=manifest_id,
                expected_root_hash=expected_root_hash,
            )
            if reference.status != "ELIGIBLE":
                raise DeliveryManifestUnavailable("delivery manifest is not eligible")
            return tuple(
                self._load_verified_page(
                    session,
                    manifest_id=manifest_id,
                    page_no=descriptor.page_no,
                    descriptors=descriptors,
                )
                for descriptor in descriptors
            )

    def _verified_header_and_descriptors(
        self,
        session: Session,
        *,
        manifest_id: UUID,
        expected_root_hash: str,
    ) -> tuple[DeliveryManifestReference, tuple[DeliveryManifestPageDescriptor, ...]]:
        """读取 header 与轻量页面目录，并重新计算全局 root hash。"""
        if len(expected_root_hash) != 64:
            raise DeliveryManifestIntegrityError("expected delivery manifest root hash is invalid")
        header = session.get(DataOperationDeliveryManifest, manifest_id)
        if header is None:
            raise DeliveryManifestUnavailable("delivery manifest is unavailable")
        if (
            header.schema_version != DELIVERY_MANIFEST_SCHEMA
            or header.root_hash != expected_root_hash
        ):
            raise DeliveryManifestIntegrityError("delivery manifest header identity differs")
        rows = session.execute(
            select(
                DataOperationDeliveryManifestPage.page_no,
                DataOperationDeliveryManifestPage.date_from,
                DataOperationDeliveryManifestPage.date_to,
                DataOperationDeliveryManifestPage.trade_date_count,
                DataOperationDeliveryManifestPage.target_count,
                DataOperationDeliveryManifestPage.page_hash,
            )
            .where(DataOperationDeliveryManifestPage.manifest_id == manifest_id)
            .order_by(DataOperationDeliveryManifestPage.page_no)
        ).all()
        descriptors = tuple(
            DeliveryManifestPageDescriptor(
                page_no=int(row.page_no),
                date_from=row.date_from,
                date_to=row.date_to,
                trade_date_count=int(row.trade_date_count),
                target_count=int(row.target_count),
                page_hash=str(row.page_hash),
            )
            for row in rows
        )
        _verify_descriptors(header, descriptors)
        status = cast(DeliveryManifestStatus, header.status)
        calculated_root = delivery_manifest_root_hash(
            dataset_code=header.dataset_code,
            provider_id=header.provider_id,
            request_hash=header.request_hash,
            status=status,
            available_until=header.available_until,
            minimum_remaining_seconds=header.minimum_remaining_seconds,
            target_count=header.target_count,
            descriptors=descriptors,
        )
        if calculated_root != header.root_hash:
            raise DeliveryManifestIntegrityError("delivery manifest root hash differs")
        return _reference_from_model(header), descriptors

    def _load_verified_page(
        self,
        session: Session,
        *,
        manifest_id: UUID,
        page_no: int,
        descriptors: tuple[DeliveryManifestPageDescriptor, ...],
    ) -> DeliveryManifestPage:
        """读取单页正文并要求它与根摘要目录中的同序项完全一致。"""
        row = session.get(
            DataOperationDeliveryManifestPage,
            {"manifest_id": manifest_id, "page_no": page_no},
        )
        if row is None:
            raise DeliveryManifestIntegrityError("delivery manifest page is unavailable")
        page = DeliveryManifestPage(
            page_no=row.page_no,
            date_from=row.date_from,
            date_to=row.date_to,
            trade_date_count=row.trade_date_count,
            target_count=row.target_count,
            evidence=dict(row.evidence_json),
            page_hash=row.page_hash,
        )
        verify_delivery_manifest_page(page)
        if (
            page_no >= len(descriptors)
            or delivery_manifest_page_descriptor(page) != descriptors[page_no]
        ):
            raise DeliveryManifestIntegrityError("delivery manifest page directory differs")
        return page


def _verify_descriptors(
    header: DataOperationDeliveryManifest,
    descriptors: tuple[DeliveryManifestPageDescriptor, ...],
) -> None:
    """校验页号、日期范围和 header 总计数，防止缺页、重排和交叠。"""
    if len(descriptors) != header.page_count:
        raise DeliveryManifestIntegrityError("delivery manifest page count differs")
    if [item.page_no for item in descriptors] != list(range(len(descriptors))):
        raise DeliveryManifestIntegrityError("delivery manifest page numbers are not contiguous")
    if sum(item.target_count for item in descriptors) != header.target_count:
        raise DeliveryManifestIntegrityError("delivery manifest target count differs")
    previous_date_from = None
    for item in descriptors:
        if (
            item.date_from > item.date_to
            or not 1 <= item.trade_date_count <= 20
            or not 1 <= item.target_count <= 256
            or len(item.page_hash) != 64
            or (previous_date_from is not None and item.date_to >= previous_date_from)
        ):
            raise DeliveryManifestIntegrityError("delivery manifest page directory is invalid")
        previous_date_from = item.date_from
    if header.status == "ELIGIBLE" and not descriptors:
        raise DeliveryManifestIntegrityError("eligible delivery manifest has no pages")
    if header.status == "REJECTED" and descriptors:
        raise DeliveryManifestIntegrityError("rejected delivery manifest contains pages")


def _reference_from_header(manifest: ImmutableDeliveryManifest) -> DeliveryManifestReference:
    """把应用层完整清单投影为小型引用。"""
    return DeliveryManifestReference(
        manifest_id=manifest.manifest_id,
        root_hash=manifest.root_hash,
        status=manifest.status,
        target_count=manifest.target_count,
        page_count=manifest.page_count,
        available_until=manifest.available_until,
        minimum_remaining_seconds=manifest.minimum_remaining_seconds,
    )


def _reference_from_model(
    header: DataOperationDeliveryManifest,
) -> DeliveryManifestReference:
    """把数据库 header 投影为不含私有页面正文的小型引用。"""
    return DeliveryManifestReference(
        manifest_id=header.manifest_id,
        root_hash=header.root_hash,
        status=cast(DeliveryManifestStatus, header.status),
        target_count=header.target_count,
        page_count=header.page_count,
        available_until=header.available_until,
        minimum_remaining_seconds=header.minimum_remaining_seconds,
    )


def _require_executable(
    reference: DeliveryManifestReference,
    *,
    observed_at: datetime,
) -> None:
    """要求执行读取面对的是可执行且尚未到绝对截止时间的清单。"""
    if reference.status != "ELIGIBLE":
        raise DeliveryManifestUnavailable("delivery manifest is not eligible")
    if reference.available_until <= observed_at:
        raise DeliveryManifestUnavailable("delivery manifest has expired")


def _require_aware(value: datetime) -> None:
    """拒绝无时区时间，避免不同进程对可用窗口产生不同判断。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("delivery manifest observed time must be timezone-aware")
