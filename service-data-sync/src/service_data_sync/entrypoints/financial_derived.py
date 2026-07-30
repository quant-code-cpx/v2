"""已停用的财务派生指标历史 CLI。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.legacy_entrypoints import reject_legacy_cli

_ENTRYPOINT = "data-sync-financial-derived"


def main(argv: Sequence[str] | None = None) -> int:
    """拒绝旧派生发布，避免模型计算绕过 command 审计与全局执行槽。"""
    reject_legacy_cli(entrypoint=_ENTRYPOINT, argv=argv, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
