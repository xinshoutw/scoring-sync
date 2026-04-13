from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from net_grading.config import get_settings
from net_grading.db.engine import dispose_engine

_STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="net-grading",
        version="0.1.0",
        debug=settings.app_env != "production",
        lifespan=lifespan,
    )

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "env": settings.app_env})

    return app


app = create_app()
