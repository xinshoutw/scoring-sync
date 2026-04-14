import secrets
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from net_grading.auth.session import SESSION_COOKIE
from net_grading.config import get_settings
from net_grading.db.engine import dispose_engine
from net_grading.routes import auth as auth_routes
from net_grading.routes import conflicts as conflicts_routes
from net_grading.routes import grading as grading_routes
from net_grading.routes import settings as settings_routes
from net_grading.routes.templating import templates

_STATIC_DIR = Path(__file__).parent / "static"

# 進程啟動唯一 id；瀏覽器 heartbeat 比對 → 偵測重啟
SERVER_INSTANCE_ID = secrets.token_hex(8)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()


def _prefers_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept or "*/*" in accept


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

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException) -> Response:
        # 401：未登入或 session 過期 → 導回 /login（並清掉失效 cookie）
        if exc.status_code == 401:
            resp = RedirectResponse("/login", status_code=303)
            resp.delete_cookie(SESSION_COOKIE, path="/")
            return resp
        # 404：HTML 友善錯誤頁（API 請求仍吐 JSON）
        if exc.status_code == 404 and _prefers_html(request):
            return templates.TemplateResponse(
                request,
                "error.html",
                {
                    "user": None,
                    "code": 404,
                    "title": "找不到頁面",
                    "message": "這個網址不存在或已搬家。",
                },
                status_code=404,
            )
        return await http_exception_handler(request, exc)

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
