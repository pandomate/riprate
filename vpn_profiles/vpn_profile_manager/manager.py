from __future__ import annotations

import asyncio
import ipaddress
import os
import random
import signal
import urllib.request
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Optional

from .config import config_for_remote, parse_config, resolve_host
from .firewall import IptablesKillSwitch
from .models import OpenVPNConfigError, ProfileInfo, ProfileResult, ProfileStatus, Remote
from .storage import ProfileStore

StateCallback = Callable[[bool], Awaitable[None]]
PublicIpChecker = Callable[[float], Awaitable[str | None]]

_IP_ECHO = (
    # DNS can be temporarily unavailable while OpenVPN replaces the default
    # route. Cloudflare serves a TLS-valid trace endpoint directly on 1.1.1.1,
    # giving verification a DNS-independent path through the tunnel.
    "https://1.1.1.1/cdn-cgi/trace",
    "https://ipv4.icanhazip.com",
    "https://api.ipify.org",
    "https://4.ident.me",
)


def _parse_public_ipv4(value: str) -> str | None:
    """Extract an IPv4 address from a plain-IP or Cloudflare trace response."""
    candidates = [value.strip()]
    candidates.extend(
        line[3:].strip() for line in value.splitlines() if line.startswith("ip=")
    )
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.version == 4 and address.is_global:
            return str(address)
    return None


class OpenVPNProfileManager:
    """Manage a pool of OpenVPN profiles in one network namespace.

    The manager is application-agnostic. `state_callback` is called whenever
    the tunnel becomes usable or unusable; an application can use it to pause
    jobs while rotation is in progress. `failure_callback` is optional and is
    called after the watchdog rotates away from a dead tunnel.
    """

    def __init__(self, root: Path, *, openvpn_binary: str = "openvpn",
                 kill_switch: IptablesKillSwitch | None = None,
                 state_callback: StateCallback | None = None,
                 failure_callback: Callable[[str], Awaitable[None]] | None = None,
                 public_ip_checker: PublicIpChecker | None = None,
                 ip_echo_urls: Iterable[str] = _IP_ECHO,
                 require_ip_change: bool = True,
                 runtime_dir: Path | None = None,
                 openvpn_options: Iterable[str] = (),
                 health_check_interval: float = 30.0,
                 health_check_failures: int = 2):
        self.store = ProfileStore(root)
        self.openvpn_binary = openvpn_binary
        self.kill_switch = kill_switch or IptablesKillSwitch()
        self.state_callback = state_callback
        self.failure_callback = failure_callback
        # Embedding applications can replace the egress check or its endpoint
        # set. The default deliberately bypasses environment proxies so it
        # measures this network namespace's route, not a proxy's route.
        self.public_ip_checker = public_ip_checker
        self.ip_echo_urls = tuple(ip_echo_urls)
        self.require_ip_change = require_ip_change
        # Extra argv options are intentionally supplied by the embedding
        # application, not baked into the reusable manager. This is useful for
        # network-specific settings such as MTU/MSS without shell parsing.
        self.openvpn_options = tuple(openvpn_options)
        # A tunnel can stay established while its routes or upstream internet
        # access are broken. Check direct public IPv4 egress independently of
        # the OpenVPN process watchdog and rotate after consecutive failures.
        self.health_check_interval = max(5.0, health_check_interval)
        self.health_check_failures = max(1, health_check_failures)
        self._lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._health_watchdog_task: asyncio.Task | None = None
        self._startup_retry_task: asyncio.Task | None = None
        self._active_ip: str | None = None
        self._last_error: str | None = None
        self._health_failures = 0
        self._enabled = self.store.runtime()["enabled"]
        # A persisted id is only the last profile used. It is not proof that
        # a tunnel survived an application restart.
        self._active_profile_id = None
        self._active_remote: Remote | None = None
        # Keep runtime files isolated per profile store by default. Hosts that
        # need a different location can provide one explicitly; managers from
        # separate applications must never share a fixed temp directory.
        self._runtime_dir = runtime_dir or (root / ".runtime")
        self._runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    async def _notify(self, active: bool) -> None:
        if self.state_callback:
            await self.state_callback(active)

    def list_profiles(self) -> list[ProfileInfo]:
        return self.store.list()

    def add_profile(self, name: str, config_text: str, *, username: str | None = None,
                    password: str | None = None, profile_type: str = "auto",
                    group: str | None = None) -> ProfileInfo:
        return self.store.add(name, config_text, username, password, profile_type, group)

    async def remove_profile(self, profile_id: str) -> None:
        async with self._lock:
            if self._active_profile_id == profile_id:
                await self._disconnect_locked(remove_kill_switch=True)
                self._active_profile_id = None
            self.store.remove(profile_id)

    def set_profile_enabled(self, profile_id: str, enabled: bool) -> ProfileInfo:
        return self.store.set_enabled(profile_id, enabled)

    async def _public_ip(self, timeout: float = 8.0) -> str | None:
        if self.public_ip_checker is not None:
            try:
                return await self.public_ip_checker(timeout)
            except Exception:
                return None

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        async def one(url: str) -> str | None:
            def request() -> str:
                req = urllib.request.Request(url, headers={"User-Agent": "vpn-profile-manager/0.1"})
                with opener.open(req, timeout=timeout) as response:
                    return response.read(2048).decode("ascii", errors="ignore").strip()
            try:
                value = await asyncio.to_thread(request)
                return _parse_public_ipv4(value)
            except Exception:
                return None

        tasks = [asyncio.create_task(one(url)) for url in self.ip_echo_urls]
        try:
            for task in asyncio.as_completed(tasks, timeout=timeout + 2):
                value = await task
                if value:
                    return value
        except (asyncio.TimeoutError, Exception):
            pass
        finally:
            for task in tasks:
                task.cancel()
        return None

    async def _tooling_ok(self) -> tuple[bool, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.openvpn_binary, "--version", stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                return False, err.decode(errors="replace").strip() or "OpenVPN is unavailable."
        except OSError as exc:
            return False, f"OpenVPN is unavailable: {exc}"
        return True, ""

    def _remote_rules(self, profile: ProfileInfo) -> list[tuple[str, int, str]]:
        rules: list[tuple[str, int, str]] = []
        for remote in profile.remotes:
            for address in resolve_host(remote.host):
                rules.append((address, remote.port, remote.protocol))
        return rules

    def _all_remote_rules(self) -> list[tuple[str, int, str]]:
        rules: list[tuple[str, int, str]] = []
        for profile in self.store.enabled_profiles():
            rules.extend(self._remote_rules(profile))
        return rules

    async def _arm_failsafe(self) -> None:
        """Keep egress blocked if every profile fails during rotation."""
        rules = self._all_remote_rules()
        if rules:
            try:
                await self.kill_switch.install(rules)
            except Exception:
                # The caller still reports failure; status will expose whether
                # the firewall could be armed.
                pass

    async def _start_process(self, profile: ProfileInfo, remote: Remote) -> tuple[asyncio.subprocess.Process, asyncio.Event, list[str]]:
        config_text = self.store.read_config(profile.id)
        # Parse again at connection time so a file modified outside the API is
        # never executed without validation.
        parse_config(config_text)
        config_text = config_for_remote(config_text, remote)
        runtime = self._runtime_dir / profile.id
        runtime.mkdir(mode=0o700, exist_ok=True)
        config_path = runtime / "profile.ovpn"
        config_path.write_text(config_text)
        config_path.chmod(0o600)
        status_path = runtime / "status.log"
        args = [
            self.openvpn_binary, "--config", str(config_path),
            "--status", str(status_path), "5",
            "--verb", "3",
            "--auth-nocache", "--redirect-gateway", "def1",
            "--route-delay", "2",
        ]
        args.extend(self.openvpn_options)
        auth = self.store.credentials_path(profile.id)
        if auth:
            args += ["--auth-user-pass", str(auth)]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        ready = asyncio.Event()
        lines: list[str] = []

        async def capture() -> None:
            if proc.stdout is None:
                return
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    return
                line = raw.decode(errors="replace").strip()
                if line:
                    lines.append(line)
                    del lines[:-80]
                    if "Initialization Sequence Completed" in line:
                        ready.set()

        asyncio.create_task(capture())
        return proc, ready, lines

    async def _connect_locked(self, profile: ProfileInfo, remote: Remote) -> ProfileResult:
        ok, message = await self._tooling_ok()
        if not ok:
            return ProfileResult(False, message, profile.id, profile.name, source=remote.as_dict())
        baseline = await self._public_ip()
        rules = self._remote_rules(profile)
        if not rules:
            return ProfileResult(False, "Could not resolve the selected OpenVPN source.", profile.id, profile.name,
                                 source=remote.as_dict())

        await self._disconnect_locked(remove_kill_switch=False)
        try:
            await self.kill_switch.install(rules)
            proc, ready, lines = await self._start_process(profile, remote)
        except Exception as exc:
            await self.kill_switch.remove()
            return ProfileResult(False, f"Could not start OpenVPN: {exc}", profile.id, profile.name,
                                 source=remote.as_dict())
        self._process = proc
        try:
            deadline = asyncio.get_running_loop().time() + 30
            while not ready.is_set() and proc.returncode is None:
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.25)
            if not ready.is_set():
                detail = lines[-1] if lines else "OpenVPN did not finish initialization."
                await self._disconnect_locked(remove_kill_switch=True)
                return ProfileResult(False, detail[:500], profile.id, profile.name,
                                     source=remote.as_dict())
            public_ip = await self._public_ip(timeout=10)
            if not public_ip:
                await self._disconnect_locked(remove_kill_switch=True)
                return ProfileResult(
                    False,
                    "OpenVPN initialized, but direct public IPv4 egress could not be checked.",
                    profile.id, profile.name, public_ip, remote.as_dict(),
                )
            if self.require_ip_change and baseline and public_ip == baseline:
                await self._disconnect_locked(remove_kill_switch=True)
                return ProfileResult(
                    False,
                    f"OpenVPN initialized, but direct public IPv4 egress did not change "
                    f"(before {baseline}, after {public_ip}). The tunnel is up, but "
                    "default traffic is not using it.",
                    profile.id, profile.name, public_ip, remote.as_dict(),
                )
            self._active_profile_id = profile.id
            self._active_remote = remote
            self._active_ip = public_ip
            self._health_failures = 0
            self._last_error = None
            self.store.mark_connected(profile.id)
            self.store.set_runtime(active_profile_id=profile.id, enabled=True)
            await self._notify(True)
            return ProfileResult(True, f"Connected through {profile.name} ({public_ip}).",
                                 profile.id, profile.name, public_ip, remote.as_dict())
        except Exception as exc:
            await self._disconnect_locked(remove_kill_switch=True)
            return ProfileResult(False, f"VPN verification failed: {exc}", profile.id, profile.name,
                                 source=remote.as_dict())

    def _ordered_profiles(self) -> list[ProfileInfo]:
        """Least-recently-used profiles first; randomizes equal-age ties."""
        profiles = self.store.enabled_profiles()
        random.SystemRandom().shuffle(profiles)
        profiles.sort(key=lambda p: (p.last_connected_at is not None,
                                     p.last_connected_at or ""))
        return profiles

    def _candidates(self, *, exclude: tuple[str, str] | None = None,
                    prefer_other_profile: bool = False) -> list[tuple[ProfileInfo, Remote]]:
        candidates: list[tuple[ProfileInfo, Remote]] = []
        profiles = self._ordered_profiles()
        if prefer_other_profile and exclude:
            profiles = [p for p in profiles if p.id != exclude[0]] + [
                p for p in profiles if p.id == exclude[0]
            ]
        for profile in profiles:
            remotes = list(profile.remotes)
            random.SystemRandom().shuffle(remotes)
            for remote in remotes:
                if exclude and (profile.id, remote.key) == exclude:
                    continue
                candidates.append((profile, remote))
        return candidates

    async def connect(self, profile_id: str) -> ProfileResult:
        async with self._lock:
            profile = self.store.get(profile_id)
            if not profile.enabled:
                return ProfileResult(False, "That VPN profile is disabled.", profile.id, profile.name)
            if not profile.remotes:
                return ProfileResult(False, "That VPN profile contains no sources.", profile.id, profile.name)
            remote = random.SystemRandom().choice(profile.remotes)
            return await self._connect_locked(profile, remote)

    async def connect_random(self, *, exclude: set[str] | None = None) -> ProfileResult:
        exclude = exclude or set()
        async with self._lock:
            candidates = self._candidates()
            if exclude:
                candidates = [(p, r) for p, r in candidates if p.id not in exclude]
            if not candidates:
                return ProfileResult(False, "No enabled VPN profiles are available.")
            attempts: list[dict] = []
            for profile, remote in candidates:
                result = await self._connect_locked(profile, remote)
                attempts.append({"profile_id": profile.id, "profile_name": profile.name,
                                 "source": remote.as_dict(),
                                 "ok": result.ok, "message": result.message})
                if result.ok:
                    result.attempts = attempts
                    return result
                self.store.mark_error(profile.id, result.message)
            await self._arm_failsafe()
            return ProfileResult(False, "All enabled VPN profiles failed.", attempts=attempts)

    async def failover(self, reason: str = "VPN health check failed",
                       expected_process: asyncio.subprocess.Process | None = None) -> ProfileResult:
        async with self._recovery_lock:
            if not self._enabled:
                return ProfileResult(False, "VPN rotation is disabled.")
            if expected_process is not None and self._process is not expected_process:
                return ProfileResult(False, "VPN was already rotated.")
            async with self._lock:
                # Gate downloads before tearing down the old tunnel. A
                # successful replacement calls _notify(True) below.
                await self._notify(False)
                current = self._active_profile_id
                if current:
                    self.store.mark_error(current, reason)
                excluded = (current, self._active_remote.key) if current and self._active_remote else None
                # Remove the known-bad tunnel before probing alternatives. Keep
                # the kill switch armed so downloads cannot escape between
                # profiles, and ensure an exhausted rotation can retry cleanly.
                await self._disconnect_locked(remove_kill_switch=False)
                candidates = self._candidates(exclude=excluded, prefer_other_profile=True)
                attempts: list[dict] = []
                for profile, remote in candidates:
                    result = await self._connect_locked(profile, remote)
                    attempts.append({"profile_id": profile.id, "profile_name": profile.name,
                                     "source": remote.as_dict(),
                                     "ok": result.ok, "message": result.message})
                    if result.ok:
                        result.attempts = attempts
                        self._start_watchdogs()
                        return result
                    self.store.mark_error(profile.id, result.message)
                self._last_error = reason
                # Keep the desired state enabled so a transient outage is
                # retried automatically instead of leaving the queue gated.
                self._enabled = True
                self.store.set_runtime(enabled=True, clear_active=True)
                await self._arm_failsafe()
                await self._notify(False)
                if self._startup_retry_task is None or self._startup_retry_task.done():
                    self._startup_retry_task = asyncio.create_task(self._retry_startup())
                return ProfileResult(False, "VPN failover exhausted every enabled profile.", attempts=attempts)

    async def _disconnect_locked(self, *, remove_kill_switch: bool) -> None:
        proc = self._process
        self._process = None
        if proc and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), timeout=8)
            except (ProcessLookupError, asyncio.TimeoutError):
                if proc.returncode is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        if remove_kill_switch:
            await self.kill_switch.remove()
        self._active_ip = None
        self._active_profile_id = None
        self._active_remote = None
        if remove_kill_switch:
            # Keep the desired enabled state separate from the live process.
            # A failed connection or a clean application reload should not
            # silently turn off automatic reconnect on the next boot.
            self.store.set_runtime(clear_active=True, enabled=self._enabled)
            await self._notify(False)

    async def disconnect(self) -> None:
        async with self._lock:
            self._enabled = False
            await self._disconnect_locked(remove_kill_switch=True)

    async def _watchdog(self) -> None:
        while self._enabled:
            proc = self._process
            if proc is None:
                await asyncio.sleep(1)
                continue
            await proc.wait()
            if not self._enabled:
                return
            # A health-triggered rotation may have already replaced this
            # process. Do not start a second failover for the old process.
            if self._process is not proc:
                continue
            reason = "OpenVPN process exited unexpectedly."
            self._last_error = reason
            await self._recover(reason, expected_process=proc)

    async def _health_watchdog(self) -> None:
        """Rotate a live tunnel that no longer carries internet traffic."""
        while self._enabled:
            await asyncio.sleep(self.health_check_interval)
            if not self._enabled:
                return
            proc = self._process
            if proc is None or proc.returncode is not None or not self._active_profile_id:
                self._health_failures = 0
                continue

            public_ip = await self._public_ip(timeout=min(10.0, self.health_check_interval))
            if public_ip:
                self._active_ip = public_ip
                self._health_failures = 0
                continue

            self._health_failures += 1
            if self._health_failures < self.health_check_failures:
                continue

            self._health_failures = 0
            reason = "VPN tunnel is up, but direct internet connectivity failed."
            self._last_error = reason
            await self._recover(reason, expected_process=proc)

    async def _recover(self, reason: str,
                       expected_process: asyncio.subprocess.Process | None = None) -> ProfileResult | None:
        """Handle a process or health failure through the serialized rotator."""
        if not self._enabled:
            return None
        if self.failure_callback:
            await self.failure_callback(reason)
        return await self.failover(reason, expected_process=expected_process)

    def _start_watchdogs(self) -> None:
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog())
        if self._health_watchdog_task is None or self._health_watchdog_task.done():
            self._health_watchdog_task = asyncio.create_task(self._health_watchdog())

    async def enable_random(self) -> ProfileResult:
        async with self._lock:
            if self._startup_retry_task and not self._startup_retry_task.done():
                self._startup_retry_task.cancel()
                self._startup_retry_task = None
            self._enabled = True
            self.store.set_runtime(enabled=True)
            result = await self._connect_random_locked()
            if result.ok:
                self._start_watchdogs()
            return result

    async def _connect_random_locked(self, *, keep_enabled_on_failure: bool = False) -> ProfileResult:
        candidates = self._candidates()
        attempts: list[dict] = []
        if not candidates:
            message = "No included VPN profiles are available."
            self._last_error = message
            self._enabled = keep_enabled_on_failure
            self.store.set_runtime(clear_active=True, enabled=keep_enabled_on_failure)
            await self._arm_failsafe()
            await self._notify(False)
            return ProfileResult(False, message)
        for profile, remote in candidates:
            result = await self._connect_locked(profile, remote)
            attempts.append({"profile_id": profile.id, "profile_name": profile.name,
                             "source": remote.as_dict(),
                             "ok": result.ok, "message": result.message})
            if result.ok:
                result.attempts = attempts
                return result
            self.store.mark_error(profile.id, result.message)
            self._last_error = result.message
        self._enabled = keep_enabled_on_failure
        self.store.set_runtime(clear_active=True, enabled=keep_enabled_on_failure)
        await self._arm_failsafe()
        await self._notify(False)
        return ProfileResult(False, "All enabled VPN profiles failed.", attempts=attempts)

    async def _retry_startup(self) -> None:
        """Retry a configured VPN after a transient boot/network failure."""
        while self._enabled and self.store.runtime()["enabled"] and not self._active_profile_id:
            await asyncio.sleep(10)
            if not self._enabled or not self.store.runtime()["enabled"] or self._active_profile_id:
                return
            async with self._lock:
                if self._active_profile_id:
                    return
                result = await self._connect_random_locked(keep_enabled_on_failure=True)
                if result.ok:
                    self._start_watchdogs()
                    return

    async def start_on_boot(self) -> ProfileResult | None:
        runtime = self.store.runtime()
        if not runtime["enabled"]:
            await self._notify(False)
            return None
        self._enabled = True
        async with self._lock:
            result = await self._connect_random_locked(keep_enabled_on_failure=True)
        if result.ok:
            self._start_watchdogs()
        elif not result.ok and self._enabled and self.store.runtime()["enabled"]:
            if self._startup_retry_task is None or self._startup_retry_task.done():
                self._startup_retry_task = asyncio.create_task(self._retry_startup())
        return result

    async def shutdown(self) -> None:
        """Stop child processes when the host application is reloaded/stopped."""
        desired = self.store.runtime()["enabled"]
        self._enabled = False
        for task in (self._startup_retry_task, self._watchdog_task, self._health_watchdog_task):
            if task and not task.done() and task is not asyncio.current_task():
                task.cancel()
        self._startup_retry_task = None
        self._watchdog_task = None
        self._health_watchdog_task = None
        async with self._lock:
            await self._disconnect_locked(remove_kill_switch=True)
            if desired:
                self.store.set_runtime(enabled=True)

    async def status(self) -> ProfileStatus:
        runtime = self.store.runtime()
        running = bool(self._process and self._process.returncode is None and self._active_profile_id)
        profiles = self.store.list()
        has_candidates = any(profile.enabled for profile in profiles)
        connection_state = "connected" if running else "connecting" if runtime["enabled"] and has_candidates else "off"
        active = None
        if self._active_profile_id:
            try:
                active = self.store.get(self._active_profile_id)
            except OpenVPNConfigError:
                active = None
        return ProfileStatus(
            configured=bool(self.store.list()), enabled=bool(runtime["enabled"]),
            running=running, connection_state=connection_state,
            active_profile_id=self._active_profile_id,
            active_profile_name=active.name if active else None,
            active_source=self._active_remote.as_dict() if self._active_remote else None,
            public_ip=self._active_ip, kill_switch=await self.kill_switch.active(),
            last_error=self._last_error, profiles=profiles,
        )
