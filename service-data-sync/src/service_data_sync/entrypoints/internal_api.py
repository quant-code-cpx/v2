"""启动 service-data-sync 的受认证内部只读 HTTP 服务。

服务只向受信任的内部调用方投影已发布的 canonical 数据，认证、条件请求和错误映射由
接口层统一处理；此进程不暴露原始证据、供应商字段、写接口或同步任务控制能力。
"""

from __future__ import annotations

import uvicorn

from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.interfaces.internal_sector_api import create_app


def main() -> int:
    """加载受校验配置后启动内部只读 API；监听地址仅由部署配置决定。

    路由本身仍执行 bearer token 校验并只投影已经发布的数据；此入口不提供管理、
    同步或来源抓取能力，避免内部网络监听被误解为无需授权的运维接口。
    """
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
