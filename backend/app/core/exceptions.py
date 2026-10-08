"""统一异常体系与全局异常处理。

所有对外错误均返回 {code, message, data} 结构。
"""
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    """业务异常基类。"""

    code: int = 50000
    message: str = "Internal server error"
    http_status: int = 500

    def __init__(self, message: str | None = None, code: int | None = None) -> None:
        if message is not None:
            self.message = message
        if code is not None:
            self.code = code
        super().__init__(self.message)


class NotFoundError(AppError):
    code = 40400
    http_status = 404
    message = "Resource not found"


class BadRequestError(AppError):
    code = 40000
    http_status = 400
    message = "Bad request"


class ValidationError(AppError):
    code = 42200
    http_status = 422
    message = "Validation failed"


class UnauthorizedError(AppError):
    code = 40100
    http_status = 401
    message = "Unauthorized"


class ForbiddenError(AppError):
    code = 40300
    http_status = 403
    message = "Forbidden"


class LLMError(AppError):
    """LLM / AI 调用失败。"""

    code = 50001
    http_status = 502
    message = "AI service error"


class TaskError(AppError):
    code = 50002
    http_status = 500
    message = "Task execution error"


def _payload(code: int, message: str, data: Any = None) -> dict[str, Any]:
    return {"code": code, "message": message, "data": data}


def register_exception_handlers(app: FastAPI) -> None:
    """在 FastAPI 实例上注册全局异常处理器。"""

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(f"AppError {exc.code}: {exc.message} path={request.url.path}")
        return JSONResponse(
            status_code=exc.http_status,
            content=_payload(exc.code, exc.message),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # 未命中路由 / 其它 HTTP 层错误与业务异常区分编码：业务 404 用 40400
        # （NotFoundError），HTTP 层 404 用 40410，二者不复用同一 code，便于前端与日志区分。
        code = {
            404: 40410,  # 路由未命中
            405: 40500,  # 方法不允许
        }.get(exc.status_code, exc.status_code * 100)
        # 记录访问日志（尤其是路由未命中，排查打错路径／被扫描器试探）
        logger.info(
            f"HTTP {exc.status_code} [{code}] {request.method} {request.url.path} - {exc.detail}"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(code, str(exc.detail)),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []))
        msg = f"参数校验失败: {loc} - {first.get('msg', '')}"
        return JSONResponse(
            status_code=422,
            content=_payload(42200, msg),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(f"Unhandled error: {exc} path={request.url.path}")
        return JSONResponse(
            status_code=500,
            content=_payload(50000, "Internal server error"),
        )
