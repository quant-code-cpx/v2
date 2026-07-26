"""启动 service-data-sync 的受认证内部只读 HTTP 服务。"""

from __future__ import annotations

import uvicorn

from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.interfaces.internal_sector_api import create_app


def main() -> int:
    """加载受校验配置后启动内部 API；生产监听地址仅由部署配置决定。"""
    settings = load_settings()
    configure_logging(settings, process_role="internal-api")
    uvicorn.run(
        create_app(settings=settings),
        host=settings.internal_api_host,
        port=settings.internal_api_port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
