"""数据运维控制面 `Celery` worker 的注册与调度测试。"""

from __future__ import annotations

from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.messaging.celery_app import create_worker_app


def test_worker_registers_only_data_operations_tasks(configured_environment: None) -> None:
    """worker 只能注册 command dispatcher、reaper、健康检查和持久化计划 tick。"""
    del configured_environment
    app = create_worker_app(load_settings())
    custom_task_names = {
        task_name for task_name in app.tasks if task_name.startswith("service_data_sync.")
    }
    assert custom_task_names == {
        "service_data_sync.data_operations.dispatch",
        "service_data_sync.data_operations.reap",
        "service_data_sync.data_operations.health_dispatch",
        "service_data_sync.data_operations.scheduler_tick",
    }
    assert app.conf.task_ignore_result is True
    assert app.conf.result_backend is None
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True


def test_worker_beat_only_wakes_data_operations_control_plane(configured_environment: None) -> None:
    """beat 不得调度旧同步任务；所有业务频率保存在数据库自动计划中。"""
    del configured_environment
    app = create_worker_app(load_settings())
    assert set(app.conf.beat_schedule) == {
        "data-operations-dispatch",
        "data-operations-reap",
        "data-operations-health-dispatch",
        "data-operations-scheduler-tick",
    }
    assert all(
        task["task"].startswith("service_data_sync.data_operations.")
        for task in app.conf.beat_schedule.values()
    )
