from __future__ import annotations

import asyncio
import ipaddress
import re
from typing import Iterable


_DEFAULT_ALLOWED_NETWORKS = (
    "127.0.0.0/8",   # local/Docker DNS resolvers
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)


class IptablesKillSwitch:
    """Small container-local OUTPUT kill switch for a full-tunnel VPN."""

    def __init__(self, chain: str = "VPN_PROFILES_KS", tunnel_interface: str = "tun0",
                 allowed_networks: Iterable[str] = _DEFAULT_ALLOWED_NETWORKS):
        if not re.fullmatch(r"[A-Z0-9_]{1,28}", chain):
            raise ValueError("Invalid iptables chain name")
        self.chain = chain
        self.tunnel_interface = tunnel_interface
        self.allowed_networks = tuple(allowed_networks)

    async def _run(self, *args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "iptables", *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
            return 124, out.decode(errors="replace"), "iptables command timed out"
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def active(self) -> bool:
        rc, _, _ = await self._run("-C", "OUTPUT", "-j", self.chain)
        return rc == 0

    async def remove(self) -> None:
        for _ in range(4):
            rc, _, _ = await self._run("-D", "OUTPUT", "-j", self.chain)
            if rc != 0:
                break
        await self._run("-F", self.chain)
        await self._run("-X", self.chain)

    async def install(self, remote_addresses: Iterable[tuple[str, int, str]]) -> None:
        await self.remove()
        rc, _, err = await self._run("-N", self.chain)
        if rc != 0:
            raise RuntimeError(f"Could not create VPN kill-switch chain: {err.strip()}")

        async def add(*args: str) -> None:
            rc2, _, err2 = await self._run("-A", self.chain, *args)
            if rc2 != 0:
                raise RuntimeError(f"Could not install VPN kill-switch rule: {err2.strip()}")

        await add("-o", "lo", "-j", "ACCEPT")
        await add("-o", self.tunnel_interface, "-j", "ACCEPT")
        await add("-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT")
        for subnet in self.allowed_networks:
            await add("-d", subnet, "-j", "ACCEPT")
        seen: set[tuple[str, int, str]] = set()
        for address, port, protocol in remote_addresses:
            try:
                ipaddress.ip_address(address)
            except ValueError:
                continue
            rule = (address, port, protocol)
            if rule in seen:
                continue
            seen.add(rule)
            await add("-p", protocol, "-d", address, "--dport", str(port), "-j", "ACCEPT")
        await add("-j", "DROP")
        rc, _, err = await self._run("-I", "OUTPUT", "1", "-j", self.chain)
        if rc != 0:
            await self.remove()
            raise RuntimeError(f"Could not activate VPN kill switch: {err.strip()}")
