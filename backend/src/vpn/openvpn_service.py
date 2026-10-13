"""RipRate adapter for the reusable OpenVPN profile manager.

The actual profile storage, OpenVPN process lifecycle, random selection, and
failover live in the top-level ``vpn_profiles`` package. This module only
connects that package to RipRate's queue gate and persisted VPN mode.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from pathlib import Path

from src.database import get_setting, set_setting

from vpn_profile_manager import OpenVPNConfigError, OpenVPNProfileManager, ProfileResult

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
        # OpenVPN can take 30+ seconds per remote, and a profile bundle can
        # contain several remotes. Never make an HTTP request wait for that
        # work: this task is observed through the status endpoint instead.
        self._connection_task: asyncio.Task | None = None

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

    async def _connect_in_background(self, *, rotate: bool = False,
                                     reason: str = "manual rotation") -> None:
        """Connect or rotate without holding an API request open."""
        try:
            current = await self.manager.status()
            if rotate and current.running:
                result = await self.manager.failover(reason)
            else:
                # start_on_boot keeps the desired state enabled and arranges
                # retries when every candidate fails transiently.
                result = await self.manager.start_on_boot()
            if result and not result.ok:
                _log.error("[openvpn] connection attempt failed: %s", result.message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.error("[openvpn] background connection failed: %s", exc, exc_info=True)

    async def _cancel_connection_task(self) -> None:
        task = self._connection_task
        self._connection_task = None
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def _connection_pending(self) -> bool:
        return bool(self._connection_task and not self._connection_task.done())

    async def _queue_connection(self, *, rotate: bool = False,
                                reason: str = "manual rotation") -> ProfileResult:
        if not self.manager.store.enabled_profiles():
            return ProfileResult(False, "No included VPN profiles are available.")
        if self._connection_pending():
            return ProfileResult(
                True, "A VPN connection attempt is already in progress.", pending=True
            )

        # Persist intent and close the download gate before returning to the
        # browser. status() will now report `connecting` immediately.
        set_setting("vpn_enabled", "true")
        self.manager.store.set_runtime(enabled=True, clear_active=not rotate)
        await _dm().apply_vpn_gate(True)
        operation = "rotation" if rotate else "connection"
        self._connection_task = asyncio.create_task(
            self._connect_in_background(rotate=rotate, reason=reason),
            name=f"openvpn-{operation}",
        )
        return ProfileResult(
            True,
            f"VPN {operation} started. Status will update automatically.",
            pending=True,
        )

    async def start_on_boot(self) -> None:
        runtime_enabled = self.manager.store.runtime().get("enabled")
        if get_setting("vpn_enabled") != "true" and not runtime_enabled:
            return
        # Reconcile the DB flag with the profile manager's persisted desired
        # state so a restart resumes the user's enabled profile pool.
        if get_setting("vpn_enabled") != "true":
            set_setting("vpn_enabled", "true")
        if not runtime_enabled:
            self.manager.store.set_runtime(enabled=True)
        # Close the queue gate before workers start, then reconnect in the
        # background. A dead VPN or DNS resolver must never prevent the API,
        # login page, or settings UI from becoming available to fix it.
        await _dm().apply_vpn_gate(True)
        if not self._connection_pending():
            self._connection_task = asyncio.create_task(
                self._connect_in_background(), name="openvpn-startup"
            )

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
        return await self._queue_connection()

    async def rotate(self, reason: str = "manual rotation"):
        return await self._queue_connection(rotate=True, reason=reason)

    async def disable(self) -> None:
        await self._cancel_connection_task()
        await self.manager.disconnect()
        set_setting("vpn_enabled", "false")

    async def shutdown(self) -> None:
        await self._cancel_connection_task()
        await self.manager.shutdown()

    async def status(self) -> dict:
        return (await self.manager.status()).as_dict()


openvpn_service = OpenVPNService()
