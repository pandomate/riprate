"""RipRate adapter for the reusable OpenVPN profile manager.

The actual profile storage, OpenVPN process lifecycle, random selection, and
failover live in the top-level ``vpn_profiles`` package. This module only
connects that package to RipRate's queue gate and persisted VPN mode.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from src.database import get_setting, set_setting

from vpn_profile_manager import OpenVPNConfigError, OpenVPNProfileManager

_log = logging.getLogger("vpn.openvpn")


def _dm():
    from src.queue.manager import download_manager
    return download_manager


class OpenVPNService:
    def __init__(self) -> None:
        root = Path(os.environ.get("VPN_PROFILE_DIR", "/app/data/vpn-profiles"))
        # Docker's bridge path can be MTU 1420 even when the host/container
        # interface reports 1500. Proton profiles commonly set tun-mtu 1500
        # and mssfix 0, which lets TCP connect but black-holes larger TLS
        # packets. Keep these RipRate deployment defaults here; the reusable
        # manager accepts openvpn_options for other network layouts.
        tun_mtu = os.environ.get("VPN_OPENVPN_TUN_MTU", "1280")
        mssfix = os.environ.get("VPN_OPENVPN_MSSFIX", "1200")
        try:
            health_interval = float(os.environ.get("VPN_OPENVPN_HEALTH_INTERVAL", "30"))
        except ValueError:
            health_interval = 30.0
        try:
            health_failures = int(os.environ.get("VPN_OPENVPN_HEALTH_FAILURES", "2"))
        except ValueError:
            health_failures = 2
        self.manager = OpenVPNProfileManager(
            root,
            state_callback=self._state_changed,
            failure_callback=self._failure,
            openvpn_options=("--tun-mtu", tun_mtu, "--mssfix", mssfix),
            health_check_interval=health_interval,
            health_check_failures=health_failures,
        )
        self.cached_active = False
        self.cached_public_ip: str | None = None

    async def _state_changed(self, active: bool) -> None:
        self.cached_active = active
        if not active:
            self.cached_public_ip = None
        else:
            status = await self.manager.status()
            self.cached_public_ip = status.public_ip
        await _dm().apply_vpn_gate(not active)

    async def _failure(self, reason: str) -> None:
        _log.warning("[openvpn] %s", reason)

    async def start_on_boot(self):
        runtime_enabled = self.manager.store.runtime().get("enabled")
        if get_setting("vpn_enabled") != "true" and not runtime_enabled:
            return None
        # Reconcile the DB flag with the profile manager's persisted desired
        # state so a restart resumes the user's enabled profile pool.
        if get_setting("vpn_enabled") != "true":
            set_setting("vpn_enabled", "true")
        if not runtime_enabled:
            self.manager.store.set_runtime(enabled=True)
        result = await self.manager.start_on_boot()
        if result and not result.ok:
            _log.error("[openvpn] startup failed: %s", result.message)
        return result

    def add_profile(self, name: str, config_text: str, username: str | None = None,
                    password: str | None = None, profile_type: str = "auto",
                    group: str | None = None):
        return self.manager.add_profile(
            name, config_text, username=username, password=password,
            profile_type=profile_type, group=group,
        )

    async def remove_profile(self, profile_id: str) -> None:
        await self.manager.remove_profile(profile_id)

    def set_profile_enabled(self, profile_id: str, enabled: bool):
        return self.manager.set_profile_enabled(profile_id, enabled)

    async def enable_random(self):
        result = await self.manager.enable_random()
        if result.ok:
            set_setting("vpn_enabled", "true")
        return result

    async def rotate(self, reason: str = "manual rotation"):
        current = await self.manager.status()
        result = (await self.manager.failover(reason)
                  if current.running else await self.manager.enable_random())
        if result.ok:
            set_setting("vpn_enabled", "true")
        return result

    async def disable(self) -> None:
        await self.manager.disconnect()
        set_setting("vpn_enabled", "false")

    async def shutdown(self) -> None:
        await self.manager.shutdown()

    async def status(self) -> dict:
        return (await self.manager.status()).as_dict()


openvpn_service = OpenVPNService()
