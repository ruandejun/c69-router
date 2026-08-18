"""
clash_manager.py — Quản lý Mihomo / Clash Meta Core (Engine OpenClash)
Chịu trách nhiệm:
1. Tự động sinh file cấu hình chuẩn OpenClash YAML (clash-config.yaml).
2. Quản lý vòng đời tiến trình mihomo.exe (start/stop/restart).
3. Tích hợp Clash REST API để đổi proxy, reset Direct theo từng IP tức thì (0ms) không cần restart.
4. Bảo đảm 100% không xung đột mạng, không loop Wintun, không nghẽn CPU.
"""

import os
import sys
import yaml
import json
import logging
import platform
import subprocess
import time
import ipaddress
import urllib.request
import urllib.error
from typing import Optional, List, Dict, Any

from app.config import AppConfig, PROJECT_DIR
from app.mac_registry import MACRegistry
from app.network_setup import (
    detect_wan_interface, is_wan_interface_valid, detect_wan_subnet,
    setup_nat, remove_nat_for_singbox, restore_nat_for_singbox
)

logger = logging.getLogger(__name__)

CLASH_CONFIG = os.path.join(PROJECT_DIR, "clash-config.yaml")
CLASH_LOG = os.path.join(PROJECT_DIR, "clash.log")
CLASH_API_PORT = 26990
CLASH_API_URL = f"http://127.0.0.1:{CLASH_API_PORT}"


import threading


def _detect_wan_host_ip(wan_interface: str) -> str:
    """Detect IP cụ thể của WAN interface trên Host PC (e.g. 192.168.1.68).

    QUAN TRỌNG: Đây là WAN host IP /32 cần exclude khỏi TUN route-exclude-address.
    Nếu thiếu: traffic từ Host PC bị TUN bắt → Mihomo dial ra từ IP này →
    packet mới lại bị TUN bắt → infinite loop → 'reject loopback connection'.

    Args:
        wan_interface: Tên WAN interface (e.g. 'Ethernet', 'Wi-Fi')
    Returns:
        IP string (e.g. '192.168.1.68') hoặc '' nếu không detect được
    """
    if platform.system() != "Windows":
        return ""
    try:
        # Dùng cú pháp đơn giản, không dùng $_ trong f-string để tránh conflict
        ps = (
            f"Get-NetIPAddress -InterfaceAlias '{wan_interface}' "
            f"-AddressFamily IPv4 -ErrorAction SilentlyContinue "
            f"| Select-Object -ExpandProperty IPAddress -First 1"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=5
        )
        ip = result.stdout.strip()
        if ip and "." in ip and not ip.startswith("169.254.") and not ip.startswith("127."):
            logger.info(f"[Clash] Detected WAN host IP on '{wan_interface}': {ip}")
            return ip
    except Exception as e:
        logger.warning(f"[Clash] _detect_wan_host_ip('{wan_interface}'): {e}")
    return ""


class ClashManager:
    def __init__(self, config: AppConfig, mac_registry: MACRegistry):
        self._config = config
        self._registry = mac_registry
        self.process: Optional[subprocess.Popen] = None
        self._stop_requested = False
        self.last_error: Optional[str] = None
        self._running_state: bool = False
        self._on_crash_callback = None
        self._watchdog_thread = None
        self._watchdog_stop = threading.Event()

    def set_crash_callback(self, callback):
        """Đăng ký callback được gọi khi process crash hoặc recover."""
        self._on_crash_callback = callback

    def _notify(self, event: str, detail: str = ""):
        if self._on_crash_callback:
            try:
                self._on_crash_callback(event, detail)
            except Exception as e:
                logger.debug(f"[Clash] Callback error: {e}")

    def start_watchdog(self, interval_seconds: int = 20):
        """Giám sát nền: tự động phục hồi nếu tiến trình Mihomo bị tắt ngoài ý muốn."""
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return

        def _loop():
            while not self._watchdog_stop.wait(interval_seconds):
                if self._running_state and not self.check_running():
                    logger.warning("[Clash] Watchdog detected Mihomo process died. Auto-restarting...")
                    self._notify("singbox_crash", "Mihomo died unexpectedly")
                    if self.start():
                        self._notify("singbox_recovered", "Mihomo auto-recovered")
                    else:
                        self._notify("singbox_restart_failed", "Mihomo restart failed")

        self._watchdog_thread = threading.Thread(target=_loop, daemon=True)
        self._watchdog_thread.start()

    # ─── Config Generator (OpenClash YAML) ──────────────────────────

    def generate_config(self) -> bool:
        """Sinh file cấu hình clash-config.yaml chuẩn OpenClash."""
        config = self._config
        devices = self._registry.get_all_devices(include_infrastructure=True) if self._registry else []

        # 1. Xác định card WAN và Subnets
        _lan_prefix = ""
        if config.lan_gateway_ip:
            _lan_prefix = config.lan_gateway_ip.rsplit(".", 1)[0] + "."
        actual_wan = config.wan_interface or detect_wan_interface(exclude_lan_subnet=_lan_prefix)
        if not is_wan_interface_valid(actual_wan):
            actual_wan = detect_wan_interface(exclude_lan_subnet=_lan_prefix)
        logger.info(f"[Clash] Using WAN interface: '{actual_wan}'")

        lan_subnet = "192.168.10.0/24"
        try:
            ip_obj = ipaddress.ip_interface(f"{config.lan_gateway_ip}/24")
            lan_subnet = str(ip_obj.network)
        except Exception:
            pass

        wan_subnet = detect_wan_subnet()
        # Detect WAN host IP cụ thể (e.g. 192.168.1.68) để exclude khỏi TUN.
        # QUAN TRỌNG: thiếu /32 này là nguyên nhân gây loopback traffic loop!
        wan_host_ip = _detect_wan_host_ip(actual_wan)
        lan_subnet = f"{config.lan_gateway_ip.rsplit('.', 1)[0]}.0/24"
        exclude_addresses = [
            "127.0.0.0/8", "172.19.0.0/30",
            lan_subnet,
            "192.168.137.0/24",
            "224.0.0.0/4", "255.255.255.255/32"
        ]
        if wan_subnet and wan_subnet not in exclude_addresses:
            exclude_addresses.append(wan_subnet)
        # Exclude WAN host IP /32 — ngăn TUN capture traffic từ Host PC
        if wan_host_ip:
            wan_host_cidr = f"{wan_host_ip}/32"
            if wan_host_cidr not in exclude_addresses:
                exclude_addresses.append(wan_host_cidr)
            logger.info(f"[Clash] WAN host IP excluded from TUN: {wan_host_cidr}")

        # FIX: Exclude WAN gateway IP — nếu thiếu gateway, host PC không resolve
        # route tới internet khi auto-route override default route.
        try:
            _gw_ps = (
                f"$r = Get-NetRoute -InterfaceAlias '{actual_wan}' -DestinationPrefix '0.0.0.0/0' "
                "-ErrorAction SilentlyContinue | Where-Object {$_.NextHop -ne '0.0.0.0'} | "
                "Select-Object -First 1; if ($r) { $r.NextHop }"
            )
            _gw_res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", _gw_ps],
                capture_output=True, text=True, timeout=5
            )
            _wan_gw = _gw_res.stdout.strip()
            if _wan_gw:
                _gw_cidr = f"{_wan_gw}/32"
                if _gw_cidr not in exclude_addresses:
                    exclude_addresses.append(_gw_cidr)
                logger.info(f"[Clash] WAN gateway excluded from TUN: {_gw_cidr}")
        except Exception:
            pass

        # Exclude proxy IPs
        for proxy in config.proxies:
            proxy_ip = proxy.host.strip()
            if proxy_ip:
                try:
                    ipaddress.ip_address(proxy_ip)
                    proxy_cidr = f"{proxy_ip}/32"
                    if proxy_cidr not in exclude_addresses:
                        exclude_addresses.append(proxy_cidr)
                except ValueError:
                    pass

        # 2. Xây dựng danh sách Proxies
        proxies_list: List[Dict[str, Any]] = []
        proxy_names: List[str] = []
        for proxy in config.proxies:
            p_dict: Dict[str, Any] = {
                "name": proxy.id,
                "type": "socks5" if proxy.type in ("socks5", "socks") else proxy.type,
                "server": proxy.host,
                "port": proxy.port,
                "udp": True,
            }
            if proxy.username and proxy.password:
                p_dict["username"] = proxy.username
                p_dict["password"] = proxy.password
            proxies_list.append(p_dict)
            proxy_names.append(proxy.id)

        # 3. Xây dựng Proxy Groups (Per-device selector)
        block_direct = getattr(config, "block_direct_devices", False)
        whitelist_ips = []
        whitelist_macs = []
        for item in getattr(config, "direct_whitelist", []):
            item_clean = item.strip()
            if not item_clean:
                continue
            if "." in item_clean:
                whitelist_ips.append(item_clean)
            elif ":" in item_clean or "-" in item_clean:
                whitelist_macs.append(item_clean.upper().replace("-", ":"))

        # Liệt kê IP trong pool DHCP
        known_ips = {d.ip for d in devices if d.ip}
        unclaimed_pool_ips = []
        try:
            pool_cursor = ipaddress.IPv4Address(config.dhcp_range_start)
            pool_end = ipaddress.IPv4Address(config.dhcp_range_end)
            while pool_cursor <= pool_end:
                ip_str = str(pool_cursor)
                if ip_str not in known_ips:
                    unclaimed_pool_ips.append(ip_str)
                pool_cursor += 1
        except Exception:
            pass

        # Nếu bật WiFi Hotspot, pre-provision cả dải 192.168.137.2 -> 192.168.137.254
        if getattr(config, "wifi_hotspot_enabled", False):
            try:
                hs_cursor = ipaddress.IPv4Address("192.168.137.2")
                hs_end = ipaddress.IPv4Address("192.168.137.254")
                while hs_cursor <= hs_end:
                    ip_str = str(hs_cursor)
                    if ip_str not in known_ips and ip_str not in unclaimed_pool_ips:
                        unclaimed_pool_ips.append(ip_str)
                    hs_cursor += 1
            except Exception:
                pass

        proxy_groups: List[Dict[str, Any]] = []
        seen_group_names = set()

        # Selector cho từng thiết bị đã nhận diện
        for device in devices:
            ip_clean = device.ip.strip() if device.ip else ""
            if not ip_clean:
                continue
            grp_name = f"select_{ip_clean}"
            if grp_name in seen_group_names:
                continue
            seen_group_names.add(grp_name)

            assigned_proxy = next((p for p in config.proxies if p.id == device.proxy_id), None)
            is_whitelisted = (
                ip_clean in whitelist_ips or
                (device.mac and device.mac.upper().replace("-", ":") in whitelist_macs)
            )

            # Chọn default target
            if device.proxy_id and assigned_proxy:
                default_target = device.proxy_id
            elif block_direct and not is_whitelisted:
                default_target = "REJECT"
            else:
                default_target = "DIRECT"

            group_proxies = ["DIRECT", "REJECT"] + proxy_names
            # Đưa default_target lên đầu nếu cần
            if default_target in group_proxies:
                group_proxies.remove(default_target)
                group_proxies.insert(0, default_target)

            proxy_groups.append({
                "name": grp_name,
                "type": "select",
                "proxies": group_proxies
            })

        # Selector cho unclaimed pool IPs
        pool_default = "REJECT" if block_direct else "DIRECT"
        for ip_str in unclaimed_pool_ips:
            grp_name = f"select_{ip_str}"
            if grp_name in seen_group_names:
                continue
            seen_group_names.add(grp_name)

            group_proxies = ["DIRECT", "REJECT"] + proxy_names
            if pool_default in group_proxies:
                group_proxies.remove(pool_default)
                group_proxies.insert(0, pool_default)

            proxy_groups.append({
                "name": grp_name,
                "type": "select",
                "proxies": group_proxies
            })

        # 4. Xây dựng Rules
        rules: List[str] = []

        # ── BYPASS CỤC BỘ & DASHBOARD ROUTER: Luôn đi DIRECT 100% ──
        # Đảm bảo thiết bị (kể cả đã gán Proxy SOCKS5) luôn truy cập được Dashboard Web (port 9000/8000),
        # Gateway Router (192.168.137.1, 192.168.10.1), DNS, và các IP cục bộ mà không bị gửi qua Proxy.
        rules.append("DST-PORT,9000,DIRECT")
        rules.append("DST-PORT,8000,DIRECT")
        rules.append("DST-PORT,53,DIRECT")
        rules.append("IP-CIDR,127.0.0.0/8,DIRECT,no-resolve")
        rules.append("IP-CIDR,192.168.137.1/32,DIRECT,no-resolve")
        rules.append("IP-CIDR,192.168.10.1/32,DIRECT,no-resolve")
        if config.lan_gateway_ip:
            rules.append(f"IP-CIDR,{config.lan_gateway_ip}/32,DIRECT,no-resolve")
        rules.append("IP-CIDR,224.0.0.0/4,DIRECT,no-resolve")
        rules.append("IP-CIDR,255.255.255.255/32,DIRECT,no-resolve")

        # TỰ ĐỘNG BẢO VỆ ARUBA / ACCESS POINT: Luôn đi DIRECT 100% không bao giờ qua proxy
        from app.mac_registry import is_aruba_or_ap_mac
        for device in devices:
            ip_clean = device.ip.strip() if device.ip else ""
            if ip_clean and (is_aruba_or_ap_mac(device.mac, device.name)):
                rules.append(f"SRC-IP-CIDR,{ip_clean}/32,DIRECT")

        # Whitelist IP -> DIRECT
        if block_direct:
            for ip in whitelist_ips:
                rules.append(f"SRC-IP-CIDR,{ip}/32,DIRECT")

        # Per device routing
        seen_rules = set()
        for device in devices:
            ip_clean = device.ip.strip() if device.ip else ""
            if not ip_clean or ip_clean in seen_rules:
                continue
            if is_aruba_or_ap_mac(device.mac, device.name):
                continue
            seen_rules.add(ip_clean)
            rules.append(f"SRC-IP-CIDR,{ip_clean}/32,select_{ip_clean}")

        for ip_str in unclaimed_pool_ips:
            if ip_str in seen_rules:
                continue
            seen_rules.add(ip_str)
            rules.append(f"SRC-IP-CIDR,{ip_str}/32,select_{ip_str}")

        # Fallback LAN
        if block_direct:
            rules.append(f"SRC-IP-CIDR,{lan_subnet},REJECT")
            if getattr(config, "wifi_hotspot_enabled", False):
                rules.append("SRC-IP-CIDR,192.168.137.0/24,REJECT")
        else:
            rules.append(f"SRC-IP-CIDR,{lan_subnet},DIRECT")
            if getattr(config, "wifi_hotspot_enabled", False):
                rules.append("SRC-IP-CIDR,192.168.137.0/24,DIRECT")

        rules.append("MATCH,DIRECT")

        # Xác định danh sách interface LAN cần TUN bắt traffic (KHÔNG bao gồm WAN để bảo vệ Host PC)
        # DYNAMIC: scan tất cả adapter Up (trừ WAN, TUN, Hyper-V, Bluetooth) thay vì hardcode
        lan_ifaces = []
        _primary_lan = config.lan_interface or ""
        if _primary_lan and _primary_lan != actual_wan:
            lan_ifaces.append(_primary_lan)

        try:
            _ps_scan = (
                f"$wan = '{actual_wan}';"
                "Get-NetAdapter | Where-Object {"
                "  $_.Status -eq 'Up' -and"
                "  $_.Name -ne $wan -and"
                "  $_.InterfaceDescription -notlike '*Wintun*' -and"
                "  $_.InterfaceDescription -notlike '*Meta Tunnel*' -and"
                "  $_.InterfaceDescription -notlike '*Hyper-V*' -and"
                "  $_.InterfaceDescription -notlike '*VMware*' -and"
                "  $_.InterfaceDescription -notlike '*Loopback*' -and"
                "  $_.InterfaceDescription -notlike '*Bluetooth*'"
                "} | Select-Object -ExpandProperty Name"
            )
            _scan_res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", _ps_scan],
                capture_output=True, text=True, timeout=5
            )
            for _iface_name in _scan_res.stdout.strip().splitlines():
                _iface_name = _iface_name.strip()
                if _iface_name and _iface_name not in lan_ifaces and _iface_name != actual_wan:
                    lan_ifaces.append(_iface_name)
        except Exception:
            pass

        # Nếu không tìm thấy gì, dùng primary LAN từ config
        if not lan_ifaces:
            lan_ifaces = [_primary_lan or "Ethernet 3"]

        logger.info(f"[Clash] TUN include-interface (dynamic): {lan_ifaces} (WAN '{actual_wan}' EXCLUDED)")

        # 5. Hoàn thiện Clash YAML Config
        clash_yaml_data: Dict[str, Any] = {
            "port": 0,
            "socks-port": 0,
            "mixed-port": 0,
            "allow-lan": True,
            "bind-address": "*",
            "mode": "rule",
            "log-level": "info",
            "ipv6": False,
            "interface-name": actual_wan,
            "external-controller": f"127.0.0.1:{CLASH_API_PORT}",
            "external-ui": "",
            "secret": "",
            "tun": {
                "enable": True,
                "stack": "gvisor",
                "device": "GenRouterTUN",
                "auto-route": True,
                "auto-detect-interface": True,
                "strict-route": False,
                "endpoint-independent-nat": True,
                "include-interface": lan_ifaces,
                # FIX: Exclude WAN interface khỏi TUN capture — ngăn TUN bắt traffic
                # trên card WAN gây mất internet host PC (đặc biệt trên dual-LAN).
                "exclude-interface": [actual_wan],
                "dns-hijack": ["any:53", "tcp://any:53"],
                "route-exclude-address": exclude_addresses,
                "inet4-route-exclude-address": exclude_addresses
            },

            "dns": {
                "enable": True,
                "listen": "0.0.0.0:1053",
                "ipv6": False,
                "enhanced-mode": "redir-host",
                "default-nameserver": [
                    "1.1.1.1",
                    "8.8.8.8"
                ],
                "nameserver": [
                    "1.1.1.1",
                    "8.8.8.8",
                    "9.9.9.9"
                ]
            },
            "proxies": proxies_list if proxies_list else [{"name": "DIRECT-DEFAULT", "type": "direct"}],
            "proxy-groups": proxy_groups if proxy_groups else [{"name": "GLOBAL", "type": "select", "proxies": ["DIRECT"]}],
            "rules": rules
        }

        # FIX: Sync config.tun_interface cho phần còn lại của app (main.py clear_interface_dns,
        # optimize_tcp_stack, etc.) — trước đây chỉ singbox_manager làm việc này,
        # clash_manager thì không → main.py gọi clear DNS với tên cũ "genrouter-tun-9" → miss hoàn toàn.
        self._config.tun_interface = "GenRouterTUN"

        try:
            with open(CLASH_CONFIG, "w", encoding="utf-8") as f:
                yaml.dump(clash_yaml_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            logger.info(f"[Clash] Generated config with {len(proxies_list)} proxies, {len(proxy_groups)} groups, {len(rules)} rules, include-interface: {lan_ifaces}.")
            return True
        except Exception as e:
            self.last_error = f"Lỗi ghi file clash-config.yaml: {e}"
            logger.error(f"[Clash] {self.last_error}")
            return False

    # ─── Process Lifecycle ──────────────────────────────────────────

    def start(self, _retry: bool = True) -> bool:
        """Khởi chạy Mihomo Core với quyền Admin (kế thừa từ c69-router.exe)."""
        _IS_WINDOWS = platform.system() == "Windows"

        # 1. Trích xuất từ RESOURCE_DIR nếu đang chạy frozen exe
        from app.config import RESOURCE_DIR
        for fn in ["mihomo.exe", "wintun.dll", "geoip.metadb"]:
            src = os.path.join(RESOURCE_DIR, fn)
            dst = os.path.join(PROJECT_DIR, fn)
            if not os.path.exists(dst) and os.path.exists(src):
                try:
                    import shutil
                    shutil.copy2(src, dst)
                    logger.info(f"[Clash] Extracted bundled '{fn}' to {PROJECT_DIR}")
                except Exception as e:
                    logger.warning(f"[Clash] Could not extract bundled {fn}: {e}")

        mihomo_bin = os.path.join(PROJECT_DIR, "mihomo.exe" if _IS_WINDOWS else "mihomo")
        if not os.path.exists(mihomo_bin):
            logger.info("[Clash] mihomo.exe not found, attempting auto-download...")
            from app.network_setup import download_binaries
            download_binaries()
            if not os.path.exists(mihomo_bin):
                self.last_error = "Không tìm thấy file mihomo.exe và tự động tải về thất bại."
                logger.error(f"[Clash] {self.last_error}")
                return False

        if not os.path.exists(CLASH_CONFIG):
            self.generate_config()

        # Dừng instance cũ và giải phóng adapter Wintun
        self.stop()
        self._stop_requested = False
        if _IS_WINDOWS:
            try:
                ps_clean = (
                    "Get-NetAdapter -Name 'GenRouterTUN' -ErrorAction SilentlyContinue | ForEach-Object { "
                    "  pnputil /remove-device $_.DeviceID /force 2>&1 | Out-Null "
                    "}"
                )
                subprocess.run(["powershell", "-NoProfile", "-Command", ps_clean], capture_output=True, timeout=5)
            except Exception:
                pass
        time.sleep(0.5)

        # NAT removal moved to AFTER Mihomo is verified running (see below)
        # to avoid gap where NAT is gone but TUN not yet capturing traffic.

        logger.info(f"[Clash] Starting Mihomo process: {mihomo_bin}")
        log_out_path = os.path.join(PROJECT_DIR, "mihomo.log")
        try:
            log_file = open(log_out_path, "a", encoding="utf-8", errors="ignore")
            if _IS_WINDOWS:
                self.process = subprocess.Popen(
                    [mihomo_bin, "-d", PROJECT_DIR, "-f", CLASH_CONFIG],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                self.process = subprocess.Popen(
                    [mihomo_bin, "-d", PROJECT_DIR, "-f", CLASH_CONFIG],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )

            # Chờ API sẵn sàng (tối đa 6s)
            time.sleep(1.5)
            for _ in range(12):
                if self.check_running():
                    logger.info("[Clash] ✓ Mihomo started and Clash REST API is responsive!")
                    self._running_state = True
                    self._remove_nat_after_tun_ready()
                    self._fix_tun_metric()
                    return True
                time.sleep(0.5)

            # Nếu chưa running và còn retry: giải phóng Wintun và thử lại
            if _retry:
                logger.warning("[Clash] Mihomo not responsive on first attempt, cooling down Wintun and retrying...")
                self.stop()
                time.sleep(2.0)
                return self.start(_retry=False)

            logger.warning("[Clash] Mihomo started but API is not answering yet.")
            self._running_state = True
            self._remove_nat_after_tun_ready()
            self._fix_tun_metric()
            return True
        except Exception as e:
            self.last_error = f"Lỗi khởi động Mihomo: {e}"
            logger.error(f"[Clash] {self.last_error}")
            return False

    def _remove_nat_after_tun_ready(self) -> None:
        """Gỡ NAT SAU KHI TUN route thực sự active — tránh gap mất mạng.

        FIX: Trước đây gỡ NAT ngay khi Mihomo start, nhưng WinTUN driver cần
        vài giây để tạo adapter + inject routes. Trong khoảng đó NAT đã bị gỡ
        mà TUN chưa lên → LAN devices mất internet hoàn toàn.
        Giờ chờ TUN adapter Up + có route thật trước khi gỡ NAT.
        """
        _hotspot_mode = getattr(self._config, "wifi_hotspot_enabled", False)
        if not _hotspot_mode:
            _lan_subnet = "192.168.10.0/24"
            try:
                _lan_subnet = str(ipaddress.ip_interface(f"{self._config.lan_gateway_ip}/24").network)
            except Exception:
                pass

            # Chờ TUN adapter thực sự Up và có route trước khi gỡ NAT
            _tun_ready = False
            for _attempt in range(20):  # Max 20 x 0.5s = 10s
                try:
                    _chk = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         "$a = Get-NetAdapter -Name 'GenRouterTUN' -EA SilentlyContinue; "
                         "if ($a -and $a.Status -eq 'Up') { "
                         "  $r = Get-NetRoute -InterfaceAlias 'GenRouterTUN' -EA SilentlyContinue; "
                         "  if ($r) { Write-Output 'READY' } else { Write-Output 'NO_ROUTE' } "
                         "} else { Write-Output 'NOT_UP' }"],
                        capture_output=True, text=True, timeout=3
                    )
                    if 'READY' in _chk.stdout:
                        _tun_ready = True
                        logger.info(f"[Clash] TUN adapter verified READY after {_attempt * 0.5:.1f}s")
                        break
                except Exception:
                    pass
                time.sleep(0.5)

            if not _tun_ready:
                logger.warning("[Clash] TUN not ready after 10s — removing NAT anyway (risk of traffic gap)")

            remove_nat_for_singbox(_lan_subnet)
            logger.info(f"[Clash] NAT removed {'AFTER TUN verified ready' if _tun_ready else 'WITHOUT TUN verification'} — {'no' if _tun_ready else 'possible'} traffic gap.")

    def _fix_tun_metric(self) -> None:
        """Đảm bảo card TUN (GenRouterTUN) luôn có metric cao (500) và DNS luôn rỗng (EMPTY).
        Chạy liên tục dạng background loop để đè bẹp bất kỳ nỗ lực nào của Wintun/Mihomo gán DNS ảo.
        QUAN TRỌNG: KHÔNG ĐƯỢC xóa DNS trên WAN interface — chỉ TUN và LAN thực sự.
        """
        if platform.system() != "Windows":
            return

        def _clean_loop():
            tun_name = "GenRouterTUN"
            lan_name = getattr(self._config, "lan_interface", "") or ""
            _lan_gw = getattr(self._config, "lan_gateway_ip", "") or ""
            _lan_pfx = _lan_gw.rsplit(".", 1)[0] + "." if _lan_gw else ""
            wan_name = getattr(self._config, "wan_interface", "") or detect_wan_interface(exclude_lan_subnet=_lan_pfx)

            # FIX: Xây danh sách interface cần xóa DNS — CHỈ TUN, KHÔNG LAN nếu LAN==WAN
            # Trên dual-LAN machine, lan_name có thể là card Ethernet thật → nếu xóa DNS
            # trên nó trong khi nó cũng là WAN → mất DNS host PC ngay.
            clean_targets = set()
            if tun_name:
                clean_targets.add(tun_name)
            if lan_name and lan_name != wan_name:
                clean_targets.add(lan_name)
            # BẢO VỆ WAN: loại bỏ triệt để, kiểm tra cả tên lẫn alias
            if wan_name:
                clean_targets.discard(wan_name)
            if not clean_targets:
                logger.warning("[Clash] DNS cleaner: no targets after WAN protection, skipping.")
                return

            logger.info(f"[Clash] DNS cleaner targets: {clean_targets} (WAN '{wan_name}' PROTECTED)")

            # PHASE 1: Burst — xóa DNS nhanh 12 lần × 1s (đè Wintun gán DNS ngay sau khi TUN lên)
            for i in range(12):
                if self._stop_requested:
                    return
                try:
                    ps_cmd = (
                        f"$tun = '{tun_name}'; "
                        f"Set-NetIPInterface -InterfaceAlias $tun -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 500 -ErrorAction SilentlyContinue; "
                        f"Set-DnsClient -InterfaceAlias $tun -RegisterThisConnectionsAddress $false -ErrorAction SilentlyContinue; "
                    )
                    for iface in clean_targets:
                        ps_cmd += (
                            f"Set-DnsClientServerAddress -InterfaceAlias '{iface}' -ResetServerAddresses -ErrorAction SilentlyContinue; "
                            f"Set-DnsClientServerAddress -InterfaceAlias '{iface}' -ServerAddresses @() -ErrorAction SilentlyContinue; "
                            f"netsh interface ipv4 delete dns name='{iface}' all 2>&1 | Out-Null; "
                        )
                    subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, timeout=5)
                except Exception:
                    pass
                time.sleep(1.0)
            logger.info(f"[Clash] DNS cleaner burst phase done — {clean_targets} DNS EMPTY, WAN '{wan_name}' untouched.")

            # PHASE 2: Persistent daemon — kiểm tra & xóa DNS mỗi 10s liên tục
            # (Wintun/Mihomo có thể gán lại DNS bất cứ lúc nào, cần canh giữ liên tục)
            _dns_check_cmd = (
                f"(Get-DnsClientServerAddress -InterfaceAlias '{tun_name}' -AddressFamily IPv4 -EA SilentlyContinue).ServerAddresses -join ','"
            )
            while not self._stop_requested:
                time.sleep(10.0)
                if self._stop_requested:
                    break
                try:
                    _chk = subprocess.run(
                        ["powershell", "-NoProfile", "-Command", _dns_check_cmd],
                        capture_output=True, text=True, timeout=5
                    )
                    _dns_val = _chk.stdout.strip()
                    if _dns_val:
                        # DNS bị gán lại → xóa ngay
                        logger.warning(f"[Clash] DNS re-appeared on TUN '{tun_name}': {_dns_val} — clearing again!")
                        ps_cmd = (
                            f"$tun = '{tun_name}'; "
                            f"Set-NetIPInterface -InterfaceAlias $tun -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 500 -ErrorAction SilentlyContinue; "
                            f"Set-DnsClient -InterfaceAlias $tun -RegisterThisConnectionsAddress $false -ErrorAction SilentlyContinue; "
                        )
                        for iface in clean_targets:
                            ps_cmd += (
                                f"Set-DnsClientServerAddress -InterfaceAlias '{iface}' -ResetServerAddresses -ErrorAction SilentlyContinue; "
                                f"Set-DnsClientServerAddress -InterfaceAlias '{iface}' -ServerAddresses @() -ErrorAction SilentlyContinue; "
                                f"netsh interface ipv4 delete dns name='{iface}' all 2>&1 | Out-Null; "
                            )
                        subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, timeout=5)
                except Exception:
                    pass

        threading.Thread(target=_clean_loop, daemon=True, name="tun-dns-cleaner").start()

    def stop(self, restore_nat: bool = False):
        """Dừng tiến trình Mihomo và khôi phục sạch mạng."""
        self._stop_requested = True
        self._running_state = False
        _IS_WINDOWS = platform.system() == "Windows"

        if _IS_WINDOWS:
            try:
                subprocess.run(["taskkill", "/F", "/IM", "mihomo.exe"], capture_output=True, timeout=5)
                subprocess.run(["taskkill", "/F", "/IM", "sing-box.exe"], capture_output=True, timeout=5)
            except Exception:
                pass

            try:
                ps_cleanup = (
                    "Get-PnpDevice -FriendlyName '*GenRouter*' -ErrorAction SilentlyContinue | ForEach-Object { pnputil /remove-device $_.InstanceId /force 2>&1 | Out-Null }; "
                    "Get-PnpDevice -FriendlyName '*Wintun*' -ErrorAction SilentlyContinue | ForEach-Object { pnputil /remove-device $_.InstanceId /force 2>&1 | Out-Null }; "
                    "route delete 0.0.0.0 mask 128.0.0.0 2>&1 | Out-Null; "
                    "route delete 128.0.0.0 mask 128.0.0.0 2>&1 | Out-Null; "
                    "route delete 172.19.0.0 2>&1 | Out-Null; "
                    "route delete 198.18.0.0 2>&1 | Out-Null"
                )
                subprocess.run(["powershell", "-NoProfile", "-Command", ps_cleanup], capture_output=True, timeout=5)
            except Exception:
                pass
            time.sleep(0.5)
        else:
            if self.process:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=3)
                except Exception:
                    try:
                        self.process.kill()
                    except Exception:
                        pass
                self.process = None

        if _IS_WINDOWS and restore_nat:
            try:
                wan = getattr(self._config, "wan_interface", "")
                if wan and not getattr(self._config, "wifi_hotspot_enabled", False):
                    lan_sub = f"{getattr(self._config, 'lan_gateway_ip', '192.168.10.1').rsplit('.', 1)[0]}.0/24"
                    from app.network_setup import restore_nat_for_singbox
                    restore_nat_for_singbox(wan_interface=wan, lan_subnet=lan_sub)
            except Exception:
                pass

        logger.info("[Clash] Mihomo stopped & GenRouterTUN cleaned.")

    @property
    def is_running(self) -> bool:
        """Kiểm tra Mihomo có đang chạy và Clash REST API có phản hồi không."""
        return self.check_running()

    def check_running(self) -> bool:
        """Kiểm tra kết nối Clash REST API."""
        try:
            req = urllib.request.Request(f"{CLASH_API_URL}/proxies", method="GET")
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def reload_now(self) -> bool:
        """Nạp lại cấu hình Clash mà không cần restart tiến trình."""
        if not self.generate_config():
            return False
        try:
            req = urllib.request.Request(
                f"{CLASH_API_URL}/configs?force=true",
                data=json.dumps({"path": CLASH_CONFIG}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="PUT"
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status in (200, 204):
                    logger.info("[Clash] Config reloaded via Clash REST API successfully.")
                    return True
        except Exception as e:
            logger.warning(f"[Clash] Fast reload failed ({e}), falling back to full restart...")
        return self.start()

    # ─── Clash REST API Controls ────────────────────────────────────

    def select_outbound_via_api(self, device_ip: str, target_outbound: str) -> bool:
        """Đổi proxy cho một thiết bị tức thì qua Clash REST API (0ms)."""
        selector_name = f"select_{device_ip}"
        try:
            req = urllib.request.Request(
                f"{CLASH_API_URL}/proxies/{selector_name}",
                data=json.dumps({"name": target_outbound}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="PUT"
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status in (200, 204):
                    logger.info(f"[Clash] Switched {selector_name} -> {target_outbound} (status: {resp.status})")
                    return True
                else:
                    logger.warning(f"[Clash] Switch {selector_name} -> {target_outbound} returned {resp.status}")
                    return False
        except Exception as e:
            logger.warning(f"[Clash] Failed to switch {selector_name} via API: {e}")
            return False

    def update_device_routing(self, device_ip: str, proxy_id: Optional[str]) -> bool:
        """Cập nhật định tuyến proxy cho 1 thiết bị."""
        target = proxy_id if proxy_id else "DIRECT"
        success = self.select_outbound_via_api(device_ip, target)
        if not success:
            logger.info(f"[Clash] Target '{target}' not in selector '{device_ip}', reloading config...")
            return self.reload_now()
        return True

    @property
    def config(self) -> AppConfig:
        """Cấu hình AppConfig hiện tại."""
        return self._config

    def update_config(self, config: AppConfig):
        """Cập nhật cấu hình AppConfig."""
        self._config = config

    def hot_reload(self) -> bool:
        """Nạp lại cấu hình."""
        return self.reload_now()

    def update_multiple_devices_routing(self, assignments: list) -> bool:
        """Cập nhật định tuyến cho nhiều thiết bị cùng lúc.
        assignments: list of tuple (device_ip, proxy_id) hoặc list of dict {"ip": ..., "proxy_id": ...}
        """
        success_all = True
        for item in assignments:
            # Support both tuple (ip, proxy_id) and dict {"ip": ..., "proxy_id": ...}
            if isinstance(item, (tuple, list)):
                ip, proxy_id = item[0], item[1] if len(item) > 1 else None
            else:
                ip = item.get("ip")
                proxy_id = item.get("proxy_id")
            if ip:
                if not self.update_device_routing(ip, proxy_id):
                    success_all = False
        return success_all

    def get_proxy_delay(self, proxy_name: str, url: str = "http://www.gstatic.com/generate_204", timeout: int = 5000) -> Optional[int]:
        """Đo latency của một proxy qua Clash REST API."""
        try:
            test_url = f"{CLASH_API_URL}/proxies/{proxy_name}/delay?url={url}&timeout={timeout}"
            req = urllib.request.Request(test_url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout / 1000 + 1) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("delay")
        except Exception:
            return None
