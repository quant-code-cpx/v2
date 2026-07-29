"""配置与本地基础设施故障的安全领域异常。

异常消息刻意只表达故障类别与操作，不带 URL、凭据、数据库名或第三方响应；入口层可据此
输出稳定诊断结果，而受控日志仍能通过异常链保留更细的技术上下文。
"""

from __future__ import annotations


class ConfigurationError(RuntimeError):
    """表示配置缺失或无效；详细字段值只保留在异常链中，默认不会对外输出。"""


class DependencyUnavailable(RuntimeError):
    """表示无法安全访问某项本地基础设施依赖，供诊断和任务统一分类。"""

    def __init__(self, dependency: str, operation: str) -> None:
        """构造便于运维定位、但不泄漏 endpoint、连接串或底层异常文本的故障信息。"""
        super().__init__(f"{dependency} unavailable during {operation}")
        self.dependency = dependency
        self.operation = operation
