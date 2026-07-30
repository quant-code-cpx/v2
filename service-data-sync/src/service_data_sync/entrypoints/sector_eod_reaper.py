"""已停用的板块 EOD 旧租约回收 CLI。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.legacy_entrypoints import reject_legacy_cli

_ENTRYPOINT = "data-sync-sector-eod-reaper"


def main(argv: Sequence[str] | None = None) -> int:
    """拒绝旧 reaper，过期恢复只能由 data-operations reaper 在 fence 下执行。"""
    reject_legacy_cli(entrypoint=_ENTRYPOINT, argv=argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
