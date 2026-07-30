"""已停用的板块 EOD 旧 publication rollback CLI。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.legacy_entrypoints import reject_legacy_cli

_ENTRYPOINT = "data-sync-sector-eod-rollback"


def main(argv: Sequence[str] | None = None) -> int:
    """拒绝旧 rollback，防止在没有当前 fencing token 时改写 publication。"""
    reject_legacy_cli(entrypoint=_ENTRYPOINT, argv=argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
