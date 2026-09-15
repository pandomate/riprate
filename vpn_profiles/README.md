# VPN Profile Manager

vpn_profile_manager is a dependency-light, application-agnostic OpenVPN
profile pool for server applications. RipRate uses this package for its
Settings → VPN OpenVPN workflow, but the package can also be embedded in other
services.

## Capabilities

- securely store validated .ovpn profiles and optional credentials;
- parse every remote entry in a profile into a managed source;
- select and start one profile/source inside the current network namespace;
- verify the tunnel by checking public IPv4 egress;
- install a container-local iptables kill switch;
- enable, disable, remove, and list profiles;
- choose enabled profiles without immediately repeating the active profile;
- fail over through remaining enabled profiles after tunnel failure;
- periodically verify direct internet egress and rotate when a live tunnel
  stops carrying traffic;
- notify the host application when the tunnel becomes usable or unusable.

The manager does not bypass provider access controls or browser challenges. It
only changes the network path used by the host application.

## Requirements

The host or container must provide:

- Python 3.11 or newer;
- openvpn;
- iptables;
- /dev/net/tun;
- permission to create the tunnel and firewall rules, normally NET_ADMIN.

The package has no Python runtime dependencies. OpenVPN itself remains an
external system dependency.

## Install for local development

From this directory:

~~~bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
~~~

## Basic usage

~~~python
from pathlib import Path

from vpn_profile_manager import OpenVPNProfileManager


manager = OpenVPNProfileManager(Path("./data/vpn-profiles"))
manager.add_profile(
    "proton-us",
    Path("us.ovpn").read_text(),
    profile_type="country",
)

result = await manager.connect_random()
if not result.ok:
    print(result.message)
~~~

Egress verification is configurable for host applications. By default the
manager uses direct HTTP requests (environment proxies are bypassed) to its
built-in IPv4 echo endpoints and requires the exit IP to differ from the
pre-tunnel IP. Embedders can provide `public_ip_checker`, `ip_echo_urls`, or
set `require_ip_change=False` when their network or VPN design needs another
verification policy. Runtime files are isolated under the profile store by
default; use `runtime_dir` to place them elsewhere. The default kill switch
also permits loopback destinations for local/Docker DNS; applications with a
different network layout can inject `IptablesKillSwitch` with their own
`allowed_networks`. Network-specific OpenVPN flags can be passed through
`openvpn_options` as an argument list.

The manager's health watchdog checks direct public IPv4 egress at the
configured `health_check_interval` (30 seconds by default). It rotates after
two consecutive failures by default (`health_check_failures`), keeps the
kill-switch active while trying alternatives, and retries in the background if
all enabled sources are temporarily unavailable.

add_profile() accepts optional username and password arguments for profiles
that use separate credentials. The manager stores credentials with the
profile and does not include them in ProfileInfo status data.

Applications can call:

~~~python
await manager.failover("VPN health check failed")
~~~

The manager serializes connection transitions. The host application should
pause work while a transition is in progress and resume it after the state
callback reports a usable tunnel.

## Profiles and remotes

An uploaded file is one profile. If it contains multiple remote directives,
each directive becomes a selectable source with its own host, port, and
protocol. Uploading several files creates several profiles.

profile_type may be:

- country for a profile that represents a country or bundle;
- individual for one server/profile;
- auto to infer the label from the number of remotes.

The type and optional group are metadata for the host application and do not
alter the provider's OpenVPN configuration.

## Safety behavior

The manager validates profile structure before storage, restricts profile data
permissions, and keeps private configuration out of status responses. When a
tunnel is enabled, the kill switch allows the VPN's resolved remote endpoints
and the tunnel traffic required for the active connection. If the tunnel
fails, the manager removes or replaces the active connection according to the
requested operation while reporting the failure to the host application.

RipRate runs this package in the backend container with NET_ADMIN and
/dev/net/tun configured in docker-compose.yml. Other applications must provide
equivalent privileges themselves.
