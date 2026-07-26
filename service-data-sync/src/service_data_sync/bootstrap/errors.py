"""配置与本地依赖故障的安全领域异常。"""

from __future__ import annotations


class ConfigurationError(RuntimeError):
    """配置缺失或无效；详细原因仅保留在异常链中，默认不输出。"""


class DependencyUnavailable(RuntimeError):
    """无法安全访问某项本地基础设施依赖。"""

    def __init__(self, dependency: str, operation: str) -> None:
        """构造便于运维定位、但不泄漏连接细节的依赖故障信息。"""
        super().__init__(f"{dependency} unavailable during {operation}")
        self.dependency = dependency
        self.operation = operation
