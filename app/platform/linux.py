"""
c69-router Platform - Linux Implementation

Uses standard Linux networking tools:
  - ip / iproute2   : interface detection, IP assignment, routing
  - iptables        : NAT (MASQUERADE), firewall
  - sysctl          : IP forwarding
  - hostapd         : WiFi hotspot (if available)
  - systemd         : startup service

Requires: running as root (sudo) or with CAP_NET_ADMIN.
"""

import os
import re
import sys
import time
import logging
import subprocess
import socket
import struct
import platform
import urllib.request
import tarfile
import io

logger = logging.getLogger(__name__)

# Detect architecture for binary download
_ARCH_MAP = {
    "x86_64": "amd64",
    "aarch64": "arm64",
    "armv7l": "armv7",
}
_MACHINE = platform.machine()
_ARCH = _ARCH_MAP.get(_MACHINE, "amd64")

# Path helpers
def _run(cmd: list, timeout: int = 15, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)

def _run_shell(cmd: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


# ─── Admin Check ─────────────────────────────────────────────────────────────

def check_admin_elevation() -> bool:
    """Return True if running as root."""
    return os.geteuid() == 0


# ─── IP Forwarding ───────────────────────────────────────────────────────────

def enable_ip_forwarding(lan_interface: str = "") -> None:
    """Enable IPv4 forwarding via sysctl."""
    try:
        _run(["sysctl", "-w", "net.ipv4.ip_forward=1"])
        # Make persistent
        conf = "/etc/sysctl.d/99-c69router.conf"
        with open(conf, "w") as f:
            f.write("net.ipv4.ip_forward=1\n")
        logger.info("[Network/Linux] IP forwarding enabled.")
    except Exception as e:
        logger.error(f"[Network/Linux] enable_ip_forwarding failed: {e}")


def ensure_wan_forwarding_disabled(wan_interface: str) -> None:
    """No-op on Linux — iptables FORWARD rules handle this selectively."""
    logger.debug(f"[Network/Linux] ensure_wan_forwarding_disabled: no-op for {wan_interface}")


# ─── Firewall (iptables) ─────────────────────────────────────────────────────

def setup_firewall_rules(lan_interface: str = "", lan_ip: str = "192.168.10.1") -> None:
    """Open required ports via iptables."""
    rules = [
        # DHCP
        ["iptables", "-C", "INPUT", "-p", "udp", "--dport", "67", "-j", "ACCEPT"],
        # DNS
        ["iptables", "-C", "INPUT", "-p", "udp", "--dport", "53", "-j", "ACCEPT"],
        ["iptables", "-C", "INPUT", "-p", "tcp", "--dport", "53", "-j", "ACCEPT"],
        # Web UI
        ["iptables", "-C", "INPUT", "-p", "tcp", "--dport", "80", "-j", "ACCEPT"],
        ["iptables", "-C", "INPUT", "-p", "tcp", "--dport", "9000", "-j", "ACCEPT"],
        # Captive portal proxy
        ["iptables", "-C", "INPUT", "-p", "tcp", "--dport", "9001", "-j", "ACCEPT"],
    ]
    add_cmds = [
        ["iptables", "-I", "INPUT", "-p", "udp", "--dport", "67", "-j", "ACCEPT"],
        ["iptables", "-I", "INPUT", "-p", "udp", "--dport", "53", "-j", "ACCEPT"],
        ["iptables", "-I", "INPUT", "-p", "tcp", "--dport", "53", "-j", "ACCEPT"],
        ["iptables", "-I", "INPUT", "-p", "tcp", "--dport", "80", "-j", "ACCEPT"],
        ["iptables", "-I", "INPUT", "-p", "tcp", "--dport", "9000", "-j", "ACCEPT"],
        ["iptables", "-I", "INPUT", "-p", "tcp", "--dport", "9001", "-j", "ACCEPT"],
    ]
    for check_cmd, add_cmd in zip(rules, add_cmds):
        r = _run(check_cmd)
        if r.returncode != 0:
            _run(add_cmd)
    logger.info("[Network/Linux] Firewall rules applied via iptables.")


# ─── Interface Detection ─────────────────────────────────────────────────────

def detect_wan_interface() -> str:
    """Parse 'ip route show default' to find default gateway interface."""
    try:
        r = _run(["ip", "route", "show", "default"])
        # "default via 192.168.1.1 dev eth0 proto dhcp src ..."
        m = re.search(r"default.*\bdev\s+(\S+)", r.stdout)
        if m:
            iface = m.group(1)
            logger.info(f"[Network/Linux] WAN interface detected: {iface}")
            return iface
    except Exception as e:
        logger.error(f"[Network/Linux] detect_wan_interface error: {e}")
    return "eth0"


def detect_lan_interface(exclude_interface: str = "") -> str:
    """Return first UP non-loopback, non-WAN interface."""
    try:
        r = _run(["ip", "-br", "link", "show"])
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            name = parts[0]
            state = parts[1] if len(parts) > 1 else ""
            if name == "lo":
                continue
            if name == exclude_interface:
                continue
            if state == "UP":
                return name
        # Fallback: return any non-lo, non-WAN interface
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) < 1:
                continue
            name = parts[0]
            if name != "lo" and name != exclude_interface:
                return name
    except Exception as e:
        logger.error(f"[Network/Linux] detect_lan_interface error: {e}")
    return "eth1"


def smart_detect_lan(exclude_interface: str = "") -> tuple:
    """Return (lan_interface, lan_type) for the best available LAN."""
    # Prefer physical Ethernet, then WiFi, then hotspot virtual
    try:
        r = _run(["ip", "-br", "link", "show"])
        eth_candidates = []
        wifi_candidates = []
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) < 1:
                continue
            name = parts[0]
            if name == "lo" or name == exclude_interface:
                continue
            # Check if it's a wireless interface
            wifi_check = _run(["iwconfig", name], timeout=3)
            if wifi_check.returncode == 0 and "no wireless extensions" not in wifi_check.stderr:
                wifi_candidates.append(name)
            else:
                eth_candidates.append(name)
        if eth_candidates:
            return eth_candidates[0], "ethernet"
        if wifi_candidates:
            return wifi_candidates[0], "wifi"
    except Exception as e:
        logger.error(f"[Network/Linux] smart_detect_lan error: {e}")
    lan = detect_lan_interface(exclude_interface)
    return lan, "ethernet"


def is_wan_interface_valid(interface_name: str) -> bool:
    """Check if interface has a default route."""
    try:
        r = _run(["ip", "route", "show", "default", "dev", interface_name])
        return bool(r.stdout.strip())
    except Exception:
        return False


def is_lan_interface_valid(interface_name: str, wan_interface: str = "",
                            lan_gateway_ip: str = "192.168.10.1") -> bool:
    """Return True if interface is UP and not the WAN."""
    if not interface_name or interface_name == wan_interface or interface_name == "lo":
        return False
    try:
        r = _run(["ip", "-br", "link", "show", interface_name])
        return "UP" in r.stdout or "UNKNOWN" in r.stdout
    except Exception:
        return False


def detect_usb_wifi_adapter(exclude_wan_interface: str = "") -> dict:
    """Detect secondary WiFi adapter (not WAN)."""
    try:
        r = _run(["ip", "-br", "link", "show"])
        for line in r.stdout.splitlines():
            name = line.split()[0] if line.split() else ""
            if not name or name == "lo" or name == exclude_wan_interface:
                continue
            w = _run(["iwconfig", name], timeout=3)
            if w.returncode == 0 and "no wireless extensions" not in w.stderr:
                return {"found": True, "name": name, "description": f"WiFi adapter {name}"}
    except Exception as e:
        logger.debug(f"[Network/Linux] detect_usb_wifi_adapter: {e}")
    return {"found": False, "name": "", "description": ""}


# ─── IP Assignment ───────────────────────────────────────────────────────────

def _mask_to_prefix(mask: str) -> int:
    """Convert subnet mask to prefix length."""
    return sum(bin(int(x)).count("1") for x in mask.split("."))


def get_lan_ip(interface_name: str) -> str:
    """Return current IPv4 of interface."""
    try:
        r = _run(["ip", "-4", "addr", "show", interface_name])
        m = re.search(r"inet\s+([\d.]+)/", r.stdout)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def ensure_lan_ip_assigned(lan_interface: str, lan_gateway_ip: str,
                            subnet_mask: str = "255.255.255.0") -> bool:
    """Assign static IP to LAN interface if not already set."""
    prefix = _mask_to_prefix(subnet_mask)
    cidr = f"{lan_gateway_ip}/{prefix}"
    current = get_lan_ip(lan_interface)
    if current == lan_gateway_ip:
        logger.info(f"[Network/Linux] {lan_interface} already has {lan_gateway_ip}")
        return True
    try:
        # Remove existing IPs on this interface
        r = _run(["ip", "-4", "addr", "show", lan_interface])
        for m in re.finditer(r"inet\s+([\d.]+/\d+)", r.stdout):
            _run(["ip", "addr", "del", m.group(1), "dev", lan_interface])
        # Assign new IP
        _run(["ip", "addr", "add", cidr, "dev", lan_interface], check=True)
        _run(["ip", "link", "set", lan_interface, "up"], check=True)
        logger.info(f"[Network/Linux] Assigned {cidr} to {lan_interface}")
        return True
    except Exception as e:
        logger.error(f"[Network/Linux] ensure_lan_ip_assigned failed: {e}")
        return False


def setup_interface_dns(interface_name: str, dns_servers: list) -> None:
    """Set DNS via resolv.conf (simple approach; systemd-resolved may override)."""
    try:
        # Try resolvconf if available
        r = _run(["which", "resolvconf"])
        if r.returncode == 0:
            dns_input = "\n".join(f"nameserver {s}" for s in dns_servers)
            subprocess.run(
                ["resolvconf", "-a", interface_name],
                input=dns_input, text=True, capture_output=True
            )
            return
        # Fallback: write resolv.conf directly
        with open("/etc/resolv.conf", "w") as f:
            for s in dns_servers:
                f.write(f"nameserver {s}\n")
        logger.info(f"[Network/Linux] DNS set: {dns_servers}")
    except Exception as e:
        logger.warning(f"[Network/Linux] setup_interface_dns: {e}")


def detect_wan_subnet() -> str:
    """Return subnet of default route interface."""
    try:
        iface = detect_wan_interface()
        r = _run(["ip", "-4", "addr", "show", iface])
        m = re.search(r"inet\s+([\d.]+/\d+)", r.stdout)
        if m:
            import ipaddress
            net = ipaddress.ip_interface(m.group(1)).network
            return str(net)
    except Exception as e:
        logger.debug(f"[Network/Linux] detect_wan_subnet: {e}")
    return "192.168.1.0/24"


# ─── NAT (iptables) ──────────────────────────────────────────────────────────

def setup_nat(wan_interface: str, lan_subnet: str = "192.168.10.0/24") -> None:
    """Setup iptables NAT MASQUERADE."""
    try:
        cmds = [
            # NAT masquerade
            ["iptables", "-t", "nat", "-C", "POSTROUTING", "-s", lan_subnet,
             "-o", wan_interface, "-j", "MASQUERADE"],
        ]
        add_cmds = [
            ["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", lan_subnet,
             "-o", wan_interface, "-j", "MASQUERADE"],
            # Allow forwarding from LAN
            ["iptables", "-A", "FORWARD", "-s", lan_subnet, "-j", "ACCEPT"],
            ["iptables", "-A", "FORWARD", "-d", lan_subnet, "-m", "state",
             "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
        ]
        r = _run(cmds[0])
        if r.returncode != 0:
            for cmd in add_cmds:
                _run(cmd)
            logger.info(f"[Network/Linux] NAT MASQUERADE setup: {lan_subnet} -> {wan_interface}")
        else:
            logger.info(f"[Network/Linux] NAT already active for {lan_subnet}")
    except Exception as e:
        logger.error(f"[Network/Linux] setup_nat failed: {e}")


def remove_nat_for_singbox(lan_subnet: str = "192.168.10.0/24") -> None:
    """Remove NAT MASQUERADE temporarily for sing-box transparent proxy."""
    try:
        wan = detect_wan_interface()
        _run(["iptables", "-t", "nat", "-D", "POSTROUTING", "-s", lan_subnet,
              "-o", wan, "-j", "MASQUERADE"])
        logger.info(f"[Network/Linux] NAT removed for sing-box.")
    except Exception as e:
        logger.debug(f"[Network/Linux] remove_nat_for_singbox: {e}")


def restore_nat_for_singbox(wan_interface: str, lan_subnet: str = "192.168.10.0/24") -> None:
    """Restore NAT after sing-box stops."""
    setup_nat(wan_interface, lan_subnet)


def verify_nat() -> dict:
    """Check if NAT MASQUERADE is active."""
    try:
        r = _run(["iptables", "-t", "nat", "-L", "POSTROUTING", "-n", "-v"])
        active = "MASQUERADE" in r.stdout
        return {"nat_active": active, "details": r.stdout[:500]}
    except Exception as e:
        return {"nat_active": False, "details": str(e)}


# ─── TCP Stack ───────────────────────────────────────────────────────────────

def optimize_tcp_stack(tun_interface_name: str = "") -> dict:
    """Apply basic TCP optimizations."""
    settings = {
        "net.core.rmem_max": "16777216",
        "net.core.wmem_max": "16777216",
        "net.ipv4.tcp_rmem": "4096 87380 16777216",
        "net.ipv4.tcp_wmem": "4096 65536 16777216",
        "net.ipv4.tcp_congestion_control": "bbr",
    }
    applied = {}
    for key, val in settings.items():
        r = _run(["sysctl", "-w", f"{key}={val}"])
        if r.returncode == 0:
            applied[key] = val
    logger.info(f"[Network/Linux] TCP stack optimized: {list(applied.keys())}")
    return applied


# ─── Interface Metric / Health ───────────────────────────────────────────────

def adjust_lan_interface_metric(interface_name: str) -> None:
    """Set route metric for LAN interface (lower = preferred)."""
    try:
        _run(["ip", "link", "set", interface_name, "up"])
        logger.info(f"[Network/Linux] Interface {interface_name} set up.")
    except Exception as e:
        logger.warning(f"[Network/Linux] adjust_lan_interface_metric: {e}")


def check_lan_interface_health(interface_name: str) -> dict:
    """Return health information about a LAN interface."""
    ip = get_lan_ip(interface_name)
    try:
        r = _run(["ip", "-br", "link", "show", interface_name])
        up = "UP" in r.stdout
    except Exception:
        up = False
    return {
        "ok": up and bool(ip),
        "interface": interface_name,
        "ip": ip,
        "up": up,
    }


# ─── Hotspot (hostapd) ───────────────────────────────────────────────────────

_HOSTAPD_CONF = "/tmp/c69_hostapd.conf"
_HOSTAPD_PID = "/tmp/c69_hostapd.pid"


def check_hotspot_supported() -> dict:
    """Check if hostapd is available."""
    r = _run(["which", "hostapd"], timeout=5)
    if r.returncode == 0:
        return {"supported": True, "method": "hostapd", "reason": "hostapd found"}
    return {
        "supported": False,
        "method": "none",
        "reason": "hostapd not installed. Run: sudo apt install hostapd"
    }


def setup_hotspot(ssid: str = "C69-Router", password: str = "matkhau123") -> bool:
    """Start hotspot using hostapd on a secondary WiFi interface."""
    cap = check_hotspot_supported()
    if not cap["supported"]:
        logger.warning(f"[Hotspot/Linux] {cap['reason']}")
        return False

    # Find secondary WiFi (not WAN)
    wan = detect_wan_interface()
    adapter = detect_usb_wifi_adapter(exclude_wan_interface=wan)
    if not adapter["found"]:
        logger.warning("[Hotspot/Linux] No secondary WiFi adapter found for hotspot.")
        return False

    iface = adapter["name"]
    conf = (
        f"interface={iface}\n"
        f"driver=nl80211\n"
        f"ssid={ssid}\n"
        f"hw_mode=g\n"
        f"channel=6\n"
        f"macaddr_acl=0\n"
        f"auth_algs=1\n"
        f"ignore_broadcast_ssid=0\n"
        f"wpa=2\n"
        f"wpa_passphrase={password}\n"
        f"wpa_key_mgmt=WPA-PSK\n"
        f"rsn_pairwise=CCMP\n"
    )
    try:
        with open(_HOSTAPD_CONF, "w") as f:
            f.write(conf)
        stop_hotspot()  # Kill any previous instance
        r = _run(["hostapd", "-B", "-P", _HOSTAPD_PID, _HOSTAPD_CONF], timeout=10)
        if r.returncode == 0:
            logger.info(f"[Hotspot/Linux] Hotspot started: SSID={ssid} on {iface}")
            return True
        logger.error(f"[Hotspot/Linux] hostapd failed: {r.stderr}")
    except Exception as e:
        logger.error(f"[Hotspot/Linux] setup_hotspot error: {e}")
    return False


def stop_hotspot() -> None:
    """Stop hostapd."""
    try:
        if os.path.exists(_HOSTAPD_PID):
            with open(_HOSTAPD_PID) as f:
                pid = f.read().strip()
            _run(["kill", pid])
            os.remove(_HOSTAPD_PID)
        else:
            _run(["pkill", "-f", "hostapd"])
        logger.info("[Hotspot/Linux] Hotspot stopped.")
    except Exception as e:
        logger.debug(f"[Hotspot/Linux] stop_hotspot: {e}")


def get_hotspot_adapter() -> str:
    """Return the WiFi interface used as hotspot, if active."""
    try:
        if os.path.exists(_HOSTAPD_CONF):
            with open(_HOSTAPD_CONF) as f:
                for line in f:
                    if line.startswith("interface="):
                        return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


# ─── Binaries (sing-box) ─────────────────────────────────────────────────────

def get_singbox_binary_name() -> str:
    return "sing-box"


def get_singbox_start_command(config_path: str) -> list:
    """Return command to start sing-box on Linux (run directly, no UAC)."""
    from app.config import PROJECT_DIR
    binary = os.path.join(PROJECT_DIR, "sing-box")
    return [binary, "run", "-c", config_path]


def download_binaries() -> None:
    """Download sing-box binary for Linux if missing."""
    from app.config import PROJECT_DIR
    singbox_bin = os.path.join(PROJECT_DIR, "sing-box")
    if os.path.exists(singbox_bin):
        logger.info("[Network/Linux] sing-box already exists, skip download.")
        return

    version = "1.13.14"
    url = f"https://github.com/SagerNet/sing-box/releases/download/v{version}/sing-box-{version}-linux-{_ARCH}.tar.gz"
    logger.info(f"[Network/Linux] Downloading sing-box from {url}...")

    import ssl
    ctx = ssl._create_unverified_context()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=ctx) as resp:
            data = resp.read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.name.endswith("/sing-box") or member.name == "sing-box":
                    f = tar.extractfile(member)
                    if f:
                        with open(singbox_bin, "wb") as out:
                            out.write(f.read())
                        os.chmod(singbox_bin, 0o755)
                        logger.info(f"[Network/Linux] sing-box downloaded: {singbox_bin}")
                        return
        logger.error("[Network/Linux] sing-box binary not found in archive.")
    except Exception as e:
        logger.error(f"[Network/Linux] download_binaries failed: {e}")


# ─── Captive Portal ──────────────────────────────────────────────────────────

def setup_captive_portproxy(lan_ip: str, port: int = 9000) -> None:
    """Redirect HTTP port 80 to captive portal via iptables REDIRECT."""
    try:
        _run([
            "iptables", "-t", "nat", "-A", "PREROUTING",
            "-p", "tcp", "--dport", "80",
            "-j", "REDIRECT", "--to-port", str(port)
        ])
        logger.info(f"[Network/Linux] Captive portal redirect 80 -> {port}")
    except Exception as e:
        logger.error(f"[Network/Linux] setup_captive_portproxy: {e}")


def cleanup_captive_portproxy(lan_ip: str) -> None:
    """Remove captive portal iptables redirect."""
    try:
        _run([
            "iptables", "-t", "nat", "-D", "PREROUTING",
            "-p", "tcp", "--dport", "80",
            "-j", "REDIRECT", "--to-port", "9000"
        ])
        logger.info("[Network/Linux] Captive portal redirect removed.")
    except Exception as e:
        logger.debug(f"[Network/Linux] cleanup_captive_portproxy: {e}")


# ─── OS Startup (systemd) ────────────────────────────────────────────────────

def setup_os_startup(exe_path: str) -> bool:
    """Create systemd service for c69-router."""
    service = """[Unit]
Description=c69-router Network Router
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exe}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
""".format(exe=exe_path)
    try:
        service_path = "/etc/systemd/system/c69-router.service"
        with open(service_path, "w") as f:
            f.write(service)
        _run(["systemctl", "daemon-reload"])
        _run(["systemctl", "enable", "c69-router.service"])
        logger.info(f"[Network/Linux] systemd service installed: {service_path}")
        return True
    except Exception as e:
        logger.error(f"[Network/Linux] setup_os_startup failed: {e}")
        return False
