"""
First-run setup wizard API (UNAUTHENTICATED, but self-locking).

A brand-new install boots without an admin (see config._validate /
database._seed_admin). While no setup has happened, these endpoints let the
operator create the admin account and configure the app from the browser; the
results are written to the DB *and* mirrored to .env. Once setup is complete the
endpoints refuse (409) and the frontend falls through to the normal login — the
wizard is unreachable thereafter.

`needs_setup()` is the single gate. It's true only on a genuinely fresh install:
no admin yet, or an admin exists but the DB shows no prior use (grandfathers
existing/upgraded installs so they never see the wizard).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

import bcrypt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.config import settings as cfg
from src.database import (
    DownloadJob, SessionLocal, User, UserRole, get_setting, set_setting,
)
from src.library.categories import all_categories, ensure_folders, save_categories
from src.providers import tmdb
from src.providers.base import all_provider_meta, configured_providers
from src.queue.manager import download_manager
from src.security import validate_password, validate_username

router = APIRouter()
_log = logging.getLogger("setup")


def needs_setup() -> bool:
    """Onboarding is needed exactly when no admin account exists in the DB.

    The admin lives ONLY in the database (created by this wizard), never in
    .env — so "is there an admin?" is the single source of truth for whether
    the server has been set up. A fresh/wiped DB has none → wizard; an
    established install already has one → straight to login."""
    with SessionLocal() as db:
        return db.query(User).filter(User.role == UserRole.admin).first() is None


def _admin_user(db) -> Optional[User]:
    return db.query(User).filter(User.role == UserRole.admin).order_by(User.id).first()


def _prefill() -> dict:
    """Current config to pre-populate the wizard (secrets only as *_set bools)."""
    from src.api.admin import _settings_payload
    payload = _settings_payload()
    with SessionLocal() as db:
        admin = _admin_user(db)
    payload["admin_username"] = admin.username if admin else (cfg.admin_username or "admin")
    payload["admin_password_set"] = bool(admin) or bool(cfg.admin_password)
    return payload


@router.get("/status")
def setup_status():
    if not needs_setup():
        return {"needs_setup": False}
    return {"needs_setup": True, "config": _prefill()}


class SetupOpenVPNProfileInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    config: str = Field(min_length=1, max_length=512000)
    username: Optional[str] = Field(default=None, max_length=500)
    password: Optional[str] = Field(default=None, max_length=500)
    profile_type: str = Field(default="auto", pattern="^(auto|country|individual)$")
    group: Optional[str] = Field(default=None, max_length=80)


class SetupOpenVPNProfileEnabledInput(BaseModel):
    enabled: bool


def _require_setup_openvpn():
    if not needs_setup():
        raise HTTPException(status_code=409, detail="Setup has already been completed")
    from src.vpn.openvpn_service import openvpn_service
    return openvpn_service


@router.get("/vpn/openvpn")
async def setup_openvpn_status():
    return await _require_setup_openvpn().status()


@router.post("/vpn/openvpn/profiles")
async def setup_add_openvpn_profile(body: SetupOpenVPNProfileInput):
    from vpn_profile_manager import OpenVPNConfigError
    service = _require_setup_openvpn()
    try:
        profile = service.add_profile(
            body.name, body.config, body.username, body.password,
            body.profile_type, body.group,
        )
    except OpenVPNConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"profile": profile.as_dict(), "status": await service.status()}


@router.patch("/vpn/openvpn/profiles/{profile_id}")
async def setup_update_openvpn_profile(profile_id: str, body: SetupOpenVPNProfileEnabledInput):
    from vpn_profile_manager import OpenVPNConfigError
    service = _require_setup_openvpn()
    try:
        service.set_profile_enabled(profile_id, body.enabled)
    except OpenVPNConfigError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return await service.status()


@router.delete("/vpn/openvpn/profiles/{profile_id}")
async def setup_delete_openvpn_profile(profile_id: str):
    from vpn_profile_manager import OpenVPNConfigError
    service = _require_setup_openvpn()
    try:
        await service.remove_profile(profile_id)
    except OpenVPNConfigError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return await service.status()


@router.post("/vpn/openvpn/enable")
async def setup_enable_openvpn():
    service = _require_setup_openvpn()
    result = await service.enable_random()
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.message)
    return {"result": result.as_dict(), "status": await service.status()}


@router.post("/vpn/openvpn/rotate")
async def setup_rotate_openvpn():
    service = _require_setup_openvpn()
    result = await service.rotate("manual setup rotation requested")
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.message)
    return {"result": result.as_dict(), "status": await service.status()}


@router.post("/vpn/openvpn/disable")
async def setup_disable_openvpn():
    service = _require_setup_openvpn()
    await service.disable()
    return await service.status()


class SetupRequest(BaseModel):
    admin_username: str = Field(min_length=1, max_length=64)
    admin_password: Optional[str] = Field(default=None, max_length=200)  # blank = keep existing
    media_categories: Optional[list[dict]] = None
    preferred_quality: Optional[int] = None
    search_source: str = Field(default="tmdb", pattern="^(tmdb|imdb)$")
    tmdb_api_key: Optional[str] = Field(default=None, max_length=500)
    imdb_cookie: Optional[str] = Field(default=None, max_length=8000)
    wyzie_api_key: Optional[str] = Field(default=None, max_length=200)
    wyzie_enabled: bool = False
    subtitle_language: str = Field(default="en", max_length=16)
    max_concurrent_downloads: int = Field(default=3, ge=1, le=10)
    segment_connections: int = Field(default=6, ge=1, le=16)
    history_retention_days: int = Field(default=0, ge=0, le=3650)  # 0 = keep forever
    video_crf: int = Field(default=20, ge=0, le=51)
    video_preset: str = Field(default="medium", max_length=20)
    max_file_gb: float = Field(default=10.0, ge=0.5, le=200)
    stream_providers: Optional[list[dict]] = None
@router.post("")
async def run_setup(body: SetupRequest, request: Request):
    if not needs_setup():
        raise HTTPException(status_code=409, detail="Setup has already been completed")

    username = body.admin_username.strip()
    uerr = validate_username(username)
    if uerr:
        raise HTTPException(status_code=422, detail=uerr)

    pw = (body.admin_password or "").strip()

    # ── Admin account (create or update) — password is bcrypt-hashed in the DB ──
    with SessionLocal() as db:
        admin = _admin_user(db)
        if pw:
            perr = validate_password(pw, username, is_admin=True)
            if perr:
                raise HTTPException(status_code=422, detail=perr)
        elif admin is None:
            raise HTTPException(status_code=422, detail="Choose an admin password to finish setup")

        # username must be unique among OTHER accounts
        clash = db.query(User).filter(
            User.username == username,
            User.id != (admin.id if admin else -1),
        ).first()
        if clash:
            raise HTTPException(status_code=409, detail="That username is already taken")

        pw_to_env: Optional[str] = None
        if admin is None:
            admin = User(
                username=username,
                password_hash=bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode(),
                role=UserRole.admin,
                is_active=True,
                password_changed_at=datetime.utcnow(),
            )
            db.add(admin)
            pw_to_env = pw
        else:
            admin.username = username
            admin.role = UserRole.admin
            admin.is_active = True
            if pw:
                admin.password_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
                admin.password_changed_at = datetime.utcnow()
                admin.session_version = (admin.session_version or 1) + 1
                pw_to_env = pw
        db.commit()

    # The admin — username AND password — lives ONLY in the database (bcrypt).
    # Nothing about it is written to .env, so "an admin row exists" is the sole
    # setup signal (see needs_setup) and a DB wipe reliably re-opens the wizard.
    _ = pw_to_env  # (password is persisted in the DB hash above, not to .env)

    # ── Search source ───────────────────────────────────────────────────────
    tmdb_key = (body.tmdb_api_key or "").strip()
    if tmdb_key:
        set_setting("tmdb_api_key", tmdb_key)   # auto-mirrors to .env TMDB_API_KEY
    if body.search_source == "tmdb" and not (tmdb_key or tmdb.has_key()):
        raise HTTPException(
            status_code=422,
            detail="TMDB search requires a TMDB API key, or select IMDB search.",
        )
    set_setting("search_source", body.search_source)

    # ── IMDB cookie (optional, DB-only / ephemeral by design) ──
    if body.imdb_cookie is not None:
        cookie = body.imdb_cookie.strip()
        set_setting("imdb_cookie", cookie)
        set_setting("imdb_cookie_set_at", datetime.utcnow().isoformat() if cookie else "")

    # ── Wyzie subtitles (omitted key keeps an existing configuration) ──
    if body.wyzie_api_key is not None:
        set_setting("wyzie_api_key", body.wyzie_api_key.strip())
    set_setting("wyzie_enabled", "true" if body.wyzie_enabled else "false")
    from src.subtitles import normalize_language
    set_setting("subtitle_language", normalize_language(body.subtitle_language))

    # ── Quality / concurrency / connections ──
    if body.preferred_quality is not None:
        set_setting("preferred_quality", str(body.preferred_quality))
    n = await download_manager.set_concurrency(body.max_concurrent_downloads)
    set_setting("max_concurrent_downloads", str(n))
    set_setting("segment_connections", str(body.segment_connections))
    set_setting("history_retention_days", str(body.history_retention_days))

    # ── Transcoding (only used when a re-encode is needed) ──
    from src.queue.manager import _X264_PRESETS
    set_setting("video_crf", str(body.video_crf))
    set_setting("video_preset", body.video_preset if body.video_preset in _X264_PRESETS else "medium")
    set_setting("max_file_gb", str(body.max_file_gb))

    # ── Download providers (validate keys against the registry) ──
    if body.stream_providers is not None:
        valid = {p["key"] for p in all_provider_meta()}
        clean = [
            {"key": p["key"], "enabled": bool(p.get("enabled", True))}
            for p in body.stream_providers
            if isinstance(p, dict) and p.get("key") in valid
        ]
        if not clean or not any(p["enabled"] for p in clean):
            raise HTTPException(
                status_code=422,
                detail="At least one stream source must be enabled.",
            )
        if any(p["key"] == "nova" and p["enabled"] for p in clean) and not tmdb.has_key():
            raise HTTPException(
                status_code=422,
                detail="Nova requires a TMDB API key before it can be enabled.",
            )
        set_setting("stream_providers", json.dumps(clean))

    # ── Categories & folders ──
    if body.media_categories is not None:
        try:
            save_categories(body.media_categories)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    ensure_folders()

    # No "setup_completed" flag needed — the admin row created above is itself
    # the signal that the server is configured (see needs_setup).
    # Keep this log statement side-effect free.  In particular, do not refer
    # to a stale `meta` variable here: the setup work has already been
    # committed, so a logging-time NameError would turn a successful setup
    # into a 500 and leave the wizard locked on the next attempt.
    _log.info("First-run setup completed via wizard (admin=%s)", username)
    return {"ok": True}
