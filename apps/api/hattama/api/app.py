from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from hattama import __version__
from hattama.config import Settings, get_settings
from hattama.ingest.ws import ingest_endpoint, stop_supervisor
from hattama.logging_setup import configure_logging, enforce_offline_env


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        if request.url.path.startswith("/api/") and not request.url.path.startswith("/api/docs"):
            response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            response.headers.setdefault("Cache-Control", "no-store")
        elif not request.url.path.startswith("/api/"):
            # dashboard: only same-origin resources (no CDN, no remote fonts, no analytics)
            response.headers.setdefault("Content-Security-Policy",
                                        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                        "media-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        return response


class BodyLimit(BaseHTTPMiddleware):
    """Reject oversized JSON bodies early. Uploads have their own streaming limit."""

    def __init__(self, app, max_json: int, upload_prefixes: tuple[str, ...]) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self.max_json = max_json
        self.upload_prefixes = upload_prefixes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method in ("POST", "PUT", "PATCH") and not any(
                p in request.url.path for p in self.upload_prefixes):
            length = request.headers.get("content-length")
            if length is None and request.headers.get("transfer-encoding"):
                return JSONResponse({"detail": "Требуется Content-Length"}, status_code=411)
            if length is not None and int(length) > self.max_json:
                return JSONResponse({"detail": "Слишком большой запрос"}, status_code=413)
        return await call_next(request)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging()
    enforce_offline_env()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task = asyncio.create_task(stop_supervisor(stop))
        try:
            yield
        finally:
            stop.set()
            await task

    app = FastAPI(
        title="Хаттама Live API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if settings.env != "prod" else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.env != "prod" else None,
    )
    app.add_middleware(BodyLimit, max_json=settings.max_json_body_bytes, upload_prefixes=("/uploads",))
    app.add_middleware(SecurityHeaders)

    from hattama.api.routes import auth, capture, diagnostics, live, meetings, review

    for module in (auth, meetings, capture, live, review, diagnostics):
        app.include_router(module.router)
    app.add_api_websocket_route("/api/v1/ingest/ws", ingest_endpoint)

    @app.get("/api/v1/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    # Minimal dashboard (static, no CDN, no remote fonts) served from the same origin as the API.
    from fastapi.staticfiles import StaticFiles

    from hattama.config import REPO_ROOT

    lib_dir = REPO_ROOT / "packages" / "audio-client"
    static_dir = REPO_ROOT / "apps" / "web" / "static"
    if lib_dir.is_dir():
        app.mount("/lib", StaticFiles(directory=lib_dir), name="audio-client")
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="web")
    return app
