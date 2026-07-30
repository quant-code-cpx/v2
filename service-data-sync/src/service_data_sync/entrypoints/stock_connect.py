"""已停用的港通数据历史 CLI。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.legacy_entrypoints import reject_legacy_cli

_ENTRYPOINT = "data-sync-stock-connect"


def main(argv: Sequence[str] | None = None) -> int:
    """拒绝旧港通同步，避免方向和渠道参数直接创建 canonical 写入。"""
    reject_legacy_cli(entrypoint=_ENTRYPOINT, argv=argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
