"""已停用的申万行业历史 CLI。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.legacy_entrypoints import reject_legacy_cli

_ENTRYPOINT = "data-sync-sw-sector"


def main(argv: Sequence[str] | None = None) -> int:
    """拒绝旧申万同步和 replay，避免独立 publication 绕过全局 fencing。"""
    reject_legacy_cli(entrypoint=_ENTRYPOINT, argv=argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
