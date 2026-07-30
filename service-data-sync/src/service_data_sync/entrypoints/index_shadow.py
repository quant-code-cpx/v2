"""已停用的指数影子观测历史 CLI。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.legacy_entrypoints import reject_legacy_cli

_ENTRYPOINT = "data-sync-index-shadow"


def main(argv: Sequence[str] | None = None) -> int:
    """拒绝旧影子同步，防止 research 观察在 command 之外访问来源或写入数据库。"""
    reject_legacy_cli(entrypoint=_ENTRYPOINT, argv=argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
