"""Point d'entrée FastAPI + uvicorn — PanoForge (127.0.0.1:8360)."""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import router as api_router

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="PanoForge", docs_url="/api/docs", redoc_url=None)
app.include_router(api_router)

if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def main() -> None:
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8360, reload=False)


if __name__ == "__main__":
    main()
