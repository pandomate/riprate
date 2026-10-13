from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class OpenVPNConfigError(ValueError):
    """Raised when an uploaded OpenVPN profile is invalid or unsafe."""


@dataclass(frozen=True)
class Remote:
    host: str
    port: int
    protocol: str = "udp"

    def as_dict(self) -> dict:
        return {"host": self.host, "port": self.port, "protocol": self.protocol}

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}/{self.protocol}"


@dataclass
class ProfileInfo:
    id: str
    name: str
    profile_type: str = "individual"  # country | individual
    group: str = ""
    remotes: list[Remote] = field(default_factory=list)
    enabled: bool = True
    has_credentials: bool = False
    created_at: Optional[str] = None
    last_connected_at: Optional[str] = None
    last_error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "profile_type": self.profile_type,
            "group": self.group,
            "remotes": [r.as_dict() for r in self.remotes],
            "enabled": self.enabled,
            "has_credentials": self.has_credentials,
            "created_at": self.created_at,
            "last_connected_at": self.last_connected_at,
            "last_error": self.last_error,
        }


@dataclass
class ProfileStatus:
    configured: bool
    enabled: bool
    running: bool
    active_profile_id: Optional[str]
    active_profile_name: Optional[str]
    active_source: Optional[dict]
    public_ip: Optional[str]
    kill_switch: bool
    last_error: Optional[str]
    profiles: list[ProfileInfo] = field(default_factory=list)
    connection_state: str = "off"

    def as_dict(self) -> dict:
        return {
            "configured": self.configured,
            "enabled": self.enabled,
            "running": self.running,
            "connection_state": self.connection_state,
            "active_profile_id": self.active_profile_id,
            "active_profile_name": self.active_profile_name,
            "active_source": self.active_source,
            "public_ip": self.public_ip,
            "kill_switch": self.kill_switch,
            "last_error": self.last_error,
            "profiles": [p.as_dict() for p in self.profiles],
        }


@dataclass
class ProfileResult:
    ok: bool
    message: str
    profile_id: Optional[str] = None
    profile_name: Optional[str] = None
    public_ip: Optional[str] = None
    source: Optional[dict] = None
    attempts: list[dict] = field(default_factory=list)
    at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    pending: bool = False

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "message": self.message,
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "public_ip": self.public_ip,
            "source": self.source,
            "attempts": self.attempts,
            "at": self.at,
            "pending": self.pending,
        }
