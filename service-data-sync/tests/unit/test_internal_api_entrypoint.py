"""内部 API 进程入口的配置、日志和 Uvicorn 组合回归测试。"""

from __future__ import annotations

from types import SimpleNamespace

from service_data_sync.entrypoints import internal_api


def test_main_starts_uvicorn_with_validated_listener_settings(monkeypatch) -> None:
    """入口应使用配置监听地址并将构造后的应用交给 Uvicorn。"""
    settings = SimpleNamespace(internal_api_host="127.0.0.1", internal_api_port=8000)
    captured: dict[str, object] = {}

    def fake_run(application: object, *, host: str, port: int) -> None:
        """记录 Uvicorn 接收的应用和监听参数，不启动真实端口。"""
        captured.update({"application": application, "host": host, "port": port})

    monkeypatch.setattr(internal_api, "load_settings", lambda: settings)
    monkeypatch.setattr(internal_api, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(internal_api, "create_app", lambda **_kwargs: "application")
    monkeypatch.setattr(internal_api.uvicorn, "run", fake_run)

    assert internal_api.main() == 0
    assert captured == {"application": "application", "host": "127.0.0.1", "port": 8000}
