# app/main_app.py

import logging
import os
import uuid
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from urllib.parse import urlparse

from app.config import APP_NAME, APP_VERSION, ENV_DASHBOARD_APP_BASE_URL, env_optional
from app.api_routes import router as api_router
from app.telegram_webhook import router as telegram_router
from app.admin_diag import router as admin_diag_router
from app.sheets import start_sheets_request_context, finish_sheets_request_context


# -------------------------
# Logging (hardened)
# -------------------------

def _setup_logging():
    # Evita duplicar handlers si uvicorn ya configuró logging
    root_logger = logging.getLogger()
    if root_logger.handlers:
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


_setup_logging()
logger = logging.getLogger(__name__)


def _normalize_origin(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _build_allowed_cors_origins() -> list[str]:
    allowed: list[str] = []

    for candidate in [
        env_optional("FRONTEND_APP_BASE_URL"),
        env_optional(ENV_DASHBOARD_APP_BASE_URL),
    ]:
        origin = _normalize_origin(candidate)
        if origin and origin not in allowed:
            allowed.append(origin)

    extra_origins_raw = os.getenv("CORS_ALLOWED_ORIGINS", "")
    for part in extra_origins_raw.split(","):
        origin = _normalize_origin(part)
        if origin and origin not in allowed:
            allowed.append(origin)

    for local_origin in [
        "http://localhost:3000",
        "http://localhost:3001",
    ]:
        if local_origin not in allowed:
            allowed.append(local_origin)

    return allowed


ALLOWED_CORS_ORIGINS = _build_allowed_cors_origins()


# -------------------------
# App factory
# -------------------------

def create_app() -> FastAPI:
    app = FastAPI(
        title=APP_NAME,
        version=APP_VERSION,
    )

    # -------------------------
    # CORS
    # -------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def sheets_observability_middleware(request, call_next):
        request_id = (request.headers.get("x-request-id") or "").strip() or f"http_{uuid.uuid4().hex[:12]}"
        tenant_id = (request.query_params.get("tenant_id") or "").strip()

        start_sheets_request_context(
            path=str(request.url.path or "").strip(),
            flow_name=str(request.url.path or "").strip(),
            tenant_id=tenant_id,
            request_id=request_id,
        )

        try:
            response = await call_next(request)
        except Exception as e:
            finish_sheets_request_context(status_code=500, error=str(e))
            raise

        try:
            response.headers["X-Request-ID"] = request_id
        except Exception:
            pass

        finish_sheets_request_context(status_code=int(getattr(response, "status_code", 200) or 200))
        return response

    # -------------------------
    # Routers
    # -------------------------
    app.include_router(api_router)
    app.include_router(telegram_router)
    app.include_router(admin_diag_router)

    # -------------------------
    # System endpoints
    # -------------------------

    @app.get("/", tags=["system"])
    def root():
        return {
            "ok": True,
            "service": APP_NAME,
            "version": APP_VERSION,
        }

    @app.get("/health", tags=["system"])
    def health():
        return {
            "ok": True,
            "status": "healthy",
            "service": APP_NAME,
        }

    # Healthcheck público simple (NO toca secretos)
    @app.get("/healthcheck", tags=["system"])
    def healthcheck():
        return {
            "ok": True,
            "status": "running",
            "service": APP_NAME,
            "version": APP_VERSION,
        }

    # -------------------------
    # Lifecycle
    # -------------------------

    @app.on_event("startup")
    async def startup_event():
        try:
            logger.info(f"{APP_NAME} v{APP_VERSION} started")
        except Exception as e:
            logger.error(f"startup logging failed: {e}")

    @app.on_event("shutdown")
    async def shutdown_event():
        try:
            logger.info(f"{APP_NAME} shutting down")
        except Exception as e:
            logger.error(f"shutdown logging failed: {e}")

    return app


# -------------------------
# App instance
# -------------------------

app = create_app()
