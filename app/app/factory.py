# app/app_factory.py
from fastapi import FastAPI

from app.config import APP_NAME, APP_VERSION
from app.api_routes import router as api_router
from app.telegram_webhook import router as telegram_router


def create_app() -> FastAPI:
    app = FastAPI(title=APP_NAME, version=APP_VERSION)

    # Rutas API (menu, orders, admin, etc.)
    app.include_router(api_router)

    # Webhook Telegram (admin + client)
    app.include_router(telegram_router)

    return app
