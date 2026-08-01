"""验证东财传输兼容层只修正获准域名。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import requests

from service_data_sync.infrastructure.providers.akshare import eastmoney_http

_MONEY_FLOW_DAYKLINE_PARAMETERS = {
    "lmt": "0",
    "klt": "101",
    "secid": "1.600519",
    "fields1": "f1,f2,f3,f7",
    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
    "ut": "b2884a393a59ad64002292a3e90d46a5",
    "_": 1785572725481,
}


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
        captured.update(
            method=method,
            url=url,
            headers=kwargs.get("headers"),
            timeout=kwargs.get("timeout"),
        )
        return requests.Response()

    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setattr(eastmoney_http, "_installed", False)

    eastmoney_http.install_eastmoney_request_compatibility(request_timeout_seconds=17)
    requests.Session().get("https://17.push2.eastmoney.com/api/qt/clist/get", timeout=None)

    headers = cast(Mapping[str, str], captured["headers"])
    assert captured["method"] == "GET"
    assert captured["url"] == "https://17.push2delay.eastmoney.com/api/qt/clist/get"
    assert headers["Referer"] == "https://quote.eastmoney.com/"
    assert str(headers["User-Agent"]).startswith("Mozilla/5.0")
    assert captured["timeout"] == 17


def test_compatibility_rewrites_only_frozen_money_flow_daykline_request(
    monkeypatch: Any,
) -> None:
    """实测断连的资金流日线保留参数、调用方头和显式超时后才可切换节点。"""
    captured: dict[str, object] = {}

    def fake_request(
        _session: requests.Session,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        """捕获经受限路径改写后仍应原样透传的请求细节。"""
        captured.update(
            method=method,
            url=url,
            headers=kwargs.get("headers"),
            parameters=kwargs.get("params"),
            timeout=kwargs.get("timeout"),
        )
        return requests.Response()

    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setattr(eastmoney_http, "_installed", False)

    eastmoney_http.install_eastmoney_request_compatibility(request_timeout_seconds=17)
    requests.Session().get(
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
        params=_MONEY_FLOW_DAYKLINE_PARAMETERS,
        headers={"X-Request-Id": "probe"},
        timeout=9,
    )

    headers = cast(Mapping[str, str], captured["headers"])
    assert captured["method"] == "GET"
    assert captured["url"] == "https://push2delay.eastmoney.com/api/qt/stock/fflow/daykline/get"
    assert captured["parameters"] == _MONEY_FLOW_DAYKLINE_PARAMETERS
    assert headers["X-Request-Id"] == "probe"
    assert headers["Referer"] == "https://quote.eastmoney.com/"
    assert captured["timeout"] == 9


def test_compatibility_does_not_rewrite_push2his_period_bar_request(monkeypatch: Any) -> None:
    """同一主机的周月 K 线即使参数相近也必须保持原节点，避免 delay 空 K 线。"""
    captured: dict[str, object] = {}

    def fake_request(
        _session: requests.Session,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        """记录非资金流路径最终目标，确认兼容层不越界。"""
        captured.update(method=method, url=url, parameters=kwargs.get("params"))
        return requests.Response()

    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setattr(eastmoney_http, "_installed", False)

    eastmoney_http.install_eastmoney_request_compatibility(request_timeout_seconds=17)
    requests.Session().get(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        params=_MONEY_FLOW_DAYKLINE_PARAMETERS,
    )

    assert captured == {
        "method": "GET",
        "url": "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        "parameters": _MONEY_FLOW_DAYKLINE_PARAMETERS,
    }


def test_compatibility_bounds_only_frozen_ths_data_host_without_mutation(
    monkeypatch: Any,
) -> None:
    """同花顺 SDK 缺少每页超时；兼容层只能补预算，不能改动态认证头或请求路径。"""
    captured: dict[str, object] = {}

    def fake_request(
        _session: requests.Session,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        """记录同花顺请求最终参数，验证其业务语义未被兼容层改变。"""
        captured.update(
            method=method,
            url=url,
            headers=kwargs.get("headers"),
            parameters=kwargs.get("params"),
            timeout=kwargs.get("timeout"),
        )
        return requests.Response()

    headers = {"hexin-v": "dynamic-token", "X-Requested-With": "XMLHttpRequest"}
    parameters = {"page": "1"}
    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setattr(eastmoney_http, "_installed", False)

    eastmoney_http.install_eastmoney_request_compatibility(request_timeout_seconds=17)
    requests.Session().get(
        "http://data.10jqka.com.cn/funds/ggzjl/field/zdf/order/desc/page/1/ajax/1/free/1/",
        params=parameters,
        headers=headers,
    )

    assert captured == {
        "method": "GET",
        "url": "http://data.10jqka.com.cn/funds/ggzjl/field/zdf/order/desc/page/1/ajax/1/free/1/",
        "headers": headers,
        "parameters": parameters,
        "timeout": 17,
    }


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
        captured.update(
            method=method,
            url=url,
            headers=kwargs.get("headers"),
            parameters=kwargs.get("params"),
            timeout=kwargs.get("timeout"),
        )
        return requests.Response()

    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setattr(eastmoney_http, "_installed", False)

    eastmoney_http.install_eastmoney_request_compatibility(request_timeout_seconds=17)
    requests.Session().get("https://www.csindex.com.cn/example", headers={"X-Test": "1"})

    assert captured == {
        "method": "GET",
        "url": "https://www.csindex.com.cn/example",
        "headers": {"X-Test": "1"},
        "parameters": None,
        "timeout": None,
    }
