"""已停用的衍生品合约日线历史 CLI。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.legacy_entrypoints import reject_legacy_cli

_ENTRYPOINT = "data-sync-derivative-bars"


def main(argv: Sequence[str] | None = None) -> int:
    """拒绝旧合约日线同步，避免未接入 fence 的来源与 publication 路径继续运行。"""
    reject_legacy_cli(entrypoint=_ENTRYPOINT, argv=argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
