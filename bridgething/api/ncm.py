"""USB CDC-NCM bring-up.

The Car Thing is the DHCP server on a /29 inside 10.42.1.0/24 and does not
advertise a router. The host takes a lease and keeps its existing default route.
"""

from __future__ import annotations

import ipaddress
import re
import shutil
import subprocess
from typing import Callable

NCM_NETWORK = ipaddress.ip_network("10.42.1.0/24")
GATEWAY_PORT = 8892
CLIENT_PORT = 8891
PROFILE_NAME = "bridgething-ncm"
_ADDR_RE = re.compile(r"inet (10\.42\.1\.\d+)/(\d+)")

NM_PROFILE = """[connection]
id=bridgething-ncm
type=ethernet
autoconnect=true
match-device=driver:cdc_ncm

[ipv4]
method=auto
never-default=true
ignore-auto-dns=true

[ipv6]
method=disabled
"""

DHCLIENT_HOOK = """#!/bin/sh
# The Car Thing must not replace the host default route or DNS.
case "$reason" in
  BOUND|RENEW|REBIND|REBOOT)
    new_routers=""
    new_domain_name_servers=""
    ;;
esac
"""


def nmcli_add_args() -> list[str]:
    return [
        "nmcli",
        "connection",
        "add",
        "type",
        "ethernet",
        "con-name",
        PROFILE_NAME,
        "connection.autoconnect",
        "yes",
        "match.driver",
        "cdc_ncm",
        "ipv4.method",
        "auto",
        "ipv4.never-default",
        "yes",
        "ipv4.ignore-auto-dns",
        "yes",
        "ipv6.method",
        "disabled",
    ]


def nmcli_modify_args() -> list[str]:
    return [
        "nmcli",
        "connection",
        "modify",
        PROFILE_NAME,
        "connection.autoconnect",
        "yes",
        "match.driver",
        "cdc_ncm",
        "ipv4.method",
        "auto",
        "ipv4.never-default",
        "yes",
        "ipv4.ignore-auto-dns",
        "yes",
        "ipv6.method",
        "disabled",
    ]


def cdc_interfaces(entries: list[tuple[str, str | None]]) -> list[str]:
    found: list[str] = []
    for name, driver in entries:
        if driver == "cdc_ncm" or name.startswith("uncm"):
            if name not in found:
                found.append(name)
    return found


def local_ncm_addresses(addr_text: str) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for match in _ADDR_RE.finditer(addr_text):
        address = match.group(1)
        prefix = int(match.group(2))
        try:
            interface = ipaddress.ip_interface(f"{address}/{prefix}")
        except ValueError:
            continue
        if interface.ip in NCM_NETWORK:
            found.append((address, prefix))
    return found


def candidate_gateways(addresses: list[tuple[str, int]]) -> list[str]:
    hosts: list[str] = ["bridgething.local"]
    seen = set(hosts)
    for address, prefix in addresses:
        interface = ipaddress.ip_interface(f"{address}/{prefix}")
        network = interface.network
        if network.prefixlen < 28:
            network = ipaddress.ip_network(f"{address}/29", strict=False)
        for host in network.hosts():
            text = str(host)
            if text != address and text not in seen and host in NCM_NETWORK:
                seen.add(text)
                hosts.append(text)
    return hosts


def read_sysfs_interfaces(sysfs_net: str = "/sys/class/net") -> list[tuple[str, str | None]]:
    import os

    entries: list[tuple[str, str | None]] = []
    try:
        names = os.listdir(sysfs_net)
    except OSError:
        return entries
    for name in names:
        driver = os.path.join(sysfs_net, name, "device", "driver")
        resolved: str | None = None
        if os.path.islink(driver):
            resolved = os.path.basename(os.readlink(driver))
        entries.append((name, resolved))
    return entries


def ensure_profile(run: Callable[[list[str]], int] | None = None) -> str:
    """Install the NetworkManager profile. Returns 'added', 'modified', or 'unavailable'."""
    if run is None:
        if shutil.which("nmcli") is None:
            return "unavailable"

        def run(args: list[str]) -> int:
            completed = subprocess.run(args, capture_output=True, text=True)
            return completed.returncode

    show = run(["nmcli", "-g", "NAME", "connection", "show"])
    # `show` returning non-zero means NetworkManager is not reachable.
    if show != 0:
        return "unavailable"
    exists = run(["nmcli", "connection", "show", PROFILE_NAME]) == 0
    if exists:
        run(nmcli_modify_args())
        return "modified"
    if run(nmcli_add_args()) != 0:
        return "unavailable"
    return "added"


def bring_up(name: str, run: Callable[[list[str]], int] | None = None) -> None:
    if run is None:

        def run(args: list[str]) -> int:
            return subprocess.run(args, capture_output=True).returncode

    run(["ip", "link", "set", name, "up"])
    if shutil.which("nmcli") is not None:
        if run(["nmcli", "device", "connect", name]) == 0:
            return
    if shutil.which("dhclient") is not None:
        run(["dhclient", "-1", "-q", "-pf", f"/tmp/bridgething-dhclient-{name}.pid", name])


def install_dhclient_hook(path: str = "/etc/dhcp/dhclient-enter-hooks.d/bridgething-ncm") -> None:
    import os

    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(DHCLIENT_HOOK.replace("\r\n", "\n").replace("\r", "\n"))
        os.chmod(path, 0o755)
    except OSError:
        return
