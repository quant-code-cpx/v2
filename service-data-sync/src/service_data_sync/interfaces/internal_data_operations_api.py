"""数据运维控制面 0022 内部 POST 路由。

接口层只完成 bearer 身份、请求 ID、HTTP 语义和安全投影；命令、slot、fencing、健康和
计划状态转换均委托 PostgreSQL 权威 `DataOperationsControlPlane`。任何 Provider 原文、凭据、
原始 URI、真实 checkpoint 位置和堆栈都不会从这里返回。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    OperationProblem,
)


def register_data_operations_routes(
    app: FastAPI,
    *,
    control_plane: DataOperationsControlPlane,
    require_read_service_bearer: Callable[..., None],
    require_operations_service_bearer: Callable[..., None],
) -> None:
    """注册合同 0022 的十八条 POST 路由，并按读写类别强制不同服务身份。"""

    @app.exception_handler(OperationProblem)
    async def render_operation_problem(request: Request, error: OperationProblem) -> JSONResponse:
        """把应用层可预期错误投影为不泄漏内部细节的 Problem Details。"""
        request_id = _request_id(request)
        headers = {"X-Request-Id": request_id, "Cache-Control": "no-store"}
        if error.status == 503:
            headers["Retry-After"] = "5"
        return JSONResponse(
            status_code=error.status,
            content={
                "type": f"https://quant-v2.invalid/problems/{error.code}",
                "title": error.code,
                "status": error.status,
                "detail": error.detail,
                "code": error.code,
                "requestId": request_id,
            },
            media_type="application/problem+json",
            headers=headers,
        )

    @app.post(
        "/internal/v1/data-operations/overview/query",
        dependencies=[Depends(require_read_service_bearer)],
    )
    def query_overview(body: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        """查询数据运维总览；请求体必须是空对象。"""
        if body:
            raise OperationProblem(
                status=400, code="validation-error", detail="Overview request must be empty"
            )
        return control_plane.overview()

    @app.post(
        "/internal/v1/data-operations/datasets/search",
        dependencies=[Depends(require_read_service_bearer)],
    )
    def search_datasets(body: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        """分页检索数据集目录；cursor 只用于本列表。"""
        return control_plane.list_datasets(body)

    @app.post(
        "/internal/v1/data-operations/datasets/detail",
        dependencies=[Depends(require_read_service_bearer)],
    )
    def get_dataset_detail(body: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        """读取一个数据集的来源、能力、运行、发布和健康详情。"""
        dataset_code = body.get("datasetCode")
        if not isinstance(dataset_code, str):
            raise OperationProblem(
                status=400, code="validation-error", detail="datasetCode is invalid"
            )
        return control_plane.dataset_detail(dataset_code)

    @app.post(
        "/internal/v1/data-operations/commands/preflight",
        dependencies=[Depends(require_read_service_bearer)],
    )
    def preflight_command(body: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        """无副作用校验同步目标、模式与范围。"""
        targets = body.get("targets")
        if not isinstance(targets, list):
            raise OperationProblem(status=400, code="validation-error", detail="targets is invalid")
        return control_plane.preflight(targets)

    @app.post(
        "/internal/v1/data-operations/commands/submit",
        status_code=202,
        dependencies=[Depends(require_operations_service_bearer)],
    )
    def submit_command(
        request: Request,
        body: Annotated[dict[str, Any], Body(...)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        """受理同步 command；相同内部幂等键只返回同一命令。"""
        return control_plane.submit_command(
            request=body,
            idempotency_key=_required_key(idempotency_key),
            request_id=_request_id(request),
        )

    @app.post(
        "/internal/v1/data-operations/commands/detail",
        dependencies=[Depends(require_read_service_bearer)],
    )
    def get_command_detail(body: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        """读取 command 与按提交顺序排列的 child runs。"""
        return control_plane.command_detail(_uuid_from_body(control_plane, body, "commandId"))

    @app.post(
        "/internal/v1/data-operations/commands/cancel",
        status_code=202,
        dependencies=[Depends(require_operations_service_bearer)],
    )
    def cancel_command(
        request: Request,
        body: Annotated[dict[str, Any], Body(...)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        """取消 command 或 run；过晚取消仅记录 FAILED 动作事件。"""
        return control_plane.cancel_command(
            request=body,
            idempotency_key=_required_key(idempotency_key),
            request_id=_request_id(request),
        )

    @app.post(
        "/internal/v1/data-operations/commands/retry",
        status_code=202,
        dependencies=[Depends(require_operations_service_bearer)],
    )
    def retry_command(
        request: Request,
        body: Annotated[dict[str, Any], Body(...)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        """从可重试失败、部分成功或中断目标创建新 command。"""
        return control_plane.retry_command(
            request=body,
            idempotency_key=_required_key(idempotency_key),
            request_id=_request_id(request),
        )

    @app.post(
        "/internal/v1/data-operations/runs/search",
        dependencies=[Depends(require_read_service_bearer)],
    )
    def search_runs(body: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        """分页检索同步 run 与队列状态。"""
        return control_plane.list_runs(body)

    @app.post(
        "/internal/v1/data-operations/runs/detail",
        dependencies=[Depends(require_read_service_bearer)],
    )
    def get_run_detail(body: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        """读取 run、分区、质量门和独立 timeline cursor 页。"""
        return control_plane.run_detail(body)

    @app.post(
        "/internal/v1/data-operations/health/evaluations/search",
        dependencies=[Depends(require_read_service_bearer)],
    )
    def search_health_evaluations(body: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        """分页检索不可变发布后健康评估摘要。"""
        return control_plane.list_health_evaluations(body)

    @app.post(
        "/internal/v1/data-operations/health/evaluations/detail",
        dependencies=[Depends(require_read_service_bearer)],
    )
    def get_health_evaluation_detail(body: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        """读取不可变评估事实与当前开放问题投影。"""
        return control_plane.health_evaluation_detail(body)

    @app.post(
        "/internal/v1/data-operations/health/checks/submit",
        status_code=202,
        dependencies=[Depends(require_operations_service_bearer)],
    )
    def submit_health_check(
        request: Request,
        body: Annotated[dict[str, Any], Body(...)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        """受理独立主动健康检查批次。"""
        return control_plane.submit_health_check(
            request=body,
            idempotency_key=_required_key(idempotency_key),
            request_id=_request_id(request),
        )

    @app.post(
        "/internal/v1/data-operations/health/checks/detail",
        dependencies=[Depends(require_read_service_bearer)],
    )
    def get_health_check_detail(body: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        """读取健康检查和按原 target 顺序返回的逐项结果。"""
        return control_plane.health_check_detail(
            _uuid_from_body(control_plane, body, "healthCheckId")
        )

    @app.post(
        "/internal/v1/data-operations/schedules/search",
        dependencies=[Depends(require_read_service_bearer)],
    )
    def search_schedules(body: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        """分页检索数据库持久化的结构化自动计划。"""
        return control_plane.list_schedules(body)

    @app.post(
        "/internal/v1/data-operations/schedules/upsert",
        dependencies=[Depends(require_operations_service_bearer)],
    )
    def upsert_schedule(
        request: Request,
        body: Annotated[dict[str, Any], Body(...)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        """创建或使用 expectedVersion 更新数据集唯一计划。"""
        return control_plane.upsert_schedule(
            request=body,
            idempotency_key=_required_key(idempotency_key),
            request_id=_request_id(request),
        )

    @app.post(
        "/internal/v1/data-operations/schedules/set-enabled",
        dependencies=[Depends(require_operations_service_bearer)],
    )
    def set_schedule_enabled(
        request: Request,
        body: Annotated[dict[str, Any], Body(...)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        """按版本启停计划，不允许无条件覆盖并发编辑。"""
        return control_plane.set_schedule_enabled(
            request=body,
            idempotency_key=_required_key(idempotency_key),
            request_id=_request_id(request),
        )

    @app.post(
        "/internal/v1/data-operations/events/search",
        dependencies=[Depends(require_read_service_bearer)],
    )
    def search_events(body: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        """分页检索不可变控制面运维事件。"""
        return control_plane.list_events(body)


def _required_key(value: str | None) -> str:
    """把缺失 Idempotency-Key 映射为合同参数问题。"""
    if value is None:
        raise OperationProblem(
            status=400, code="validation-error", detail="Idempotency key is required"
        )
    return value


def _request_id(request: Request) -> str:
    """返回调用方提供的受限请求 ID 或生成安全关联标识。"""
    value = request.headers.get("X-Request-Id")
    if value is not None and 0 < len(value.strip()) <= 128:
        return value.strip()
    return f"data-operations-{id(request):x}"


def _uuid_from_body(control_plane: DataOperationsControlPlane, body: dict[str, Any], field: str):
    """复用控制面 UUID 校验，避免接口层建立不一致的错误语义。"""
    return control_plane._uuid_field(body, field)
