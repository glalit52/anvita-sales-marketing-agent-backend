import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging, org_id_var, request_id_var
from app.core.querycase import CamelCaseQueryMiddleware
from app.db.session import dispose_engine, get_engine

log = logging.getLogger("anvita.api")


async def check_db_role() -> None:
    """Tenant isolation relies on RLS, which superusers and BYPASSRLS roles silently skip."""
    async with get_engine().connect() as conn:
        row = (
            await conn.execute(text("select rolsuper, rolbypassrls from pg_roles where rolname = current_user"))
        ).one()
    if row.rolsuper or row.rolbypassrls:
        msg = "Database role bypasses row-level security; connect as a regular (non-superuser) role"
        if get_settings().is_production:
            raise RuntimeError(msg)
        log.warning(msg)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    s = get_settings()
    s.validate_for_production()
    configure_logging(s.log_level)
    await check_db_role()
    yield
    await dispose_engine()


def _error(status: int, code: str, message: str, details: object = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "details": details}})


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="Anvita API",
        version="0.1.0",
        description="Backend for Anvita, the AI sales & marketing agent.",
        lifespan=lifespan,
        docs_url=None if s.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if s.is_production else "/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-request-id"],
    )
    app.add_middleware(CamelCaseQueryMiddleware)

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # noqa: ANN001, ANN202
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request_id_var.set(rid)
        org_id_var.set(None)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error", extra={"data": {"path": request.url.path}})
            response = _error(500, "internal_error", "Something went wrong")
        elapsed = int((time.perf_counter() - started) * 1000)
        response.headers["x-request-id"] = rid
        log.info(
            "request",
            extra={
                "data": {
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "ms": elapsed,
                }
            },
        )
        return response

    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError) -> JSONResponse:
        return _error(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return _error(422, "validation_error", "Request validation failed", details)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def readyz() -> JSONResponse:
        try:
            async with get_engine().connect() as conn:
                await conn.execute(text("select 1"))
        except Exception:  # noqa: BLE001
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return JSONResponse(content={"status": "ok"})

    app.include_router(api_router, prefix=s.api_prefix)
    return app


app = create_app()
