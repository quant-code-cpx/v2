"""已停用的板块 EOD 历史 CLI。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.legacy_entrypoints import reject_legacy_cli

_ENTRYPOINT = "data-sync-sector-eod"


def main(argv: Sequence[str] | None = None) -> int:
    """拒绝旧 EOD 同步及其私有重放参数，避免绕过 command、slot 与 fence。"""
    reject_legacy_cli(entrypoint=_ENTRYPOINT, argv=argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
