"""
c69-router Platform - macOS Implementation

Uses macOS networking tools:
  - ifconfig / networksetup  : interface management, IP assignment
  - route                    : routing table
  - pfctl                    : Packet Filter (NAT, firewall)
  - sysctl                   : IP forwarding
  - Internet Sharing CLI     : hotspot via sharing.plist
  - launchd / LaunchAgent    : startup service

Requires: running as root (sudo).
"""

import os
import re
import sys
import time
import plistlib
import logging
import subprocess
import platform
import urllib.request
import tarfile
import io

logger = logging.getLogger(__name__)

# Detect architecture (Intel vs Apple Silicon)
_MACHINE = platform.machine()
_ARCH = "arm64" if _MACHINE == "arm64" else "amd64"

# Temp files for pf
_PF_CONF = "/tmp/c69_pf.conf"
_PF_ANCHOR = "c69router"


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
    """Enable IPv4 forwarding via sysctl (macOS uses net.inet.ip.forwarding)."""
    try:
        _run(["sysctl", "-w", "net.inet.ip.forwarding=1"])
        logger.info("[Network/macOS] IP forwarding enabled.")
    except Exception as e:
        logger.error(f"[Network/macOS] enable_ip_forwarding failed: {e}")


def ensure_wan_forwarding_disabled(wan_interface: str) -> None:
    """No-op on macOS — pfctl handles forwarding selectively."""
    logger.debug(f"[Network/macOS] ensure_wan_forwarding_disabled: no-op for {wan_interface}")


# ─── Firewall (pfctl) ────────────────────────────────────────────────────────

def setup_firewall_rules(lan_interface: str = "", lan_ip: str = "192.168.10.1") -> None:
    """Open ports via pfctl pass rules."""
    # On macOS, application firewall (socketfilterfw) is separate from pfctl.
    # We just ensure pf is not blocking our ports.
    try:
        # Allow DHCP
        _run(["socketfilterfw", "--add", "/usr/sbin/bootpd"], timeout=5)
    except Exception:
        pass
    logger.info("[Network/macOS] Firewall: macOS Application Firewall allows our services by default.")


# ─── Interface Detection ─────────────────────────────────────────────────────

def detect_wan_interface() -> str:
    """Parse 'route get default' to find default gateway interface."""
    try:
        r = _run(["route", "get", "default"])
        m = re.search(r"interface:\s*(\S+)", r.stdout)
        if m:
            iface = m.group(1)
            logger.info(f"[Network/macOS] WAN interface: {iface}")
            return iface
    except Exception as e:
        logger.error(f"[Network/macOS] detect_wan_interface error: {e}")
    return "en0"


def detect_lan_interface(exclude_interface: str = "") -> str:
    """Return first UP non-loopback, non-WAN interface."""
    try:
        r = _run(["ifconfig", "-l"])
        interfaces = r.stdout.strip().split()
        for iface in interfaces:
            if iface in ("lo0", exclude_interface):
                continue
            r2 = _run(["ifconfig", iface])
            if "status: active" in r2.stdout or "UP" in r2.stdout.split("\n")[0]:
                return iface
    except Exception as e:
        logger.error(f"[Network/macOS] detect_lan_interface error: {e}")
    return "en1"


def smart_detect_lan(exclude_interface: str = "") -> tuple:
    """Return (lan_interface, lan_type)."""
    try:
        r = _run(["ifconfig", "-l"])
        interfaces = r.stdout.strip().split()
        eth_candidates = []
        wifi_candidates = []
        for iface in interfaces:
            if iface in ("lo0", exclude_interface):
                continue
            # networksetup -listallhardwareports shows type
            r2 = _run(["networksetup", "-listallhardwareports"])
            lines = r2.stdout.splitlines()
            for i, line in enumerate(lines):
                if iface in line:
                    # Check previous lines for type
                    for j in range(max(0, i-3), i):
                        if "Wi-Fi" in lines[j] or "AirPort" in lines[j]:
                            wifi_candidates.append(iface)
                        elif "Ethernet" in lines[j]:
                            eth_candidates.append(iface)
        if eth_candidates:
            return eth_candidates[0], "ethernet"
        if wifi_candidates:
            return wifi_candidates[0], "wifi"
    except Exception as e:
        logger.error(f"[Network/macOS] smart_detect_lan error: {e}")
    lan = detect_lan_interface(exclude_interface)
    return lan, "ethernet"


def is_wan_interface_valid(interface_name: str) -> bool:
    """Check if interface is the default route interface."""
    try:
        wan = detect_wan_interface()
        return wan == interface_name
    except Exception:
        return False


def is_lan_interface_valid(interface_name: str, wan_interface: str = "",
                            lan_gateway_ip: str = "192.168.10.1") -> bool:
    """Return True if interface is UP and not WAN."""
    if not interface_name or interface_name == wan_interface or interface_name == "lo0":
        return False
    try:
        r = _run(["ifconfig", interface_name])
        return "UP" in r.stdout.split("\n")[0]
    except Exception:
        return False


def detect_usb_wifi_adapter(exclude_wan_interface: str = "") -> dict:
    """Detect secondary WiFi adapter."""
    try:
        r = _run(["networksetup", "-listallhardwareports"])
        lines = r.stdout.splitlines()
        for i, line in enumerate(lines):
            if ("Wi-Fi" in line or "AirPort" in line) and i + 1 < len(lines):
                # Next line usually has "Device: en1"
                dev_line = lines[i + 1]
                m = re.search(r"Device:\s*(\S+)", dev_line)
                if m:
                    name = m.group(1)
                    if name != exclude_wan_interface:
                        return {"found": True, "name": name, "description": f"WiFi {name}"}
    except Exception as e:
        logger.debug(f"[Network/macOS] detect_usb_wifi_adapter: {e}")
    return {"found": False, "name": "", "description": ""}


# ─── IP Assignment ───────────────────────────────────────────────────────────

def _mask_to_prefix(mask: str) -> int:
    return sum(bin(int(x)).count("1") for x in mask.split("."))


def get_lan_ip(interface_name: str) -> str:
    """Return current IPv4 of interface."""
    try:
        r = _run(["ifconfig", interface_name])
        m = re.search(r"inet\s+([\d.]+)\s", r.stdout)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def ensure_lan_ip_assigned(lan_interface: str, lan_gateway_ip: str,
                            subnet_mask: str = "255.255.255.0") -> bool:
    """Assign static IP via ifconfig."""
    current = get_lan_ip(lan_interface)
    if current == lan_gateway_ip:
        logger.info(f"[Network/macOS] {lan_interface} already has {lan_gateway_ip}")
        return True
    try:
        _run(["ifconfig", lan_interface, lan_gateway_ip, "netmask", subnet_mask], check=True)
        _run(["ifconfig", lan_interface, "up"])
        logger.info(f"[Network/macOS] Assigned {lan_gateway_ip}/{subnet_mask} to {lan_interface}")
        return True
    except Exception as e:
        logger.error(f"[Network/macOS] ensure_lan_ip_assigned failed: {e}")
        return False


def setup_interface_dns(interface_name: str, dns_servers: list) -> None:
    """Set DNS via networksetup."""
    try:
        # Get service name for interface
        r = _run(["networksetup", "-listallhardwareports"])
        lines = r.stdout.splitlines()
        service_name = None
        for i, line in enumerate(lines):
            if interface_name in line:
                for j in range(max(0, i-2), i):
                    if "Hardware Port:" in lines[j]:
                        service_name = lines[j].replace("Hardware Port:", "").strip()
                        break
        if service_name:
            cmd = ["networksetup", "-setdnsservers", service_name] + dns_servers
            _run(cmd)
            logger.info(f"[Network/macOS] DNS set for {service_name}: {dns_servers}")
    except Exception as e:
        logger.warning(f"[Network/macOS] setup_interface_dns: {e}")


def detect_wan_subnet() -> str:
    """Return subnet of default route interface."""
    try:
        import ipaddress
        iface = detect_wan_interface()
        r = _run(["ifconfig", iface])
        m = re.search(r"inet\s+([\d.]+)\s+netmask\s+(0x[0-9a-f]+|\d+\.\d+\.\d+\.\d+)", r.stdout)
        if m:
            ip_str = m.group(1)
            mask_raw = m.group(2)
            if mask_raw.startswith("0x"):
                # Convert hex netmask to dotted
                n = int(mask_raw, 16)
                mask_str = socket.inet_ntoa(struct.pack(">I", n))
            else:
                mask_str = mask_raw
            net = ipaddress.ip_interface(f"{ip_str}/{mask_str}").network
            return str(net)
    except Exception as e:
        logger.debug(f"[Network/macOS] detect_wan_subnet: {e}")
    return "192.168.1.0/24"


import socket, struct


# ─── NAT (pfctl) ─────────────────────────────────────────────────────────────

def setup_nat(wan_interface: str, lan_subnet: str = "192.168.10.0/24") -> None:
    """Setup pf NAT rules for macOS."""
    pf_rules = (
        f"nat on {wan_interface} from {lan_subnet} to any -> ({wan_interface})\n"
        f"pass all\n"
    )
    try:
        with open(_PF_CONF, "w") as f:
            f.write(pf_rules)
        # Enable pf if not enabled
        _run(["pfctl", "-e"], timeout=5)
        # Load anchor rules
        _run(["pfctl", "-a", _PF_ANCHOR, "-f", _PF_CONF])
        logger.info(f"[Network/macOS] pfctl NAT setup: {lan_subnet} -> {wan_interface}")
    except Exception as e:
        logger.error(f"[Network/macOS] setup_nat failed: {e}")


def remove_nat_for_singbox(lan_subnet: str = "192.168.10.0/24") -> None:
    """Flush pf anchor for c69router (removes NAT)."""
    try:
        _run(["pfctl", "-a", _PF_ANCHOR, "-F", "all"])
        logger.info("[Network/macOS] pfctl NAT removed for sing-box.")
    except Exception as e:
        logger.debug(f"[Network/macOS] remove_nat_for_singbox: {e}")


def restore_nat_for_singbox(wan_interface: str, lan_subnet: str = "192.168.10.0/24") -> None:
    """Restore pfctl NAT after sing-box stop."""
    setup_nat(wan_interface, lan_subnet)


def verify_nat() -> dict:
    """Check if pfctl NAT is active."""
    try:
        r = _run(["pfctl", "-a", _PF_ANCHOR, "-s", "nat"])
        active = "nat on" in r.stdout
        return {"nat_active": active, "details": r.stdout[:500]}
    except Exception as e:
        return {"nat_active": False, "details": str(e)}


# ─── TCP Stack ───────────────────────────────────────────────────────────────

def optimize_tcp_stack(tun_interface_name: str = "") -> dict:
    """Apply basic macOS TCP optimizations."""
    settings = {
        "net.inet.tcp.recvspace": "65536",
        "net.inet.tcp.sendspace": "65536",
        "kern.ipc.maxsockbuf": "8388608",
    }
    applied = {}
    for key, val in settings.items():
        r = _run(["sysctl", "-w", f"{key}={val}"])
        if r.returncode == 0:
            applied[key] = val
    logger.info(f"[Network/macOS] TCP stack optimized: {list(applied.keys())}")
    return applied


# ─── Interface Metric / Health ───────────────────────────────────────────────

def adjust_lan_interface_metric(interface_name: str) -> None:
    """macOS doesn't use metric the same way; bring interface up."""
    try:
        _run(["ifconfig", interface_name, "up"])
    except Exception as e:
        logger.warning(f"[Network/macOS] adjust_lan_interface_metric: {e}")


def check_lan_interface_health(interface_name: str) -> dict:
    ip = get_lan_ip(interface_name)
    try:
        r = _run(["ifconfig", interface_name])
        up = "UP" in r.stdout.split("\n")[0]
    except Exception:
        up = False
    return {"ok": up and bool(ip), "interface": interface_name, "ip": ip, "up": up}


# ─── Hotspot (Internet Sharing) ──────────────────────────────────────────────

def check_hotspot_supported() -> dict:
    """Check if Internet Sharing is available (macOS 10.7+)."""
    sharing_plist = "/Library/Preferences/SystemConfiguration/com.apple.nat.plist"
    if os.path.exists(sharing_plist) or _run(["which", "networksetup"]).returncode == 0:
        return {"supported": True, "method": "internet_sharing", "reason": "macOS Internet Sharing available"}
    return {"supported": False, "method": "none", "reason": "Internet Sharing not available"}


def setup_hotspot(ssid: str = "C69-Router", password: str = "matkhau123") -> bool:
    """Enable macOS Internet Sharing via plist manipulation."""
    wan = detect_wan_interface()
    # Find WiFi interface
    adapter = detect_usb_wifi_adapter(exclude_wan_interface=wan)
    if not adapter["found"]:
        # Try to use built-in WiFi if not WAN
        r = _run(["networksetup", "-listallhardwareports"])
        m = re.search(r"Wi-Fi.*?Device:\s*(\w+)", r.stdout, re.DOTALL)
        if m and m.group(1) != wan:
            iface = m.group(1)
        else:
            logger.warning("[Hotspot/macOS] No WiFi interface available for hotspot.")
            return False
    else:
        iface = adapter["name"]

    try:
        # Set SSID and password via networksetup (requires Keychain access)
        _run(["networksetup", "-setairportnetwork", iface, ssid, password], timeout=10)

        # Enable Internet Sharing via plist
        nat_plist = "/Library/Preferences/SystemConfiguration/com.apple.nat.plist"
        try:
            with open(nat_plist, "rb") as f:
                plist = plistlib.load(f)
        except Exception:
            plist = {}

        plist["NAT"] = {
            "AirPort": {"_AirPortEnabled": True, "Enabled": True, "SharingNetworkMask": "255.255.255.0"},
            "Enabled": True,
            "NatPortMapDisabled": False,
            "PrimaryInterface": {"Enabled": True, "PrimaryUserDefinedName": wan},
            "PrimaryService": wan,
            "SharingDevices": [iface],
        }
        with open(nat_plist, "wb") as f:
            plistlib.dump(plist, f)

        # Restart Internet Sharing
        _run_shell("launchctl unload /System/Library/LaunchDaemons/com.apple.InternetSharing.plist 2>/dev/null")
        time.sleep(1)
        _run_shell("launchctl load /System/Library/LaunchDaemons/com.apple.InternetSharing.plist")

        logger.info(f"[Hotspot/macOS] Hotspot started: SSID={ssid} on {iface}")
        return True
    except Exception as e:
        logger.error(f"[Hotspot/macOS] setup_hotspot error: {e}")
        return False


def stop_hotspot() -> None:
    """Disable Internet Sharing."""
    try:
        _run_shell("launchctl unload /System/Library/LaunchDaemons/com.apple.InternetSharing.plist 2>/dev/null")
        logger.info("[Hotspot/macOS] Internet Sharing stopped.")
    except Exception as e:
        logger.debug(f"[Hotspot/macOS] stop_hotspot: {e}")


def get_hotspot_adapter() -> str:
    """Return the interface used for Internet Sharing."""
    try:
        nat_plist = "/Library/Preferences/SystemConfiguration/com.apple.nat.plist"
        with open(nat_plist, "rb") as f:
            plist = plistlib.load(f)
        devices = plist.get("NAT", {}).get("SharingDevices", [])
        if devices:
            return devices[0]
    except Exception:
        pass
    return ""


# ─── Binaries (sing-box) ─────────────────────────────────────────────────────

def get_singbox_binary_name() -> str:
    return "sing-box"


def get_singbox_start_command(config_path: str) -> list:
    from app.config import PROJECT_DIR
    binary = os.path.join(PROJECT_DIR, "sing-box")
    return [binary, "run", "-c", config_path]


def download_binaries() -> None:
    """Download sing-box for macOS if missing."""
    from app.config import PROJECT_DIR
    singbox_bin = os.path.join(PROJECT_DIR, "sing-box")
    if os.path.exists(singbox_bin):
        logger.info("[Network/macOS] sing-box already exists, skip download.")
        return

    version = "1.13.14"
    url = f"https://github.com/SagerNet/sing-box/releases/download/v{version}/sing-box-{version}-darwin-{_ARCH}.tar.gz"
    logger.info(f"[Network/macOS] Downloading sing-box from {url}...")

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
                        logger.info(f"[Network/macOS] sing-box downloaded: {singbox_bin}")
                        return
    except Exception as e:
        logger.error(f"[Network/macOS] download_binaries failed: {e}")


# ─── Captive Portal ──────────────────────────────────────────────────────────

def setup_captive_portproxy(lan_ip: str, port: int = 9000) -> None:
    """Redirect HTTP 80 to captive portal via pfctl rdr."""
    rdr_rule = f"rdr on * proto tcp from any to {lan_ip} port 80 -> {lan_ip} port {port}\n"
    try:
        existing = ""
        if os.path.exists(_PF_CONF):
            with open(_PF_CONF) as f:
                existing = f.read()
        if rdr_rule not in existing:
            with open(_PF_CONF, "a") as f:
                f.write(rdr_rule)
            _run(["pfctl", "-a", _PF_ANCHOR, "-f", _PF_CONF])
        logger.info(f"[Network/macOS] Captive portal pfctl rdr 80 -> {port}")
    except Exception as e:
        logger.error(f"[Network/macOS] setup_captive_portproxy: {e}")


def cleanup_captive_portproxy(lan_ip: str) -> None:
    """Remove captive portal rdr by flushing and reloading pf config without it."""
    try:
        if os.path.exists(_PF_CONF):
            with open(_PF_CONF) as f:
                content = f.read()
            filtered = "\n".join(
                l for l in content.splitlines() if "port 80" not in l
            )
            with open(_PF_CONF, "w") as f:
                f.write(filtered)
            _run(["pfctl", "-a", _PF_ANCHOR, "-f", _PF_CONF])
        logger.info("[Network/macOS] Captive portal rdr removed.")
    except Exception as e:
        logger.debug(f"[Network/macOS] cleanup_captive_portproxy: {e}")


# ─── OS Startup (LaunchAgent) ────────────────────────────────────────────────

def setup_os_startup(exe_path: str) -> bool:
    """Create LaunchDaemon for c69-router."""
    plist = {
        "Label": "com.c69.router",
        "ProgramArguments": [exe_path],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": "/var/log/c69-router.log",
        "StandardErrorPath": "/var/log/c69-router.err",
    }
    plist_path = "/Library/LaunchDaemons/com.c69.router.plist"
    try:
        with open(plist_path, "wb") as f:
            plistlib.dump(plist, f)
        _run(["launchctl", "load", plist_path])
        logger.info(f"[Network/macOS] LaunchDaemon installed: {plist_path}")
        return True
    except Exception as e:
        logger.error(f"[Network/macOS] setup_os_startup failed: {e}")
        return False
