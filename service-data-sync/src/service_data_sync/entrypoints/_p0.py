"""P0 受控同步入口共用的来源批准、窗口校验和 adapter 选择逻辑。"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderError,
    ProviderErrorCode,
)
from service_data_sync.infrastructure.persistence.typed_p0_support import TypedP0SourceApproval

_PERSONAL_INTERNAL_RESEARCH = "personal-internal-research"
_PERSONAL_INTERNAL_RIGHTS_STATUS = "personal_internal_research"
_NO_REDISTRIBUTION_SCOPE = "internal_research_no_redistribution"


@dataclass(frozen=True, slots=True)
class P0UnavailableSyncResult:
    """表示 adapter 尚未注册时的成功空结果，不将配置缺口升级为链路失败。"""

    data_version: UUID | None = None
    inserted_count: int = 0
    unchanged_count: int = 0
    availability: str = "source_unavailable"


def add_source_approval_arguments(parser: argparse.ArgumentParser) -> None:
    """注册可覆盖的 AKShare 默认来源与个人内部研究使用范围。"""
    parser.add_argument("--provider-id", default="akshare")
    parser.add_argument("--source-code", default="akshare")
    parser.add_argument("--source-legal-name", default="AKShare")
    parser.add_argument("--source-kind", default="community_aggregator")
    parser.add_argument(
        "--source-policy",
        choices=(_PERSONAL_INTERNAL_RESEARCH,),
        default=_PERSONAL_INTERNAL_RESEARCH,
        help="仅允许个人内部研究；禁止对外再分发。",
    )


def build_source_approval[Approval: TypedP0SourceApproval](
    arguments: argparse.Namespace, approval_type: type[Approval]
) -> Approval:
    """把显式个人研究策略映射为不可再分发的领域来源批准项。"""
    if arguments.source_policy != _PERSONAL_INTERNAL_RESEARCH:
        raise ValueError("unsupported P0 source policy")
    return approval_type(
        provider_id=arguments.provider_id,
        source_code=arguments.source_code,
        legal_name=arguments.source_legal_name,
        source_kind=arguments.source_kind,
        rights_status=_PERSONAL_INTERNAL_RIGHTS_STATUS,
        license_scope=_NO_REDISTRIBUTION_SCOPE,
    )


def select_single_source(
    *, sources: Sequence[DataSourcePort], provider_id: str, capability: str
) -> DataSourcePort | None:
    """返回唯一精确 adapter；缺失或歧义时交由调用方写成功空状态。"""
    del capability
    matched = tuple(source for source in sources if source.provider_id == provider_id)
    return matched[0] if len(matched) == 1 else None


def is_source_unavailable_error(error: ProviderError) -> bool:
    """判断可降级为成功空结果的来源故障；schema 和写入失败仍必须失败留证。"""
    return error.code in {
        ProviderErrorCode.UNAVAILABLE,
        ProviderErrorCode.RATE_LIMITED,
        ProviderErrorCode.AUTHENTICATION,
        ProviderErrorCode.INVALID_REQUEST,
    }


def require_window(arguments: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """拒绝倒置日期窗口，确保每个受控 backfill 都有明确且有限的边界。"""
    if arguments.start > arguments.end:
        parser.error("--start must not be after --end")
