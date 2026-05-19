# app/main_app.py

import logging
import uuid
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import APP_NAME, APP_VERSION
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

ALLOWED_CORS_ORIGINS = [
    "https://app-pedidos-rho-eight.vercel.app",
    "https://app-pedidos-git-main-rasbol93s-projects.vercel.app",
    "http://localhost:3000",
    "http://localhost:3001",
]


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
