import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after: int | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after
        super().__init__(message)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def business_error(request: Request, exc: ApiError) -> JSONResponse:
        error = {"code": exc.code, "message": exc.message}
        headers = {"Cache-Control": "no-store"}
        if exc.retry_after is not None:
            headers["Retry-After"] = str(exc.retry_after)
            error["retry_after"] = exc.retry_after
        return JSONResponse(status_code=exc.status, content={"error": error}, headers=headers)

    @app.exception_handler(RedisError)
    async def redis_error(request: Request, exc: RedisError) -> JSONResponse:
        logger.warning("Redis request failed: %s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "SESSION_UNAVAILABLE",
                    "message": "登录服务暂不可用，请检查 Redis 连接",
                }
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": f"HTTP_{exc.status_code}", "message": str(exc.detail)}},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Do not echo input values: future authentication requests may contain secrets.
        details = [
            {"field": ".".join(map(str, e["loc"])), "message": e["msg"], "type": e["type"]}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "请求参数不合法",
                    "details": details,
                }
            },
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.warning("Database request failed: %s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "DATABASE_UNAVAILABLE",
                    "message": "数据库暂不可用，请检查连接配置和表结构",
                }
            },
        )

    @app.exception_handler(TimeoutError)
    async def database_timeout(request: Request, exc: TimeoutError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": {"code": "DATABASE_TIMEOUT", "message": "数据库请求超时，请稍后重试"}
            },
        )
