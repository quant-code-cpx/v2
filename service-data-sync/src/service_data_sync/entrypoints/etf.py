"""已停用的 ETF 历史 CLI。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.legacy_entrypoints import reject_legacy_cli

_ENTRYPOINT = "data-sync-etf"


def main(argv: Sequence[str] | None = None) -> int:
    """拒绝旧 ETF 同步，避免目录、行情和状态写入绕过统一 command。"""
    reject_legacy_cli(entrypoint=_ENTRYPOINT, argv=argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
