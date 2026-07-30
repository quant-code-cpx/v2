"""验证东财传输兼容层只修正获准域名。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import requests

from service_data_sync.infrastructure.providers.akshare import eastmoney_http


def test_compatibility_rewrites_push2_and_adds_required_headers(monkeypatch: Any) -> None:
    """旧节点请求应改到稳定节点，并补齐东财当前要求的浏览器来源头。"""
    captured: dict[str, object] = {}

    def fake_request(
        _session: requests.Session,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        """记录兼容层交给底层传输的最终参数。"""
        captured.update(method=method, url=url, headers=kwargs.get("headers"))
        return requests.Response()

    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setattr(eastmoney_http, "_installed", False)

    eastmoney_http.install_eastmoney_request_compatibility()
    requests.Session().get("https://17.push2.eastmoney.com/api/qt/clist/get")

    headers = cast(Mapping[str, str], captured["headers"])
    assert captured["method"] == "GET"
    assert captured["url"] == "https://17.push2delay.eastmoney.com/api/qt/clist/get"
    assert headers["Referer"] == "https://quote.eastmoney.com/"
    assert str(headers["User-Agent"]).startswith("Mozilla/5.0")


def test_compatibility_leaves_other_provider_requests_untouched(monkeypatch: Any) -> None:
    """非东财 URL 不得被改写，也不得收到东财专用请求头。"""
    captured: dict[str, object] = {}

    def fake_request(
        _session: requests.Session,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        """记录非东财请求最终收到的参数。"""
        captured.update(method=method, url=url, headers=kwargs.get("headers"))
        return requests.Response()

    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setattr(eastmoney_http, "_installed", False)

    eastmoney_http.install_eastmoney_request_compatibility()
    requests.Session().get("https://www.csindex.com.cn/example", headers={"X-Test": "1"})

    assert captured == {
        "method": "GET",
        "url": "https://www.csindex.com.cn/example",
        "headers": {"X-Test": "1"},
    }
