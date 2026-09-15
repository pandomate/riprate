import time
from datetime import datetime, timedelta

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.api.deps import require_auth
from src.database import User, UserRole, get_db
from src.security import (
    LOCKOUT_MINUTES, LOCKOUT_THRESHOLD, audit, client_ip,
    login_ip_limiter, login_user_limiter, validate_password,
)

router = APIRouter(tags=["auth"])

# Constant-cost comparison target for unknown usernames (prevents timing-based
# username enumeration).
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password-for-timing", bcrypt.gensalt()).decode()


class LoginRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: str = Field(max_length=128)


def _verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        return False


def _start_session(request: Request, user: User) -> None:
    # Fresh session on login: drop anything that was there before
    request.session.clear()
    now = int(time.time())
    request.session.update({
        "uid": user.id,
        "ver": user.session_version,
        "iat": now,
        "seen": now,
    })


@router.post("/login")
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    ip = client_ip(request)
    generic = HTTPException(status_code=401, detail="Invalid username or password")

    if login_ip_limiter.hit(ip) or login_user_limiter.is_limited(body.username.lower()):
        audit(db, "login.rate_limited", username=body.username, ip=ip)
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")

    user = db.query(User).filter_by(username=body.username).first()

    if user is None:
        _verify_password(body.password, _DUMMY_HASH)  # constant-time-ish
        login_user_limiter.hit(body.username.lower())
        audit(db, "login.failed", username=body.username, detail="unknown user", ip=ip)
        raise generic

    if user.locked_until and user.locked_until > datetime.utcnow():
        _verify_password(body.password, _DUMMY_HASH)
        audit(db, "login.locked", username=user.username, ip=ip)
        raise HTTPException(
            status_code=429,
            detail="Account temporarily locked after repeated failures. Try again later.",
        )

    if not user.is_active:
        _verify_password(body.password, _DUMMY_HASH)
        audit(db, "login.disabled", username=user.username, ip=ip)
        raise generic

    if not _verify_password(body.password, user.password_hash):
        login_user_limiter.hit(body.username.lower())
        user.failed_logins += 1
        if user.failed_logins >= LOCKOUT_THRESHOLD:
            user.locked_until = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_logins = 0
            audit(db, "login.lockout", username=user.username,
                  detail=f"locked for {LOCKOUT_MINUTES} min", ip=ip)
        else:
            audit(db, "login.failed", username=user.username, ip=ip)
        db.commit()
        raise generic

    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = datetime.utcnow()
    db.commit()
    _start_session(request, user)
    audit(db, "login.success", username=user.username, ip=ip)
    return {"ok": True, "username": user.username, "role": user.role}


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("uid")
    if uid:
        user = db.query(User).filter_by(id=uid).first()
        audit(db, "logout", username=user.username if user else str(uid), ip=client_ip(request))
    request.session.clear()
    return {"ok": True}


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)):
    from src.api.deps import _session_user
    user = _session_user(request, db)
    if user is None:
        return {"username": None, "role": None, "authenticated": False}
    return {"username": user.username, "role": user.role, "authenticated": True}


@router.post("/change-password")
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    if not _verify_password(body.current_password, user.password_hash):
        audit(db, "password.change_failed", username=user.username, ip=client_ip(request))
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    err = validate_password(body.new_password, user.username, is_admin=user.role == UserRole.admin)
    if err:
        raise HTTPException(status_code=400, detail=err)

    db_user = db.query(User).filter_by(id=user.id).first()
    db_user.password_hash = bcrypt.hashpw(body.new_password.encode(), bcrypt.gensalt()).decode()
    db_user.password_changed_at = datetime.utcnow()
    db_user.session_version += 1  # invalidate all other sessions
    db.commit()

    # Keep the current session alive under the new version
    request.session["ver"] = db_user.session_version
    audit(db, "password.changed", username=user.username, ip=client_ip(request))
    return {"ok": True}
