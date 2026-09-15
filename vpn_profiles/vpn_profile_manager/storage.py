from __future__ import annotations

import json
import os
import secrets
from datetime import datetime
from pathlib import Path

from .config import parse_config, validate_name
from .models import OpenVPNConfigError, ProfileInfo, Remote


class ProfileStore:
    """File-backed profile store with private files restricted to owner access."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.state_path = self.root / "state.json"

    def _state(self) -> dict:
        if not self.state_path.exists():
            return {"profiles": {}, "active_profile_id": None, "enabled": False}
        try:
            data = json.loads(self.state_path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {"profiles": {}, "active_profile_id": None, "enabled": False}

    def _save_state(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        tmp.chmod(0o600)
        os.replace(tmp, self.state_path)

    def _profile_dir(self, profile_id: str) -> Path:
        if not profile_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in profile_id):
            raise OpenVPNConfigError("Invalid profile id.")
        return self.root / profile_id

    def list(self) -> list[ProfileInfo]:
        result: list[ProfileInfo] = []
        for raw in self._state().get("profiles", {}).values():
            try:
                result.append(self._from_meta(raw))
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(result, key=lambda p: (p.name.lower(), p.id))

    def _from_meta(self, raw: dict) -> ProfileInfo:
        remotes = [Remote(str(r["host"]), int(r["port"]), str(r.get("protocol", "udp")))
                   for r in raw.get("remotes", [])]
        profile_type = str(raw.get("profile_type") or ("country" if len(remotes) > 1 else "individual"))
        if profile_type not in {"country", "individual"}:
            profile_type = "country" if len(remotes) > 1 else "individual"
        return ProfileInfo(
            id=str(raw["id"]), name=str(raw["name"]),
            profile_type=profile_type, group=str(raw.get("group") or raw["name"]),
            remotes=remotes,
            enabled=bool(raw.get("enabled", True)),
            has_credentials=bool(raw.get("has_credentials", False)),
            created_at=raw.get("created_at"), last_connected_at=raw.get("last_connected_at"),
            last_error=raw.get("last_error"),
        )

    def get(self, profile_id: str) -> ProfileInfo:
        raw = self._state().get("profiles", {}).get(profile_id)
        if not raw:
            raise OpenVPNConfigError("VPN profile not found.")
        return self._from_meta(raw)

    def read_config(self, profile_id: str) -> str:
        path = self._profile_dir(profile_id) / "profile.ovpn"
        try:
            return path.read_text()
        except OSError as exc:
            raise OpenVPNConfigError("VPN profile file is unavailable.") from exc

    def credentials_path(self, profile_id: str) -> Path | None:
        path = self._profile_dir(profile_id) / "auth.txt"
        return path if path.exists() else None

    def add(self, name: str, config_text: str, username: str | None = None,
            password: str | None = None, profile_type: str = "auto",
            group: str | None = None) -> ProfileInfo:
        name = validate_name(name)
        remotes = parse_config(config_text)
        if profile_type not in {"auto", "country", "individual"}:
            raise OpenVPNConfigError("Profile type must be auto, country, or individual.")
        if profile_type == "auto":
            profile_type = "country" if len(remotes) > 1 else "individual"
        group = validate_name(group or name)
        if (username is None) != (password is None):
            raise OpenVPNConfigError("Provide both a VPN username and password, or neither.")
        if "auth-user-pass" in config_text.lower() and not username:
            raise OpenVPNConfigError("This profile requires an OpenVPN username and password.")
        state = self._state()
        profile_id = secrets.token_urlsafe(12).replace("-", "_")
        now = datetime.utcnow().isoformat() + "Z"
        directory = self._profile_dir(profile_id)
        directory.mkdir(mode=0o700)
        (directory / "profile.ovpn").write_text(config_text.strip() + "\n")
        (directory / "profile.ovpn").chmod(0o600)
        if username is not None:
            auth = directory / "auth.txt"
            auth.write_text(username.strip() + "\n" + password + "\n")
            auth.chmod(0o600)
        info = ProfileInfo(
            id=profile_id, name=name, profile_type=profile_type, group=group,
            remotes=remotes, enabled=True, has_credentials=username is not None,
            created_at=now,
        )
        state.setdefault("profiles", {})[profile_id] = info.as_dict()
        self._save_state(state)
        return info

    def remove(self, profile_id: str) -> None:
        self.get(profile_id)
        state = self._state()
        state.get("profiles", {}).pop(profile_id, None)
        if state.get("active_profile_id") == profile_id:
            state["active_profile_id"] = None
        self._save_state(state)
        directory = self._profile_dir(profile_id)
        for child in directory.glob("*"):
            child.unlink(missing_ok=True)
        directory.rmdir()

    def set_enabled(self, profile_id: str, enabled: bool) -> ProfileInfo:
        info = self.get(profile_id)
        state = self._state()
        state["profiles"][profile_id]["enabled"] = bool(enabled)
        self._save_state(state)
        info.enabled = bool(enabled)
        return info

    def enabled_profiles(self) -> list[ProfileInfo]:
        return [p for p in self.list() if p.enabled]

    def set_runtime(self, *, active_profile_id: str | None = None,
                    clear_active: bool = False, enabled: bool | None = None) -> None:
        state = self._state()
        if clear_active:
            state["active_profile_id"] = None
        elif active_profile_id is not None:
            state["active_profile_id"] = active_profile_id
        if enabled is not None:
            state["enabled"] = bool(enabled)
        self._save_state(state)

    def runtime(self) -> dict:
        state = self._state()
        return {
            "active_profile_id": state.get("active_profile_id"),
            "enabled": bool(state.get("enabled", False)),
        }

    def mark_connected(self, profile_id: str) -> None:
        state = self._state()
        if profile_id in state.get("profiles", {}):
            state["profiles"][profile_id]["last_connected_at"] = datetime.utcnow().isoformat() + "Z"
            state["profiles"][profile_id]["last_error"] = None
            self._save_state(state)

    def mark_error(self, profile_id: str, error: str) -> None:
        state = self._state()
        if profile_id in state.get("profiles", {}):
            state["profiles"][profile_id]["last_error"] = error[:500]
            self._save_state(state)
