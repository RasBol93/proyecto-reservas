# app/main_app.py

import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import APP_NAME, APP_VERSION
from app.api_routes import router as api_router
from app.telegram_webhook import router as telegram_router
from app.admin_diag import router as admin_diag_router


# -------------------------------------------------
# Logging básico (Render-friendly)
# -------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(
        title=APP_NAME,
        version=APP_VERSION,
    )

    # -------------------------------------------------
    # Middlewares (preparado para escalar)
    # -------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # ajustar en producción si necesitas restringir
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -------------------------------------------------
    # Routers
    # -------------------------------------------------
    app.include_router(api_router)
    app.include_router(telegram_router)
    app.include_router(admin_diag_router)

    # -------------------------------------------------
    # Root público
    # -------------------------------------------------
    @app.get("/", tags=["system"])
    def root():
        return {
            "ok": True,
            "service": APP_NAME,
            "version": APP_VERSION,
        }

    # -------------------------------------------------
    # Health público simple (Render)
    # -------------------------------------------------
    @app.get("/health", tags=["system"])
    def health():
        return {
            "ok": True,
            "status": "healthy",
            "service": APP_NAME,
        }

    # -------------------------------------------------
    # Health interno detallado (futuro monitoreo)
    # -------------------------------------------------
    @app.get("/healthcheck", tags=["system"])
    def healthcheck():
        return {
            "ok": True,
            "service": APP_NAME,
            "version": APP_VERSION,
            "status": "running",
        }

    # -------------------------------------------------
    # Eventos lifecycle
    # -------------------------------------------------
    @app.on_event("startup")
    async def startup_event():
        logger.info(f"🚀 {APP_NAME} v{APP_VERSION} started successfully")

    @app.on_event("shutdown")
    async def shutdown_event():
        logger.info(f"🛑 {APP_NAME} shutting down")

    return app


app = create_app()
