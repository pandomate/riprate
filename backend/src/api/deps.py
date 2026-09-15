import time

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.config import settings
from src.database import User, UserRole, get_db

_UNAUTH = HTTPException(status_code=401, detail="Not authenticated")


def _session_user(request: Request, db: Session) -> User | None:
    """Resolve and validate the session; returns the User or None.

    Enforces: user exists + active, session version matches (revocation),
    absolute lifetime and idle timeout (stricter for admins).
    """
    sess = request.session
    user_id = sess.get("uid")
    if not user_id:
        return None

    user = db.query(User).filter_by(id=user_id).first()
    if not user or not user.is_active:
        sess.clear()
        return None

    # Session revocation: password change / disable bumps session_version
    if sess.get("ver") != user.session_version:
        sess.clear()
        return None

    now = time.time()
    issued = sess.get("iat", 0)
    last_seen = sess.get("seen", issued)
    is_admin = user.role == UserRole.admin
    max_age = settings.session_max_age_admin if is_admin else settings.session_max_age_user
    idle = settings.session_idle_admin if is_admin else settings.session_idle_user

    if now - issued > max_age or now - last_seen > idle:
        sess.clear()
        return None

    # Refresh activity timestamp (only when meaningfully stale, to avoid
    # re-signing the cookie on every request)
    if now - last_seen > 60:
        sess["seen"] = int(now)
    return user


def require_auth(request: Request, db: Session = Depends(get_db)) -> User:
    user = _session_user(request, db)
    if user is None:
        raise _UNAUTH
    return user


def require_admin(user: User = Depends(require_auth)) -> User:
    if user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
