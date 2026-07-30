"""已停用的板块成分历史 CLI。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.legacy_entrypoints import reject_legacy_cli

_ENTRYPOINT = "data-sync-sector-membership"


def main(argv: Sequence[str] | None = None) -> int:
    """拒绝旧成分同步，防止独立 run ledger 绕过 command 生命周期。"""
    reject_legacy_cli(entrypoint=_ENTRYPOINT, argv=argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
