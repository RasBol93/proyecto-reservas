# app/main_app.py

from fastapi import FastAPI

from app.config import APP_NAME
from app.api_routes import router as api_router
from app.telegram_webhook import router as telegram_router


def create_app() -> FastAPI:
    app = FastAPI(title=APP_NAME, version="2.0.0-modular")

    # API routes (Swagger / ManyChat / etc.)
    app.include_router(api_router)

    # Telegram webhook routes
    app.include_router(telegram_router)

    @app.get("/")
    def root():
        return {"ok": True, "service": APP_NAME}

    return app


app = create_app()
