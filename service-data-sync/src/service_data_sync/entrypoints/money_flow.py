"""已停用的日频资金流历史 CLI。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.legacy_entrypoints import reject_legacy_cli

_ENTRYPOINT = "data-sync-money-flow"


def main(argv: Sequence[str] | None = None) -> int:
    """拒绝旧资金流同步，避免任意 capability 参数直接触发 canonical 写入。"""
    reject_legacy_cli(entrypoint=_ENTRYPOINT, argv=argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
