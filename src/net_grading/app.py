import secrets
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from net_grading.config import get_settings
from net_grading.db.engine import dispose_engine
from net_grading.routes import auth as auth_routes
from net_grading.routes import conflicts as conflicts_routes
from net_grading.routes import grading as grading_routes
from net_grading.routes import settings as settings_routes

_STATIC_DIR = Path(__file__).parent / "static"

# 進程啟動唯一 id；瀏覽器 heartbeat 比對 → 偵測重啟
SERVER_INSTANCE_ID = secrets.token_hex(8)


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

    app.include_router(auth_routes.router)
    app.include_router(grading_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(conflicts_routes.router)

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "env": settings.app_env})

    @app.get("/heartbeat")
    async def heartbeat() -> JSONResponse:
        resp = JSONResponse({"id": SERVER_INSTANCE_ID})
        resp.headers["cache-control"] = "no-store, no-cache, must-revalidate"
        return resp

    return app


app = create_app()
