"""已停用的 A 股证券目录历史 CLI。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.legacy_entrypoints import reject_legacy_cli

_ENTRYPOINT = "data-sync-equity-catalog"


def main(argv: Sequence[str] | None = None) -> int:
    """拒绝旧目录发布，直到其具备 command 受控的 fenced canonical 执行器。"""
    reject_legacy_cli(entrypoint=_ENTRYPOINT, argv=argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
