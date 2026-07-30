"""已停用的板块行情历史 CLI。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.legacy_entrypoints import reject_legacy_cli

_ENTRYPOINT = "data-sync-sector-bars"


def main(argv: Sequence[str] | None = None) -> int:
    """拒绝旧行情同步，避免它绕过全局 execution slot 和 fencing。"""
    reject_legacy_cli(entrypoint=_ENTRYPOINT, argv=argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
