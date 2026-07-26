from __future__ import annotations


class ConfigurationError(RuntimeError):
    """Settings are missing or invalid. Details remain chained, never rendered by default."""


class DependencyUnavailable(RuntimeError):
    """A local infrastructure dependency cannot be reached safely."""

    def __init__(self, dependency: str, operation: str) -> None:
        super().__init__(f"{dependency} unavailable during {operation}")
        self.dependency = dependency
        self.operation = operation
