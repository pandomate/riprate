from __future__ import annotations

import ipaddress
import re
from .models import OpenVPNConfigError, Remote

_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$")
_SCRIPT_DIRECTIVES = {
    "askpass", "auth-user-pass", "client-connect", "client-disconnect",
    "down", "down-pre", "ipchange", "iproute", "route-up", "tls-verify",
    "up", "up-delay", "up-restart", "plugin", "management-external-key",
}
_DEFAULT_PORT = 1194


def validate_name(name: str) -> str:
    value = name.strip()
    if not _PROFILE_NAME.fullmatch(value):
        raise OpenVPNConfigError(
            "Profile names may contain letters, numbers, spaces, dots, dashes, and underscores."
        )
    return value


def _clean_value(value: str) -> str:
    return value.strip().strip('"').strip("'")


def parse_config(text: str) -> list[Remote]:
    """Validate a provider profile and extract its remote endpoints.

    OpenVPN profiles are executable configuration, so directives that run
    arbitrary local programs are rejected. Inline certificates/keys remain
    supported because Proton and other providers commonly embed them.
    """
    if not text or len(text.encode("utf-8")) > 512_000:
        raise OpenVPNConfigError("The OpenVPN profile is empty or larger than 512 KB.")
    sections: list[str] = []
    remotes: list[Remote] = []
    has_client = False
    auth_without_value = False
    inline_depth = 0
    default_protocol = "udp"

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("<") and line.endswith(">"):
            tag = line[1:-1].strip().lower()
            if tag.startswith("/"):
                inline_depth = max(0, inline_depth - 1)
            else:
                inline_depth += 1
            continue
        if inline_depth:
            continue
        parts = line.split()
        directive = parts[0].lower()
        if directive == "client":
            has_client = True
        if directive == "proto" and len(parts) >= 2:
            candidate = parts[1].lower()
            if candidate in {"udp", "udp4", "udp6"}:
                default_protocol = "udp"
            elif candidate in {"tcp", "tcp-client", "tcp4", "tcp6"}:
                default_protocol = "tcp"
        if directive in _SCRIPT_DIRECTIVES:
            if directive == "auth-user-pass" and len(parts) == 1:
                auth_without_value = True
                continue
            # auth-user-pass with a path is also rejected: uploaded configs
            # must not make the daemon read arbitrary paths in the container.
            if directive == "auth-user-pass":
                raise OpenVPNConfigError(
                    "auth-user-pass must not reference a file; enter credentials in the profile form."
                )
            raise OpenVPNConfigError(
                f"The '{directive}' directive is not allowed in uploaded profiles."
            )
        if directive == "remote":
            if len(parts) < 2 or len(parts) > 4:
                raise OpenVPNConfigError("Each remote line must be: remote host [port] [tcp|udp].")
            host = _clean_value(parts[1])
            if not host or any(c in host for c in "\r\n/\\"):
                raise OpenVPNConfigError("A remote endpoint has an invalid host.")
            port = _DEFAULT_PORT
            if len(parts) >= 3:
                try:
                    port = int(parts[2])
                except ValueError as exc:
                    raise OpenVPNConfigError(f"Invalid remote port: {parts[2]}") from exc
            if not 1 <= port <= 65535:
                raise OpenVPNConfigError("Remote ports must be between 1 and 65535.")
            protocol = parts[3].lower() if len(parts) == 4 else default_protocol
            if protocol not in {"udp", "tcp", "udp4", "tcp4", "udp6", "tcp6"}:
                raise OpenVPNConfigError(f"Unsupported OpenVPN protocol: {protocol}")
            remotes.append(Remote(host, port, protocol[:3]))
        if directive in {"route", "route-ipv6", "redirect-gateway", "redirect-private"}:
            # Routes are allowed; the manager additionally forces a full-tunnel
            # route at launch to keep application traffic inside the VPN.
            continue

    if not has_client:
        raise OpenVPNConfigError("The profile must contain the 'client' directive.")
    if not remotes:
        raise OpenVPNConfigError("The profile must contain at least one remote endpoint.")
    # `auth-user-pass` with no argument is the normal Proton format. The
    # manager requires credentials at connect time for that profile.
    if auth_without_value:
        return remotes
    return remotes


def config_for_remote(text: str, remote: Remote) -> str:
    """Return a safe copy of a profile pinned to one parsed remote.

    Proton profiles commonly contain several ``remote`` lines. OpenVPN can
    fail over those itself, but applications need to know which source was
    selected and rotate it deliberately. Removing the original remote lines
    and adding one selected endpoint makes that choice deterministic while
    preserving certificates, auth, routes, and all other provider settings.
    """
    output: list[str] = []
    inline_depth = 0
    removed = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("<") and line.endswith(">"):
            tag = line[1:-1].strip().lower()
            inline_depth = max(0, inline_depth - 1) if tag.startswith("/") else inline_depth + 1
            output.append(raw)
            continue
        if inline_depth == 0 and line and not line.startswith(("#", ";")):
            if line.split()[0].lower() == "remote":
                removed = True
                continue
        output.append(raw)
    if not removed:
        raise OpenVPNConfigError("Selected source was not found in the OpenVPN profile.")
    output.append("")
    output.append(f"remote {remote.host} {remote.port} {remote.protocol}")
    return "\n".join(output).rstrip() + "\n"


def redact_config(text: str) -> str:
    """Return a safe diagnostic copy without private key/certificate bodies."""
    out: list[str] = []
    inline = False
    for raw in text.splitlines():
        line = raw.strip().lower()
        if line.startswith("<") and line.endswith(">"):
            tag = line[1:-1].strip()
            inline = not tag.startswith("/")
            out.append("<redacted>")
            continue
        if inline:
            continue
        if any(line.startswith(k) for k in ("private-key", "secret", "askpass")):
            out.append("<redacted>")
        else:
            out.append(raw)
    return "\n".join(out)


def resolve_host(host: str) -> list[str]:
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    import socket
    try:
        return sorted({item[4][0] for item in socket.getaddrinfo(host, None, socket.AF_INET)})
    except socket.gaierror:
        return []
