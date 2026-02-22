# app/main_app.py

from fastapi import FastAPI

from app.config import APP_NAME, APP_VERSION
from app.api_routes import router as api_router
from app.telegram_webhook import router as telegram_router


def create_app() -> FastAPI:
    app = FastAPI(title=APP_NAME, version=APP_VERSION)

    app.include_router(api_router)
    app.include_router(telegram_router)

    @app.get("/")
    def root():
        return {"ok": True, "service": APP_NAME, "version": APP_VERSION}

    return app


app = create_app()
