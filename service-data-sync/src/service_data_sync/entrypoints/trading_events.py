"""已停用的交易公开信息历史 CLI。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.legacy_entrypoints import reject_legacy_cli

_ENTRYPOINT = "data-sync-trading-events"


def main(argv: Sequence[str] | None = None) -> int:
    """拒绝旧交易事件同步，防止操作类型直接绕过 command 审计和全局 slot。"""
    reject_legacy_cli(entrypoint=_ENTRYPOINT, argv=argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
