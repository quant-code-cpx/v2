"""已停用的两融数据历史 CLI。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.legacy_entrypoints import reject_legacy_cli

_ENTRYPOINT = "data-sync-margin"


def main(argv: Sequence[str] | None = None) -> int:
    """拒绝旧两融同步，防止自由来源参数绕过 command 和全局 fenced 执行。"""
    reject_legacy_cli(entrypoint=_ENTRYPOINT, argv=argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
