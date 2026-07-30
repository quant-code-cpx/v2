"""已停用的板块目录历史 CLI。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.legacy_entrypoints import reject_legacy_cli

_ENTRYPOINT = "data-sync-sector-catalog"


def main(argv: Sequence[str] | None = None) -> int:
    """拒绝旧目录同步，直到该数据集具备 fenced dispatcher 执行器。"""
    reject_legacy_cli(entrypoint=_ENTRYPOINT, argv=argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
