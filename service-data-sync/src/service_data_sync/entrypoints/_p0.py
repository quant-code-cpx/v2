"""P0 受控同步入口共用的来源批准、窗口校验和 adapter 选择逻辑。

P0 能力可在个人内部研究环境中保留“来源尚不可用”的明确状态，但所有入口必须复用
这里的授权与唯一选择规则，避免各数据集对再分发限制或缺失来源产生不一致解释。
"""

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
    """表示 adapter 尚未注册时的成功空结果，不把配置缺口伪装成同步失败。

    P0 个人研究能力允许在未接入来源时安全返回空集，并由可用性账本记录原因；这与
    已选择来源后的解析、质量或写入失败不同，后者必须失败留证以支持排障。
    """

    data_version: UUID | None = None
    inserted_count: int = 0
    unchanged_count: int = 0
    availability: str = "source_unavailable"


def add_source_approval_arguments(parser: argparse.ArgumentParser) -> None:
    """注册来源身份与使用范围参数，并保留个人内部研究的安全默认值。

    这些参数不是让调用者任意放宽授权：CLI 只接受已声明的内部研究策略，来源名称等
    元数据则会随 canonical 记录写入，供后续血缘和再分发审查使用。
    """
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
    """把经 CLI 显式确认的研究策略映射为不可再分发的来源批准值对象。

    这里集中固定 `rights_status` 和 `license_scope`，避免每个 P0 入口自行拼装时
    漏记限制，或把供应商标识误写成可以对外消费的授权声明。
    """
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
    """返回唯一精确 adapter；缺失或歧义时由调用方写成功空状态。

    `capability` 已在注册表查询阶段过滤，这里只再核对 `provider_id` 的唯一性。若
    有零个或多个匹配项，宁可不发请求，也不凭注册顺序猜测一个可能错误的数据源。
    """
    del capability
    matched = tuple(source for source in sources if source.provider_id == provider_id)
    return matched[0] if len(matched) == 1 else None


def is_source_unavailable_error(error: ProviderError) -> bool:
    """判断可降级为成功空结果的来源故障；schema 和写入失败仍必须失败留证。

    此分类仅用于个人研究 P0 的“尚无可用来源”语义，不代表数据有效；任何已经收到
    载荷后的格式、质量或持久化问题都不能走此分支，以免消费者误把损坏数据当空集。
    """
    return error.code in {
        ProviderErrorCode.UNAVAILABLE,
        ProviderErrorCode.RATE_LIMITED,
        ProviderErrorCode.AUTHENTICATION,
        ProviderErrorCode.INVALID_REQUEST,
    }


def require_window(arguments: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """拒绝倒置日期窗口，确保每个受控 backfill 都有明确、有限且可审计的边界。"""
    if arguments.start > arguments.end:
        parser.error("--start must not be after --end")
