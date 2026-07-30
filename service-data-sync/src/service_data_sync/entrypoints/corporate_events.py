"""已停用的公司事件历史 CLI。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.legacy_entrypoints import reject_legacy_cli

_ENTRYPOINT = "data-sync-corporate-events"


def main(argv: Sequence[str] | None = None) -> int:
    """拒绝旧公司事件同步，直到存在有界 selector 的 fenced 执行器。"""
    reject_legacy_cli(entrypoint=_ENTRYPOINT, argv=argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
