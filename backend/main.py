#!/usr/bin/env python3
"""
RipRate — personal media acquisition & library manager.

Auto-installs Python dependencies on first run so the user can just:
    python main.py
"""
from __future__ import annotations

import subprocess
import sys
import logging
from pathlib import Path


def _ensure_deps() -> None:
    req = Path(__file__).parent / "requirements.txt"
    try:
        import fastapi, uvicorn, sqlalchemy, httpx, bs4, itsdangerous, cryptography  # noqa: F401
    except ImportError:
        print("[riprate] Installing Python dependencies…")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", "-r", str(req)]
        )
        print("[riprate] Dependencies installed.")


_ensure_deps()

from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from src.config import settings
from src.database import init_db
from src.queue.manager import download_manager
from src.scheduler import scheduler_service
from src.auth.router import router as auth_router
from src.api.search import router as search_router
from src.api.download import router as download_router
from src.api.admin import router as admin_router
from src.api.users import router as users_router
from src.api.requests import router as requests_router
from src.api.library import router as library_router
from src.api.schedules import router as schedules_router
from src.api.watch import router as watch_router
from src.api.setup import router as setup_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    from src.library.sync import check_media_storage, library_sync
    check_media_storage()
    init_db()
    # Re-arm the OpenVPN profile pool without blocking the web server. The
    # service closes the queue gate synchronously, then reconnects in the
    # background so login/settings remain available when VPN networking is
    # broken. Downloads remain gated until a tunnel and exit IP are verified.
    try:
        from src.vpn.openvpn_service import openvpn_service
        await openvpn_service.start_on_boot()
    except Exception as exc:
        logging.getLogger("vpn.openvpn").error("openvpn startup failed: %s", exc)

    # Reconcile persisted provider settings before the queue starts claiming
    # work. This removes provider keys deleted from the code and appends newly
    # shipped providers automatically.
    from src.providers.base import configured_providers
    configured_providers()
    # Category folders must exist inside the container even when the host has
    # nothing mounted over them yet (Settings wizard / MOUNT_* in .env).
    from src.library.categories import ensure_folders
    ensure_folders()
    await download_manager.start()
    await library_sync.start()
    await scheduler_service.start()
    try:
        yield
    finally:
        from src.api.admin import stop_subtitle_sync_tasks
        await stop_subtitle_sync_tasks()
        await library_sync.stop()
        await scheduler_service.stop()
        await download_manager.stop()
        # OpenVPN starts in its own process group. Explicitly terminate it on
        # reload/shutdown so the next application instance can create a clean
        # tunnel instead of colliding with a leftover process.
        try:
            from src.vpn.openvpn_service import openvpn_service
            await openvpn_service.shutdown()
        except Exception as exc:
            logging.getLogger("vpn.openvpn").warning("shutdown cleanup failed: %s", exc)


app = FastAPI(title="RipRate Media Vault", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_ALLOWED_ORIGIN_HOSTS = {urlparse(o).netloc for o in settings.allowed_origins}


class SecurityMiddleware(BaseHTTPMiddleware):
    """CSRF origin verification + security headers on every response."""

    async def dispatch(self, request: Request, call_next):
        # CSRF defence-in-depth (cookies are also SameSite=strict): browsers
        # attach Origin to unsafe cross-site requests — reject mismatches.
        if request.url.path.startswith("/api") and request.method in _UNSAFE_METHODS:
            origin = request.headers.get("origin")
            if origin:
                origin_host = urlparse(origin).netloc
                if (origin_host != request.headers.get("host", "")
                        and origin_host not in _ALLOWED_ORIGIN_HOSTS):
                    return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
            sec_fetch = request.headers.get("sec-fetch-site")
            if sec_fetch and sec_fetch not in ("same-origin", "same-site", "none"):
                return JSONResponse({"detail": "Cross-site request rejected"}, status_code=403)

        response = await call_next(request)

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        if request.url.path.startswith("/api"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


app.add_middleware(SecurityMiddleware)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    https_only=settings.session_secure,
    same_site="strict",
    max_age=settings.session_max_age_user,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)

app.include_router(auth_router, prefix="/api/auth")
app.include_router(search_router, prefix="/api")
app.include_router(download_router, prefix="/api")
app.include_router(library_router, prefix="/api")
app.include_router(watch_router, prefix="/api")
app.include_router(requests_router, prefix="/api")
app.include_router(users_router, prefix="/api")
app.include_router(schedules_router, prefix="/api")
app.include_router(admin_router, prefix="/api/admin")
app.include_router(setup_router, prefix="/api/setup")

# Serve compiled frontend in production (after `npm run build`)
_frontend_dist = Path(__file__).parent.parent / "frontend" / "dist"
if _frontend_dist.exists():
    # Serve static assets directly
    _assets_dir = _frontend_dist / "assets"
    if _assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

    # SPA catch-all: serve index.html for all non-API paths so browser
    # refresh on deep routes (/library, etc.) works correctly.
    _spa_index = _frontend_dist / "index.html"

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str, request: Request):
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        if _spa_index.exists():
            return FileResponse(str(_spa_index))
        return JSONResponse({"detail": "Frontend not built"}, status_code=404)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
