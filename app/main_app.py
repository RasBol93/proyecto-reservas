# app/main_app.py

from fastapi import FastAPI

from app.config import APP_NAME, APP_VERSION
from app.api_routes import router as api_router
from app.telegram_webhook import router as telegram_router
from app.admin_diag import router as admin_diag_router


def create_app() -> FastAPI:
    app = FastAPI(
        title=APP_NAME,
        version=APP_VERSION,
    )

    # -------------------------------
    # Routers principales
    # -------------------------------
    app.include_router(api_router)
    app.include_router(telegram_router)

    # 🔐 Router de diagnóstico admin (nuevo)
    app.include_router(admin_diag_router)

    # -------------------------------
    # Health & Root
    # -------------------------------
    @app.get("/")
    def root():
        return {
            "ok": True,
            "service": APP_NAME,
            "version": APP_VERSION,
        }

    @app.get("/health")
    def health():
        return {
            "ok": True,
            "status": "healthy",
            "service": APP_NAME,
        }

    return app


app = create_app()
