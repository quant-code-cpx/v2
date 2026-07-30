"""已停用的证券生命周期历史 CLI。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.legacy_entrypoints import reject_legacy_cli

_ENTRYPOINT = "data-sync-equity-lifecycle"


def main(argv: Sequence[str] | None = None) -> int:
    """拒绝旧生命周期同步与 replay，避免它们单独写入 checkpoint 或终态。"""
    reject_legacy_cli(entrypoint=_ENTRYPOINT, argv=argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
