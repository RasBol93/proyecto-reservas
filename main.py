from fastapi import FastAPI
from app.config import APP_NAME, APP_VERSION
from app.telegram_webhook import router as telegram_router
from app.api_routes import router as api_router

app = FastAPI(title=APP_NAME, version=APP_VERSION)

app.include_router(api_router)
app.include_router(telegram_router)
