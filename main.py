import os
from fastapi import FastAPI, Request

app = FastAPI()

@app.get("/")
def root():
    return {"status": "ok", "message": "proyecto-reservas activo"}

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    # Por ahora solo confirmamos recepción
    return {"ok": True, "received": True}
