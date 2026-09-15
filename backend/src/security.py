"""
Shared security helpers: login rate limiting, audit logging, password policy.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import Request
from sqlalchemy.orm import Session

from src.database import AuditLog

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")

MIN_PASSWORD_LEN_USER = 10
MIN_PASSWORD_LEN_ADMIN = 12

# Account lockout: after N consecutive failures, lock for LOCKOUT_MINUTES
LOCKOUT_THRESHOLD = 5
LOCKOUT_MINUTES = 15


def validate_username(username: str) -> str | None:
    if not USERNAME_RE.match(username):
        return "Username must be 3-32 characters: letters, digits, dot, dash, underscore."
    return None


def validate_password(password: str, username: str = "", is_admin: bool = False) -> str | None:
    min_len = MIN_PASSWORD_LEN_ADMIN if is_admin else MIN_PASSWORD_LEN_USER
    if len(password) < min_len:
        return f"Password must be at least {min_len} characters."
    if len(password) > 128:
        return "Password must be at most 128 characters."
    if username and password.lower() == username.lower():
        return "Password must not be the same as the username."
    return None


class SlidingWindowLimiter:
    """In-process sliding-window rate limiter keyed by an arbitrary string."""

    def __init__(self, max_events: int, window_seconds: float) -> None:
        self.max_events = max_events
        self.window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def hit(self, key: str) -> bool:
        """Record an event; returns True if the key is now over the limit."""
        now = time.monotonic()
        with self._lock:
            q = self._events[key]
            while q and now - q[0] > self.window:
                q.popleft()
            q.append(now)
            return len(q) > self.max_events

    def is_limited(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._events.get(key)
            if not q:
                return False
            while q and now - q[0] > self.window:
                q.popleft()
            return len(q) >= self.max_events


# Login attempts: 10 per 15 min per IP, 5 per 15 min per username
login_ip_limiter = SlidingWindowLimiter(max_events=10, window_seconds=900)
login_user_limiter = SlidingWindowLimiter(max_events=5, window_seconds=900)


def client_ip(request: Request) -> str:
    # nginx sets X-Real-IP; fall back to the socket peer
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


def audit(db: Session, action: str, username: str | None = None,
          detail: str | None = None, ip: str | None = None) -> None:
    try:
        db.add(AuditLog(action=action, username=username, detail=detail, ip=ip))
        db.commit()
    except Exception:
        db.rollback()
