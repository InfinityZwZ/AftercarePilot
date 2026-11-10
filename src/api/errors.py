from uuid import uuid4

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from src.domain.exceptions import AppError


def register_errors(app):
    @app.middleware("http")
    async def tracing(request, call_next):
        request.state.request_trace_id = "req-" + uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-Trace-Id"] = request.state.request_trace_id
        return response

    def result(request, code, message, status, retryable=False, details=None):
        body = dict(error={"code": code, "message": message, "retryable": retryable, "details": details or {}},
                    request_trace_id=getattr(request.state, "request_trace_id", "unknown"))
        if status == 202:
            body.update(status="unknown", next_action="query_execution")
        return JSONResponse(body, status_code=status)

    @app.exception_handler(AppError)
    async def application_error(request, exc):
        return result(request, exc.code, exc.message, exc.status, exc.retryable, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        fields = [{"loc": list(error["loc"]), "type": error["type"]} for error in exc.errors()]
        return result(request, "VALIDATION_ERROR", "Request does not match the schema", 422, details={"fields": fields})

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return result(request, "DATABASE_UNAVAILABLE", "Database operation could not complete", 503, retryable=True)

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        return result(request, "INTERNAL_ERROR", "Request could not complete", 500)
