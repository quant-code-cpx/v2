"""为 `AKShare` 的东财请求安装受限的传输兼容层。"""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import urlsplit

import requests
from requests.structures import CaseInsensitiveDict

_EASTMONEY_HOST_SUFFIX = ".eastmoney.com"
_PUSH2_HOST = "push2.eastmoney.com"
_PUSH2_DELAY_HOST = "push2delay.eastmoney.com"
_REFERER = "https://quote.eastmoney.com/"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
_installed = False


def install_eastmoney_request_compatibility() -> None:
    """为东财域名补齐来源头，并绕开会直接断连的旧 `push2` 入口。

    兼容层只改写东财域名，其他供应商请求保持原样。安装是进程级且幂等的，适用于
    `AKShare` 内部每次新建 `requests.Session` 的调用方式。
    """
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
        """在请求真正发出前仅修正东财传输参数。"""
        compatible_url = _compatible_url(url)
        if _is_eastmoney_url(compatible_url):
            headers = CaseInsensitiveDict(kwargs.get("headers") or {})
            headers.setdefault("User-Agent", _USER_AGENT)
            headers.setdefault("Referer", _REFERER)
            kwargs["headers"] = headers
        return original_request(session, method, compatible_url, **kwargs)

    cast(Any, requests.Session).request = compatible_request
    _installed = True


def _compatible_url(url: str) -> str:
    """把东财主动重定向到的稳定延迟节点提前设为请求目标。"""
    hostname = urlsplit(url).hostname
    if hostname is None or not (hostname == _PUSH2_HOST or hostname.endswith(f".{_PUSH2_HOST}")):
        return url
    return url.replace(hostname, hostname.replace(_PUSH2_HOST, _PUSH2_DELAY_HOST), 1)


def _is_eastmoney_url(url: str) -> bool:
    """判断请求是否严格属于东财主域或其子域。"""
    hostname = urlsplit(url).hostname
    return hostname == "eastmoney.com" or bool(
        hostname and hostname.endswith(_EASTMONEY_HOST_SUFFIX)
    )
