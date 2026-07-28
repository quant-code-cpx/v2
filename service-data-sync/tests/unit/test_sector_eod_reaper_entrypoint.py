"""板块 EOD 过期租约 reaper CLI 的资源边界测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from service_data_sync.entrypoints import sector_eod_reaper


class FakeContainer:
    """提供 reaper 所需的最小数据库依赖，并记录 finally 是否关闭资源。"""

    def __init__(self) -> None:
        """初始化不连接外部基础设施的数据库替身。"""
        self.database = object()
        self.closed = False

    def close(self) -> None:
        """记录入口在成功执行后关闭组合根。"""
        self.closed = True


def test_reaper_cli_requeues_expired_eod_leases_without_enabling_source_policy(
    monkeypatch, capsys
) -> None:
    """reaper 只处理本地 checkpoint；无需 provider、对象存储或 EOD source policy 开关。"""
    container = FakeContainer()

    class FakeRepository:
        """返回固定回收数量，隔离 PostgreSQL 事务实现。"""

        def __init__(self, database: object) -> None:
            """断言入口只注入 canonical 数据库。"""
            assert database is container.database

        def requeue_expired_leases(self, *, now: object) -> int:
            """确认入口提供时区时间后返回模拟回收数。"""
            assert getattr(now, "tzinfo", None) is not None
            return 2

    monkeypatch.setattr(sector_eod_reaper, "load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(sector_eod_reaper, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sector_eod_reaper, "build_container", lambda _settings: container)
    monkeypatch.setattr(sector_eod_reaper, "SqlAlchemySectorEodRepository", FakeRepository)

    assert sector_eod_reaper.main([]) == 0

    assert json.loads(capsys.readouterr().out) == {"requeued": 2}
    assert container.closed is True
