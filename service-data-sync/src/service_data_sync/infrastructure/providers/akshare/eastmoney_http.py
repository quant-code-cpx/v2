"""为 `AKShare` 的东财与同花顺请求安装受限的传输兼容层。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import urlsplit

import requests
from requests.structures import CaseInsensitiveDict

_EASTMONEY_HOST_SUFFIX = ".eastmoney.com"
_PUSH2_HOST = "push2.eastmoney.com"
_PUSH2_HIS_HOST = "push2his.eastmoney.com"
_PUSH2_DELAY_HOST = "push2delay.eastmoney.com"
_THS_DATA_HOST = "data.10jqka.com.cn"
_MONEY_FLOW_DAYKLINE_PATH = "/api/qt/stock/fflow/daykline/get"
_MONEY_FLOW_DAYKLINE_REQUIRED_PARAMETERS = {
    "lmt": "0",
    "klt": "101",
    "fields1": "f1,f2,f3,f7",
    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
}
_MONEY_FLOW_DAYKLINE_ALLOWED_PARAMETERS = frozenset(
    {
        "lmt",
        "klt",
        "secid",
        "secid2",
        "fields1",
        "fields2",
        "ut",
        "_",
    }
)
_REFERER = "https://quote.eastmoney.com/"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
_installed = False


def install_eastmoney_request_compatibility(
    *, request_timeout_seconds: float = _DEFAULT_REQUEST_TIMEOUT_SECONDS
) -> None:
    """为东财域名补齐来源头，并为同花顺固定数据主机注入请求超时。

    兼容层只改写东财受验证的旧 `push2` 请求；同花顺请求不改 URL、query、headers、响应或
    重试语义。安装是进程级且幂等的，适用于 `AKShare` 内部每次新建 `requests.Session` 的
    调用方式。两者 SDK 都是阻塞调用，必须在传输层注入单请求超时，避免上层协程超时后遗留
    仍在运行的线程。
    """
    if request_timeout_seconds <= 0:
        raise ValueError("eastmoney request timeout must be positive")
    global _installed
    if _installed:
        return

    original_request = requests.Session.request

    def compatible_request(
        session: requests.Session,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        """在请求真正发出前仅修正冻结主机的传输参数。"""
        compatible_url = _compatible_url(url, parameters=kwargs.get("params"))
        if _is_eastmoney_url(compatible_url):
            headers = CaseInsensitiveDict(kwargs.get("headers") or {})
            headers.setdefault("User-Agent", _USER_AGENT)
            headers.setdefault("Referer", _REFERER)
            kwargs["headers"] = headers
            # 只在 SDK 未显式提供预算时注入，保证单页网络 I/O 可真实中断，而不是仅取消等待它的协程。
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = request_timeout_seconds
        elif _is_ths_data_url(compatible_url) and kwargs.get("timeout") is None:
            # 固定版本同花顺 SDK 未向 `requests.get` 传入 timeout；不改变其动态 `hexin-v` 等认证头。
            kwargs["timeout"] = request_timeout_seconds
        return original_request(session, method, compatible_url, **kwargs)

    cast(Any, requests.Session).request = compatible_request
    _installed = True


def _compatible_url(url: str, *, parameters: object | None = None) -> str:
    """仅把实测断连的资金流日线请求提前路由到稳定延迟节点。"""
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if _is_money_flow_daykline_request(
        hostname=hostname,
        path=parsed.path,
        parameters=parameters,
    ):
        return url.replace(_PUSH2_HIS_HOST, _PUSH2_DELAY_HOST, 1)
    if hostname is None or not (hostname == _PUSH2_HOST or hostname.endswith(f".{_PUSH2_HOST}")):
        return url
    return url.replace(hostname, hostname.replace(_PUSH2_HOST, _PUSH2_DELAY_HOST), 1)


def _is_money_flow_daykline_request(
    *,
    hostname: str | None,
    path: str,
    parameters: object | None,
) -> bool:
    """识别固定 AKShare 资金流日线签名，避免误改同主机的 K 线请求。"""
    if hostname != _PUSH2_HIS_HOST or path != _MONEY_FLOW_DAYKLINE_PATH:
        return False
    if not isinstance(parameters, Mapping):
        return False
    if not set(parameters).issubset(_MONEY_FLOW_DAYKLINE_ALLOWED_PARAMETERS):
        return False
    return all(
        parameters.get(key) == value
        for key, value in _MONEY_FLOW_DAYKLINE_REQUIRED_PARAMETERS.items()
    ) and isinstance(parameters.get("secid"), str)


def _is_eastmoney_url(url: str) -> bool:
    """判断请求是否严格属于东财主域或其子域。"""
    hostname = urlsplit(url).hostname
    return hostname == "eastmoney.com" or bool(
        hostname and hostname.endswith(_EASTMONEY_HOST_SUFFIX)
    )


def _is_ths_data_url(url: str) -> bool:
    """判断请求是否精确属于固定 `AKShare` 同花顺数据主机。"""
    return urlsplit(url).hostname == _THS_DATA_HOST
