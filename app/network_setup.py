"""
PhoneFarm GenRouter v2.0 - Network Setup

Handles Windows-specific network configuration:
- IP Forwarding (Registry)
- Windows Firewall rules
- LAN interface IP setup
- WAN interface auto-detection
- Binary downloads (sing-box.exe, wintun.dll)
"""

import subprocess
import logging
import os
import sys
import platform
import time
import urllib.request
import zipfile
import io
import tempfile

from app.config import PROJECT_DIR

logger = logging.getLogger(__name__)



def _run_ps_script(script: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Helper to run a PowerShell script string using a temporary file to avoid character escaping issues."""
    fd, path = tempfile.mkstemp(suffix=".ps1")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(script)
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


# ─── IP Forwarding ──────────────────────────────────────

def enable_ip_forwarding(lan_interface: str = "Ethernet 3"):
    """Bật IP Packet Forwarding CHỈ trên LAN interface (không bật trên WAN).
    
    Args:
        lan_interface: Tên LAN interface kết nối Aruba AP (ví dụ: 'Ethernet 3').
                       QUAN TRỌNG: KHÔNG bật forwarding trên WAN interface,
                       vì sẽ gây mất internet trên máy chủ.
    """
    if platform.system() != "Windows":
        try:
            subprocess.run("sysctl -w net.ipv4.ip_forward=1", shell=True,
                           capture_output=True, text=True)
            logger.info("[Network] IP forwarding enabled (Linux).")
        except Exception as e:
            logger.error(f"[Network] Failed to enable IP forwarding: {e}")
        return

    # ── Bước 1: Bật trong Registry (persistent, cần restart để có hiệu lực toàn diện) ──
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters",
            0, winreg.KEY_READ
        )
        value, _ = winreg.QueryValueEx(key, "IPEnableRouter")
        winreg.CloseKey(key)
        if value == 1:
            logger.info("[Network] IP Forwarding already enabled in Registry.")
    except Exception:
        logger.info("[Network] Enabling IP Forwarding in Registry...")
        cmd = (
            "powershell -Command \"Set-ItemProperty "
            "-Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters' "
            "-Name 'IPEnableRouter' -Value 1\""
        )
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            logger.warning(
                "[Network] IP Forwarding enabled in Registry. NOTE: Restart Windows once to apply fully."
            )
        else:
            logger.error(
                f"[Network] Failed to enable IP Forwarding in Registry. Run as Administrator. "
                f"Error: {result.stderr}"
            )

    # ── Bước 2: Bật Forwarding runtime CHỈ trên LAN interface ──
    # KHÔNG dùng `-Forwarding Enabled` không có -InterfaceAlias vì sẽ bật trên
    # TẤT CẢ adapters (kể cả WAN), gây mất internet trên máy chủ.
    logger.info(f"[Network] Enabling IP Forwarding runtime on LAN interface '{lan_interface}' only...")
    try:
        cmd_lan = [
            "powershell", "-NoProfile", "-Command",
            f"Set-NetIPInterface -InterfaceAlias '{lan_interface}' "
            f"-AddressFamily IPv4 -Forwarding Enabled -ErrorAction SilentlyContinue"
        ]
        res = subprocess.run(cmd_lan, capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            logger.info(f"[Network] ✓ IP Forwarding enabled on '{lan_interface}'.")
        else:
            logger.warning(f"[Network] Failed to enable forwarding on '{lan_interface}': {res.stderr.strip()}")
    except Exception as e:
        logger.warning(f"[Network] Failed to enable IP Forwarding on LAN interface: {e}")



# ─── Disable WAN Forwarding ────────────────────────────

def ensure_wan_forwarding_disabled(wan_interface: str):
    """Tắt IP Forwarding trên WAN interface để tránh routing loop và mất internet.
    
    Gọi hàm này SAU enable_ip_forwarding() để đảm bảo WAN interface
    không bị bật forwarding ngầm bởi Windows hoặc các service khác.
    
    Args:
        wan_interface: Tên WAN interface có kết nối internet (ví dụ: 'Ethernet').
    """
    if platform.system() != "Windows":
        return
    if not wan_interface:
        return

    logger.info(f"[Network] Ensuring IP Forwarding is DISABLED on WAN interface '{wan_interface}'...")
    try:
        cmd = [
            "powershell", "-NoProfile", "-Command",
            f"Set-NetIPInterface -InterfaceAlias '{wan_interface}' "
            f"-AddressFamily IPv4 -Forwarding Disabled -ErrorAction SilentlyContinue"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            logger.info(f"[Network] ✓ IP Forwarding DISABLED on WAN '{wan_interface}' - máy chủ sẽ không mất internet.")
        else:
            logger.warning(
                f"[Network] Could not disable forwarding on WAN '{wan_interface}': {res.stderr.strip()}"
            )
    except Exception as e:
        logger.warning(f"[Network] Failed to disable WAN forwarding: {e}")


# ─── Windows Firewall ───────────────────────────────────

def setup_firewall_rules():
    """Thiết lập Windows Firewall rules cho GenRouter."""
    if platform.system() != "Windows":
        return

    singbox_exe = os.path.join(PROJECT_DIR, "sing-box.exe")
    python_exe = sys.executable

    logger.info("[Network] Configuring Windows Firewall rules...")

    ps_code = f"""
    $ErrorActionPreference = 'SilentlyContinue'
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8

    # API Ports 8000-9099
    if (-not (Get-NetFirewallRule -DisplayName 'GenRouter API Ports' -ErrorAction SilentlyContinue)) {{
        New-NetFirewallRule -DisplayName 'GenRouter API Ports' -Direction Inbound -Protocol TCP -LocalPort 8000-9099 -Action Allow -Enabled True -Profile Any | Out-Null
    }}

    # DHCP UDP 67/68
    if (-not (Get-NetFirewallRule -DisplayName 'GenRouter DHCP 67' -ErrorAction SilentlyContinue)) {{
        New-NetFirewallRule -DisplayName 'GenRouter DHCP 67' -Direction Inbound -Protocol UDP -LocalPort 67 -Action Allow -Enabled True -Profile Any | Out-Null
    }}
    if (-not (Get-NetFirewallRule -DisplayName 'GenRouter DHCP 68' -ErrorAction SilentlyContinue)) {{
        New-NetFirewallRule -DisplayName 'GenRouter DHCP 68' -Direction Inbound -Protocol UDP -LocalPort 68 -Action Allow -Enabled True -Profile Any | Out-Null
    }}

    # DNS UDP 53 (for DHCP clients)
    if (-not (Get-NetFirewallRule -DisplayName 'GenRouter DNS 53' -ErrorAction SilentlyContinue)) {{
        New-NetFirewallRule -DisplayName 'GenRouter DNS 53' -Direction Inbound -Protocol UDP -LocalPort 53 -Action Allow -Enabled True -Profile Any | Out-Null
    }}

    # Mihomo program
    $mihomoExe = "{os.path.join(PROJECT_DIR, 'mihomo.exe')}"
    if (Test-Path $mihomoExe) {{
        if (-not (Get-NetFirewallRule -DisplayName 'GenRouter Mihomo' -ErrorAction SilentlyContinue)) {{
            New-NetFirewallRule -DisplayName 'GenRouter Mihomo' -Direction Inbound -Program $mihomoExe -Action Allow -Enabled True -Profile Any | Out-Null
        }}
    }}

    Write-Host "OK"
    """

    fd, path = tempfile.mkstemp(suffix=".ps1")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(ps_code)

        cmd = f'powershell -NoProfile -ExecutionPolicy Bypass -File "{path}"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            logger.info("[Network] Firewall rules configured successfully.")
        else:
            logger.error(
                f"[Network] Firewall setup error: {result.stderr}. "
                "Run as Administrator."
            )
    except Exception as e:
        logger.error(f"[Network] Firewall setup exception: {e}")
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


# ─── WAN Interface Detection ────────────────────────────


def _test_interface_connectivity(interface_name: str, timeout_sec: int = 3) -> bool:
    """Kiểm tra thực tế xem interface có internet thật không bằng TCP connect.

    Gửi TCP SYN tới 1.1.1.1:443 qua interface chỉ định. Nếu connect thành công
    trong timeout_sec giây → interface thực sự có internet, không phải chỉ có IP/route.

    Dùng PowerShell Test-NetConnection vì Python socket không bind được theo
    interface name trên Windows (chỉ bind được IP). PowerShell can bind theo
    interface natively.
    """
    if platform.system() != "Windows" or not interface_name:
        return True  # Non-Windows: assume OK

    try:
        # Lấy IP thực của interface để bind socket
        ps = (
            f"$ip = (Get-NetIPAddress -InterfaceAlias '{interface_name}' -AddressFamily IPv4 -EA SilentlyContinue "
            "| Where-Object { -not ($_.IPAddress.StartsWith('169.254.') -or $_.IPAddress.StartsWith('127.')) } "
            "| Select-Object -First 1 -ExpandProperty IPAddress); "
            "if (-not $ip) { Write-Output 'NO_IP'; exit }; "
            "try { "
            "  $tcp = New-Object System.Net.Sockets.TcpClient; "
            "  $ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Parse($ip), 0); "
            "  $tcp.Client.Bind($ep); "
            f"  $ar = $tcp.BeginConnect('1.1.1.1', 443, $null, $null); "
            f"  $ok = $ar.AsyncWaitHandle.WaitOne({timeout_sec * 1000}); "
            "  if ($ok -and $tcp.Connected) { Write-Output 'OK' } else { Write-Output 'TIMEOUT' }; "
            "  $tcp.Close() "
            "} catch { Write-Output \"FAIL:$($_.Exception.Message)\" }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=timeout_sec + 5
        )
        out = result.stdout.strip()
        if out == "OK":
            logger.debug(f"[Network] Connectivity test OK: '{interface_name}' -> 1.1.1.1:443")
            return True
        else:
            logger.info(f"[Network] Connectivity test FAILED: '{interface_name}' -> {out}")
            return False
    except Exception as e:
        logger.debug(f"[Network] Connectivity test error for '{interface_name}': {e}")
        return False


def _check_media_connect_state(interface_name: str) -> str:
    """Kiểm tra MediaConnectState thực tế của adapter.

    Returns: '1' (Connected), '0' (Disconnected), '2' (Unknown), '' (error)
    Khác với Status=='Up': MediaConnectState kiểm tra physical link layer,
    ví dụ cáp Ethernet thực sự có tín hiệu hay chưa.
    """
    if platform.system() != "Windows" or not interface_name:
        return "1"  # assume connected
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetAdapter -Name '{interface_name}' -EA SilentlyContinue).MediaConnectionState"],
            capture_output=True, text=True, timeout=5
        )
        state = r.stdout.strip()
        # Windows returns: 1=Connected, 0=Disconnected, 2=Unknown
        # Or localized strings: 'Connected', 'Disconnected'
        if state in ('1', 'Connected'):
            return '1'
        elif state in ('0', 'Disconnected', '2', 'Unknown'):
            return '0'
        return state
    except Exception:
        return ''


def detect_wan_interface() -> str:
    """Auto-detect WAN interface - uu tien Ethernet (802.3) > WiFi.

    Logic:
    1. Lay tat ca interfaces co default route (0.0.0.0/0) voi NextHop hop le
    2. Uu tien: 802.3 Ethernet > NativeWifi > others
    3. Trong cung loai: chon metric thap nhat
    4. Fallback: metric thap nhat bat ke loai
    """
    if platform.system() != "Windows":
        try:
            result = subprocess.run(
                "ip route show default | head -1 | awk '{print $5}'",
                shell=True, capture_output=True, text=True, timeout=5
            )
            iface = result.stdout.strip()
            return iface if iface else "eth0"
        except Exception:
            return "eth0"

    try:
        # Lay tat ca candidates co default route, adapter Status=Up va co IP IPv4 thuc
        ps = (
            "$routes = Get-NetRoute -DestinationPrefix '0.0.0.0/0' "
            "| Where-Object {$_.NextHop -ne '0.0.0.0'} "
            "| Select-Object InterfaceAlias, "
            "@{N='TotalMetric';E={$_.RouteMetric + $_.InterfaceMetric}};"
            "foreach ($r in $routes) {"
            "  $a = Get-NetAdapter -Name $r.InterfaceAlias -ErrorAction SilentlyContinue;"
            "  if ($a -and $a.Status -eq 'Up') {"
            "    $ip = Get-NetIPAddress -InterfaceAlias $r.InterfaceAlias -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { -not ($_.IPAddress.StartsWith('169.254.') -or $_.IPAddress.StartsWith('127.')) };"
            "    if ($ip) {"
            "      Write-Output \"$($r.InterfaceAlias)|$($a.PhysicalMediaType)|$($r.TotalMetric)\""
            "    }"
            "  }"
            "}"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=8
        )
        lines = [l.strip() for l in result.stdout.strip().splitlines() if '|' in l]

        if not lines:
            # Không có default route → scan adapter đang Up có IPv4 thật
            logger.warning("[Network] No default route found, scanning for first Up adapter with real IPv4...")
            try:
                ps_fallback = (
                    "Get-NetAdapter | Where-Object { $_.Status -eq 'Up' -and "
                    "$_.InterfaceDescription -notlike '*Wintun*' -and "
                    "$_.InterfaceDescription -notlike '*Hyper-V*' -and "
                    "$_.InterfaceDescription -notlike '*VMware*' -and "
                    "$_.InterfaceDescription -notlike '*Loopback*' -and "
                    "$_.InterfaceDescription -notlike '*Bluetooth*' -and "
                    "$_.InterfaceDescription -notlike '*Wi-Fi Direct*' "
                    "} | ForEach-Object { "
                    "  $ip = Get-NetIPAddress -InterfaceAlias $_.Name -AddressFamily IPv4 -EA SilentlyContinue | "
                    "    Where-Object { -not ($_.IPAddress.StartsWith('169.254.') -or $_.IPAddress.StartsWith('127.')) }; "
                    "  if ($ip) { Write-Output \"$($_.Name)|$($_.PhysicalMediaType)\" } "
                    "}"
                )
                fb_res = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_fallback],
                    capture_output=True, text=True, timeout=8
                )
                fb_lines = [l.strip() for l in fb_res.stdout.strip().splitlines() if '|' in l]
                # Ưu tiên Ethernet > WiFi trong fallback
                fb_eth, fb_wifi = [], []
                for fl in fb_lines:
                    fp = fl.split('|')
                    nm, mt = fp[0], fp[1].lower() if len(fp) > 1 else ''
                    if '802.3' in mt or 'ethernet' in mt:
                        fb_eth.append(nm)
                    elif 'wifi' in mt or '802.11' in mt or 'nativewifi' in mt:
                        fb_wifi.append(nm)
                for lst in [fb_eth, fb_wifi]:
                    if lst:
                        logger.info(f"[Network] Fallback WAN: '{lst[0]}' (no default route but Up with IPv4)")
                        return lst[0]
            except Exception:
                pass
            logger.warning("[Network] No Up adapter found at all, using 'Wi-Fi' as last resort")
            return "Wi-Fi"

        ethernet_candidates = []  # 802.3
        wifi_candidates = []      # NativeWifi / 802.11
        other_candidates = []     # anything else

        for line in lines:
            parts = line.split('|')
            if len(parts) < 3:
                continue
            name, media_type, metric_str = parts[0], parts[1], parts[2]
            try:
                metric = int(metric_str)
            except ValueError:
                metric = 9999
            media_lower = media_type.lower()
            if '802.3' in media_lower or 'ethernet' in media_lower:
                ethernet_candidates.append((metric, name))
            elif 'wifi' in media_lower or '802.11' in media_lower or 'nativewifi' in media_lower:
                wifi_candidates.append((metric, name))
            else:
                other_candidates.append((metric, name))

        # Chon theo priority: Ethernet > WiFi > other, trong cung loai chon metric thap nhat
        # QUAN TRONG: verify connectivity that su — khong chi check route/Up
        for candidates in [ethernet_candidates, wifi_candidates, other_candidates]:
            if candidates:
                for metric_val, chosen in sorted(candidates):
                    if _test_interface_connectivity(chosen, timeout_sec=3):
                        logger.info(f"[Network] Auto-detected WAN: '{chosen}' (connectivity verified)")
                        return chosen
                    else:
                        logger.warning(f"[Network] WAN candidate '{chosen}' has route but NO real internet — skipping")

        # Neu khong candidate nao co internet that, chon metric thap nhat (best effort)
        all_candidates = sorted(ethernet_candidates + wifi_candidates + other_candidates)
        if all_candidates:
            _, fallback = all_candidates[0]
            logger.warning(f"[Network] No candidate has verified internet — using '{fallback}' (best metric, unverified)")
            return fallback

    except Exception as e:
        logger.warning(f"[Network] detect_wan_interface error: {e}")

    return "Wi-Fi"


def detect_lan_interface(exclude_interface: str = "") -> str:
    """Auto-detect LAN interface voi 4-tier priority fallback.

    Priority:
    1. Ethernet (802.3) khac WAN, dang UP, KHONG co default route
       (co default route = dang lam WAN, khong dung lam LAN)
    2. Ethernet (802.3) khac WAN, dang UP (du co route, nhung khac WAN duoc chi ro)
    3. WiFi adapter thu 2 (NativeWifi) khac WAN, dang UP
       -> se trigger wifi_hotspot_enabled auto
    4. Fallback cung ten cu / 'Ethernet 3'

    Returns: (interface_name, needs_hotspot)
             needs_hotspot=True neu chon WiFi adapter (can bat hotspot)
    Giu API cu -> tra ve str de khong break code hien tai.
    Dung smart_detect_lan() de lay ca needs_hotspot.
    """
    result, _ = smart_detect_lan(exclude_interface=exclude_interface)
    return result


def smart_detect_lan(exclude_interface: str = "") -> tuple:
    """Smart LAN detection - tra ve (interface_name: str, needs_hotspot: bool).

    needs_hotspot=True co nghia la interface la WiFi card (khong phai Ethernet),
    caller nen tu dong bat wifi_hotspot_enabled trong config.

    Priority:
    1. Ethernet khac WAN, UP, khong co default route (pure LAN card)
    2. Ethernet khac WAN, UP (du co route nhung khac WAN)
    3. WiFi adapter thu 2 khac WAN, UP -> needs_hotspot=True
    4. Fallback 'Ethernet 3' / ten cu
    """
    if platform.system() != "Windows":
        return ("eth1", False)

    try:
        # Lay tat ca physical adapters (tru virtual/bluetooth)
        ps = (
            "$wan = '" + exclude_interface.replace("'", "''") + "';"
            "$interfaces_with_route = (Get-NetRoute -DestinationPrefix '0.0.0.0/0' "
            "| Where-Object {$_.NextHop -ne '0.0.0.0'} "
            "| Select-Object -ExpandProperty InterfaceAlias);"
            "Get-NetAdapter | Where-Object {"
            "  $_.Status -eq 'Up' -and"
            "  $_.Name -ne $wan -and"
            "  $_.InterfaceDescription -notlike '*Wintun*' -and"
            "  $_.InterfaceDescription -notlike '*Hyper-V*' -and"
            "  $_.InterfaceDescription -notlike '*VMware*' -and"
            "  $_.InterfaceDescription -notlike '*Loopback*' -and"
            "  $_.InterfaceDescription -notlike '*Hosted Network*' -and"
            "  $_.InterfaceDescription -notlike '*Wi-Fi Direct Virtual*' -and"
            "  $_.InterfaceDescription -notlike '*Bluetooth*'"
            "} | ForEach-Object {"
            "  $hasRoute = ($interfaces_with_route -contains $_.Name);"
            "  Write-Output \"$($_.Name)|$($_.PhysicalMediaType)|$hasRoute\""
            "}"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=8
        )
        lines = [l.strip() for l in result.stdout.strip().splitlines() if '|' in l]

        ethernet_no_route = []   # Tier 1: Ethernet thuan LAN (khong co WAN route)
        ethernet_any = []        # Tier 2: Ethernet bat ky
        wifi_adapters = []       # Tier 3: WiFi card thu 2 (khac WAN)

        for line in lines:
            parts = line.split('|')
            if len(parts) < 3:
                continue
            name, media_type, has_route_str = parts[0], parts[1], parts[2]
            has_route = has_route_str.strip().lower() == 'true'
            media_lower = media_type.lower()

            is_ethernet = '802.3' in media_lower or 'ethernet' in media_lower
            is_wifi = 'wifi' in media_lower or '802.11' in media_lower or 'nativewifi' in media_lower

            if is_ethernet:
                if not has_route:
                    ethernet_no_route.append(name)
                ethernet_any.append(name)
            elif is_wifi:
                wifi_adapters.append(name)

        # Tier 1: Ethernet thuan LAN (uu tien nhat - khong co WAN route)
        if ethernet_no_route:
            chosen = ethernet_no_route[0]
            logger.info(f"[Network] Smart LAN detect Tier1: Ethernet (no-WAN-route) = '{chosen}'")
            return (chosen, False)

        # Tier 2: Ethernet bat ky (khac WAN)
        if ethernet_any:
            chosen = ethernet_any[0]
            logger.info(f"[Network] Smart LAN detect Tier2: Ethernet = '{chosen}'")
            return (chosen, False)

        # Tier 3: WiFi card thu 2 (khac WAN, la card vat ly rieng)
        if wifi_adapters:
            chosen = wifi_adapters[0]
            logger.info(f"[Network] Smart LAN detect Tier3: 2nd WiFi '{chosen}' -> hotspot")
            return (chosen, True)  # needs_hotspot=True

        # Tier 4: WAN chinh la WiFi -> dung chinh no de phat hotspot
        # Windows tao "Microsoft Wi-Fi Direct Virtual Adapter" de bridge traffic.
        # Khong can USB WiFi thu 2 - 1 card WiFi vua ket noi internet vua phat hotspot.
        if exclude_interface:
            wan_media = _get_adapter_media_type(exclude_interface)
            wan_is_wifi = ('wifi' in wan_media.lower() or '802.11' in wan_media.lower()
                           or 'nativewifi' in wan_media.lower())
            if wan_is_wifi:
                logger.info(
                    f"[Network] Smart LAN detect Tier4: WAN '{exclude_interface}' is WiFi "
                    f"-> same card can host hotspot (Virtual Adapter will be created)"
                )
                return (exclude_interface, True)  # needs_hotspot=True, LAN=virtual adapter sau

    except Exception as e:
        logger.warning(f"[Network] smart_detect_lan error: {e}")

    # FALLBACK: Không tìm thấy LAN card nào Up
    # Thay vì hardcode "Ethernet 3", check xem WAN có phải WiFi không → trigger Tier 4 hotspot
    if exclude_interface:
        try:
            wan_media = _get_adapter_media_type(exclude_interface)
            wan_is_wifi = ('wifi' in wan_media.lower() or '802.11' in wan_media.lower()
                           or 'nativewifi' in wan_media.lower())
            if wan_is_wifi:
                logger.info(
                    f"[Network] Smart LAN detect FALLBACK → Tier4: WAN '{exclude_interface}' is WiFi "
                    f"→ same card can host hotspot (no physical LAN port detected)"
                )
                return (exclude_interface, True)  # needs_hotspot=True
        except Exception:
            pass

    logger.warning("[Network] No suitable LAN interface found and WAN is not WiFi — returning empty (degraded mode)")
    return ("", False)


def _get_adapter_media_type(adapter_name: str) -> str:
    """Lay PhysicalMediaType cua adapter (e.g. '802.3', 'NativeWifi'). Tra ve '' neu loi."""
    if not adapter_name or platform.system() != "Windows":
        return ""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetAdapter -Name '{adapter_name}' -ErrorAction SilentlyContinue).PhysicalMediaType"],
            capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip()
    except Exception:
        return ""



def is_lan_interface_valid(interface_name: str, wan_interface: str = "",
                           allow_wan_hotspot: bool = False) -> bool:
    """Kiem tra LAN interface con ton tai, co cable/link that su, khong trung WAN.

    2-layer verification:
    1. Adapter ton tai va Status == 'Up'
    2. MediaConnectState == 'Connected' (cable that su cam vao, co link signal)
       Tranh truong hop adapter 'Up' nhung cable bi rut hoac port loi.

    allow_wan_hotspot=True: khi wifi_hotspot_enabled=True va lan=wan la VALID
    (Tier 4 hotspot mode — sau khi hotspot bat se update sang virtual adapter).
    """
    if platform.system() != "Windows" or not interface_name:
        return False

    if wan_interface and interface_name == wan_interface:
        # Tier 4 hotspot mode: lan==wan la okay khi cho phep
        if allow_wan_hotspot:
            logger.debug(f"[Network] LAN==WAN ('{interface_name}') allowed in hotspot mode.")
            return True
        return False

    try:
        # Check ca Status va MediaConnectState trong 1 lenh
        ps = (
            f"$a = Get-NetAdapter -Name '{interface_name}' -EA SilentlyContinue; "
            f"if (-not $a) {{ Write-Output 'NOT_FOUND' }} "
            f"elseif ($a.Status -ne 'Up') {{ Write-Output \"DOWN:$($a.Status)\" }} "
            f"elseif ($a.MediaConnectionState -and $a.MediaConnectionState -ne 'Connected' -and $a.MediaConnectionState -ne 1) {{ Write-Output \"NO_LINK:$($a.MediaConnectionState)\" }} "
            f"else {{ Write-Output 'OK' }}"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=5
        )
        out = result.stdout.strip()
        if out == 'OK':
            return True
        elif out.startswith('NO_LINK'):
            logger.info(
                f"[Network] LAN '{interface_name}' adapter Up but cable NOT connected "
                f"(MediaConnectState={out.split(':',1)[1]}) — invalid"
            )
            return False
        elif out.startswith('DOWN'):
            logger.info(f"[Network] LAN '{interface_name}' is {out.split(':',1)[1]} — invalid")
            return False
        else:
            logger.info(f"[Network] LAN '{interface_name}': {out}")
            return False
    except Exception as e:
        logger.warning(f"[Network] Failed to verify LAN interface '{interface_name}': {e}")
        return True  # Khong xac minh duoc thi tin theo config, tranh false positive


def is_wan_interface_valid(interface_name: str) -> bool:
    """Kiểm tra interface có đang thực sự là đường ra internet không.

    3-layer verification:
    1. Adapter UP + có IP IPv4 thật (không APIPA)
    2. Có default route với NextHop thật
    3. TCP connect thực tế tới 1.1.1.1:443 (verify internet thật, không đoán mò)
    """
    if platform.system() != "Windows" or not interface_name:
        return True

    try:
        # Layer 1+2: Check adapter Up, real IPv4, default route
        ps = (
            f"$a = Get-NetAdapter -Name '{interface_name}' -ErrorAction SilentlyContinue; "
            f"if (-not $a -or $a.Status -ne 'Up') {{ Write-Output 'NO_UP'; exit }}; "
            f"$ip = Get-NetIPAddress -InterfaceAlias '{interface_name}' -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {{ -not ($_.IPAddress.StartsWith('169.254.') -or $_.IPAddress.StartsWith('127.')) }}; "
            f"if (-not $ip) {{ Write-Output 'NO_IP'; exit }}; "
            f"$r = Get-NetRoute -InterfaceAlias '{interface_name}' -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Where-Object {{ $_.NextHop -ne '0.0.0.0' }}; "
            f"if (-not $r) {{ Write-Output 'NO_ROUTE'; exit }}; "
            f"Write-Output 'YES'"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=5
        )
        basic_check = result.stdout.strip()
        if "YES" not in basic_check:
            logger.info(f"[Network] WAN '{interface_name}' basic check failed: {basic_check}")
            return False

        # Layer 3: TCP connectivity test — verify actual internet
        has_internet = _test_interface_connectivity(interface_name, timeout_sec=4)
        if not has_internet:
            logger.warning(
                f"[Network] WAN '{interface_name}' has route+IP but NO real internet "
                f"(TCP 1.1.1.1:443 failed) — marking as INVALID"
            )
            return False

        logger.debug(f"[Network] WAN '{interface_name}' fully verified (Up + IP + route + internet)")
        return True
    except Exception as e:
        logger.warning(f"[Network] Failed to verify WAN interface '{interface_name}': {e}")
        return True


def detect_wan_subnet() -> str:
    """Detect WAN subnet to exclude from TUN routing."""
    if platform.system() != "Windows":
        return "0.0.0.0/0"

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$r = Get-NetRoute -DestinationPrefix '0.0.0.0/0' | "
             "Where-Object {$_.NextHop -ne '0.0.0.0'} | "
             "Sort-Object @{Expression={$_.RouteMetric + $_.InterfaceMetric}} | Select-Object -First 1; "
             "$ip = (Get-NetIPAddress -InterfaceIndex $r.InterfaceIndex "
             "-AddressFamily IPv4 | Select-Object -First 1); "
             "$parts = $ip.IPAddress.Split('.'); "
             "\"$($parts[0]).$($parts[1]).$($parts[2]).0/$($ip.PrefixLength)\""],
            capture_output=True, text=True, timeout=5
        )
        subnet = result.stdout.strip()
        if subnet and "/" in subnet:
            logger.info(f"[Network] WAN subnet: {subnet}")
            return subnet
    except Exception as e:
        logger.warning(f"[Network] Failed to detect WAN subnet: {e}")

    return "192.168.100.0/24"


def get_lan_ip(interface_name: str) -> str:
    """Get IP address of LAN interface."""
    if platform.system() != "Windows":
        return "192.168.10.1"

    cmd = (
        f"powershell -NoProfile -Command "
        f"\"(Get-NetIPAddress -InterfaceAlias '{interface_name}' "
        f"-AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress\""
    )
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
        ip = res.stdout.strip()
        if ip and not ip.startswith("Error"):
            return ip
    except Exception as e:
        logger.error(f"[Network] Error getting LAN IP for {interface_name}: {e}")

    return "192.168.10.1"


def _subnet_mask_to_prefix(mask: str) -> int:
    """Chuyển subnet mask dạng chấm (vd '255.255.255.0') sang prefix length (vd 24)."""
    try:
        return sum(bin(int(octet)).count("1") for octet in mask.strip().split("."))
    except Exception:
        return 24


def ensure_lan_ip_assigned(lan_interface: str, lan_gateway_ip: str, subnet_mask: str = "255.255.255.0") -> bool:
    """Đảm bảo LAN interface có đúng IP tĩnh mong muốn — tự gán nếu chưa có/sai.

    QUAN TRỌNG: đây là bước mà tool desktop (router_automation.py) luôn tự chạy trước
    khi khởi động c69-router (New-NetIPAddress). Khi chạy c69-router ĐỘC LẬP (không qua
    tool), trước đây main.py chỉ ĐỌC IP hiện tại (get_lan_ip/check_lan_interface_health)
    để cảnh báo/tự sửa config cho khớp thực tế, chứ không tự gán IP — nếu card LAN chưa
    từng được gán IP tĩnh thủ công, DHCP server sẽ chạy sai gateway và thiết bị kết nối
    vào sẽ không có mạng. Hàm này lấp khoảng trống đó để chạy độc lập hoạt động giống
    hệt khi chạy qua tool.

    Trả về True nếu IP đã đúng sẵn hoặc gán thành công, False nếu gán thất bại.
    """
    if platform.system() != "Windows":
        return True

    current_ip = get_lan_ip(lan_interface)
    if current_ip == lan_gateway_ip:
        logger.info(f"[Network] LAN interface '{lan_interface}' đã có đúng IP {lan_gateway_ip}, bỏ qua gán lại.")
        return True

    logger.warning(
        f"[Network] LAN interface '{lan_interface}' đang có IP '{current_ip}' "
        f"(mong đợi '{lan_gateway_ip}') — tự động gán lại IP tĩnh..."
    )
    prefix_len = _subnet_mask_to_prefix(subnet_mask)
    try:
        cmd = [
            "powershell", "-NoProfile", "-Command",
            f"Remove-NetIPAddress -InterfaceAlias '{lan_interface}' -AddressFamily IPv4 -Confirm:$false -ErrorAction SilentlyContinue; "
            f"New-NetIPAddress -InterfaceAlias '{lan_interface}' -IPAddress '{lan_gateway_ip}' "
            f"-PrefixLength {prefix_len} -ErrorAction Stop"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            logger.info(f"[Network] Đã gán IP {lan_gateway_ip}/{prefix_len} cho '{lan_interface}'.")
            return True
        logger.error(f"[Network] Gán IP tĩnh thất bại cho '{lan_interface}': {res.stderr.strip()[:300]}")
        return False
    except Exception as e:
        logger.error(f"[Network] Exception khi gán IP tĩnh cho '{lan_interface}': {e}")
        return False


# ─── Binary Downloads ───────────────────────────────────


def clear_interface_dns(lan_interface: str = "", tun_interface: str = "", wan_interface: str = "") -> bool:
    """Xóa sạch toàn bộ DNS server trên LAN và TUN adapter để đảm bảo DNS luôn rỗng (EMPTY).
    Đảm bảo Host PC chỉ dùng 100% DNS của card WAN thật (Ethernet), không bị DNS multi-homed conflict.
    QUAN TRỌNG: KHÔNG xóa DNS trên WAN interface — nếu xóa, Host PC mất DNS ngay.
    """
    if platform.system() != "Windows":
        return True

    # Chỉ xóa DNS trên interface được chỉ định — KHÔNG hardcode interface name
    target_interfaces = set(filter(None, [lan_interface, tun_interface]))
    # BẢO VỆ WAN: loại bỏ WAN interface ra khỏi danh sách xóa DNS
    if wan_interface:
        target_interfaces.discard(wan_interface)
    if not target_interfaces:
        logger.info("[DNS] No non-WAN interfaces to clear DNS on, skipping.")
        return True
    for iface in target_interfaces:
        try:
            ps = (
                f"Set-DnsClientServerAddress -InterfaceAlias '{iface}' -ResetServerAddresses -ErrorAction SilentlyContinue; "
                f"Set-DnsClientServerAddress -InterfaceAlias '{iface}' -ServerAddresses @() -ErrorAction SilentlyContinue; "
                f"netsh interface ipv4 delete dns name='{iface}' all 2>&1 | Out-Null"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=5)
            logger.info(f"[DNS] ✓ Cleared DNS on '{iface}' (EMPTY)")
        except Exception as e:
            logger.warning(f"[DNS] Failed to clear DNS on '{iface}': {e}")
    logger.info(f"[DNS] WAN interface '{wan_interface}' DNS PROTECTED (not touched).")
    return True


def setup_interface_dns(lan_interface: str, tun_interface: str, primary_dns: str = "", secondary_dns: str = "", wan_interface: str = "") -> bool:
    """Alias to clear_interface_dns for backwards compatibility."""
    return clear_interface_dns(lan_interface=lan_interface, tun_interface=tun_interface, wan_interface=wan_interface)


def download_binaries():
    """Extract bundled binaries (from PyInstaller) or download mihomo.exe/wintun.dll/sing-box.exe if missing."""
    if platform.system() != "Windows":
        return

    # 1. Trích xuất trực tiếp từ RESOURCE_DIR (PyInstaller _MEIPASS) nếu có
    from app.config import RESOURCE_DIR
    for fn in ["mihomo.exe", "sing-box.exe", "wintun.dll", "geoip.metadb"]:
        src = os.path.join(RESOURCE_DIR, fn)
        dst = os.path.join(PROJECT_DIR, fn)
        if not os.path.exists(dst) and os.path.exists(src):
            try:
                import shutil
                shutil.copy2(src, dst)
                logger.info(f"[Network] Extracted bundled '{fn}' to {PROJECT_DIR}")
            except Exception as e:
                logger.warning(f"[Network] Could not extract bundled {fn}: {e}")

    mihomo_exe = os.path.join(PROJECT_DIR, "mihomo.exe")
    singbox_exe = os.path.join(PROJECT_DIR, "sing-box.exe")
    wintun_dll = os.path.join(PROJECT_DIR, "wintun.dll")

    import ssl
    context = ssl.create_default_context()

    if not os.path.exists(mihomo_exe):
        logger.info("[Network] Downloading mihomo.exe from GitHub...")
        url = "https://github.com/MetaCubeX/mihomo/releases/download/v1.19.29/mihomo-windows-amd64-v1.19.29.zip"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, context=context) as response:
                zip_data = response.read()
            with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
                for name in z.namelist():
                    if name.endswith(".exe"):
                        with open(mihomo_exe, "wb") as f:
                            f.write(z.read(name))
                        logger.info("[Network] mihomo.exe downloaded successfully!")
                        break
        except Exception as e:
            logger.error(f"[Network] Failed to download mihomo.exe: {e}")

    if not os.path.exists(singbox_exe):
        logger.info("[Network] Downloading sing-box.exe from GitHub...")
        url = "https://github.com/SagerNet/sing-box/releases/download/v1.13.14/sing-box-1.13.14-windows-amd64.zip"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, context=context) as response:
                zip_data = response.read()
            with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
                for name in z.namelist():
                    if name.endswith("sing-box.exe"):
                        with open(singbox_exe, "wb") as f:
                            f.write(z.read(name))
                        logger.info("[Network] sing-box.exe downloaded successfully!")
                        break
        except Exception as e:
            logger.error(f"[Network] Failed to download sing-box.exe: {e}")

    if not os.path.exists(wintun_dll):
        logger.info("[Network] Downloading wintun.dll...")
        url = "https://www.wintun.net/builds/wintun-0.14.1.zip"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, context=context) as response:
                zip_data = response.read()
            with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
                for name in z.namelist():
                    if name.endswith("wintun/bin/amd64/wintun.dll"):
                        with open(wintun_dll, "wb") as f:
                            f.write(z.read(name))
                        logger.info("[Network] wintun.dll downloaded successfully!")
                        break
        except Exception as e:
            logger.error(f"[Network] Failed to download wintun.dll: {e}")


# ─── Adjust Interface Metric ────────────────────────────

def adjust_lan_interface_metric(interface_name: str):
    """Set LAN interface metric to 500 (HIGH) to ensure it NEVER outranks WAN in routing.
    
    QUAN TRỌNG: Metric phải CAO HƠN WAN (WAN auto-metric thường 25-55).
    Nếu metric LAN thấp hơn WAN, Windows sẽ route traffic qua LAN (không có internet)
    thay vì WAN → Host PC mất mạng.
    """
    if platform.system() != "Windows":
        return
    logger.info(f"[Network] Adjusting metric for LAN interface '{interface_name}' to 500 (high, below WAN)...")
    try:
        # Run PowerShell commands to set manual metric of 500 on Active and Persistent stores
        cmd = [
            "powershell", "-NoProfile", "-Command",
            f"Set-NetIPInterface -InterfaceAlias '{interface_name}' -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 500 -PolicyStore ActiveStore -ErrorAction SilentlyContinue; "
            f"Set-NetIPInterface -InterfaceAlias '{interface_name}' -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 500 -PolicyStore PersistentStore -ErrorAction SilentlyContinue"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            logger.info(f"[Network] Successfully adjusted metric for '{interface_name}' to 500.")
        else:
            logger.error(f"[Network] Failed to adjust interface metric: {res.stderr}")
    except Exception as e:
        logger.error(f"[Network] Exception adjusting interface metric for '{interface_name}': {e}")


# ─── LAN Interface Health Check ──────────────────────────

def check_lan_interface_health(interface_name: str = "Ethernet 3") -> dict:
    """Check health status of LAN interface connected to Aruba AP.
    
    Returns a dict with:
        - status: "healthy" | "disconnected" | "no_ip" | "error"
        - connection_state: "Up" | "Disconnected" | "Unknown"
        - ip_address: str or None
        - link_speed: str or None
        - message: human-readable diagnostic message
        - suggestions: list of actionable fix suggestions
    """
    result = {
        "status": "error",
        "connection_state": "Unknown",
        "ip_address": None,
        "link_speed": None,
        "mac_address": None,
        "interface_name": interface_name,
        "message": "",
        "suggestions": [],
    }

    if platform.system() != "Windows":
        result["status"] = "healthy"
        result["message"] = "Non-Windows platform, skipping check."
        return result

    # Step 1: Check if interface exists and its connection state
    try:
        cmd = [
            "powershell", "-NoProfile", "-Command",
            f"$a = Get-NetAdapter -Name '{interface_name}' -ErrorAction SilentlyContinue; "
            f"if ($a) {{ \"$($a.Status)|$($a.LinkSpeed)|$($a.MacAddress)\" }} "
            f"else {{ 'NOT_FOUND' }}"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        output = res.stdout.strip()

        if output == "NOT_FOUND" or not output:
            result["status"] = "error"
            result["message"] = (
                f"Không tìm thấy card mạng '{interface_name}'. "
                f"Kiểm tra lại tên card trong Device Manager."
            )
            result["suggestions"] = [
                "Chạy 'Get-NetAdapter' trong PowerShell để xem danh sách card mạng",
                "Cập nhật tên card trong cấu hình GenRouter (lan_interface)",
            ]
            return result

        parts = output.split("|")
        conn_state = parts[0].strip() if len(parts) > 0 else "Unknown"
        link_speed = parts[1].strip() if len(parts) > 1 else None
        mac_addr = parts[2].strip().replace("-", ":") if len(parts) > 2 else None

        result["connection_state"] = conn_state
        result["link_speed"] = link_speed
        result["mac_address"] = mac_addr

        if conn_state.lower() != "up":
            result["status"] = "disconnected"
            result["message"] = (
                f"⚠ Card mạng '{interface_name}' đang DISCONNECTED! "
                f"Thiết bị Aruba kết nối qua cổng này sẽ KHÔNG CÓ MẠNG."
            )
            result["suggestions"] = [
                "Kiểm tra cáp mạng từ máy tính → Aruba (cổng E0/POE)",
                "Kiểm tra Aruba có đang bật nguồn không (đèn LED)",
                "Thử đổi dây mạng hoặc cổng khác trên máy tính",
                "Chạy: Get-NetAdapter -Name 'Ethernet 3' | Select Status",
            ]
            return result

    except subprocess.TimeoutExpired:
        result["message"] = "Timeout kiểm tra trạng thái card mạng."
        return result
    except Exception as e:
        result["message"] = f"Lỗi kiểm tra card mạng: {e}"
        return result

    # Step 2: Check if interface has a valid IP in the expected subnet
    try:
        cmd = [
            "powershell", "-NoProfile", "-Command",
            f"(Get-NetIPAddress -InterfaceAlias '{interface_name}' "
            f"-AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        ip = res.stdout.strip()

        if ip and not ip.startswith("169.254"):
            result["ip_address"] = ip
            result["status"] = "healthy"
            result["message"] = (
                f"✓ Card mạng '{interface_name}' hoạt động bình thường. "
                f"IP: {ip}, Link: {link_speed}"
            )
        elif ip and ip.startswith("169.254"):
            result["ip_address"] = ip
            result["status"] = "no_ip"
            result["message"] = (
                f"⚠ Card '{interface_name}' đang Up nhưng chỉ có IP tự cấp ({ip}). "
                f"Cần gán IP tĩnh 192.168.10.1/24."
            )
            result["suggestions"] = [
                "Chạy: New-NetIPAddress -InterfaceAlias 'Ethernet 3' "
                "-IPAddress '192.168.10.1' -PrefixLength 24",
            ]
        else:
            result["status"] = "no_ip"
            result["message"] = (
                f"⚠ Card '{interface_name}' đang Up nhưng không có IPv4. "
                f"Cần gán IP tĩnh."
            )
            result["suggestions"] = [
                "Chạy: New-NetIPAddress -InterfaceAlias 'Ethernet 3' "
                "-IPAddress '192.168.10.1' -PrefixLength 24",
            ]

    except Exception as e:
        if result["connection_state"].lower() == "up":
            result["status"] = "no_ip"
            result["message"] = f"Card '{interface_name}' Up nhưng lỗi kiểm tra: {e}"
        else:
            result["status"] = "error"
            result["message"] = f"Lỗi kiểm tra card mạng: {e}"
        return result

    # Nhánh thành công của Step 2 (không exception) rơi tới đây — thiếu return khiến hàm
    # trả về None ngầm định đúng lúc interface UP + có IP hợp lệ (trường hợp bình thường
    # nhất), làm main.py lifespan crash ngay khi khởi động với
    # "TypeError: 'NoneType' object is not subscriptable" tại lan_health["status"].
    return result


def _enable_hyperv_via_dism_packages() -> str:
    """Bật Hyper-V trên Windows HOME bằng cách cài trực tiếp các CBS package Hyper-V
    đã có sẵn trên đĩa (`%SystemRoot%\\servicing\\Packages\\*Hyper-V*.mum`) rồi bật
    tính năng với /LimitAccess — bỏ qua việc Windows Update chặn nguồn cài cho bản Home.

    LƯU Ý: đây là thủ thuật KHÔNG chính thức từ Microsoft (Hyper-V không được công bố
    hỗ trợ trên Windows Home), nhưng rủi ro thấp vì chỉ cài lại đúng file Windows đã có
    sẵn trên máy (mọi edition Windows dùng chung 1 bộ cài, chỉ khác tính năng được mở
    theo giấy phép) — được cộng đồng dùng phổ biến (Hyper-V/WSL2/Windows Sandbox trên
    Home). Chấp nhận theo yêu cầu vì đa số máy khách triển khai thực tế chạy Windows Home.
    Trả về "reboot" | "ok" | "error".
    """
    result = _run_ps_script(r"""
    $ErrorActionPreference = 'SilentlyContinue'
    $pkgDir = "$env:SystemRoot\servicing\Packages"
    $hvPackages = @(Get-ChildItem -Path $pkgDir -Filter "*Hyper-V*.mum" -ErrorAction SilentlyContinue)
    if ($hvPackages.Count -eq 0) {
        Write-Output "ERROR: Khong tim thay Hyper-V component packages trong $pkgDir"
    } else {
        foreach ($pkg in $hvPackages) {
            & dism.exe /online /norestart /add-package:"$($pkg.FullName)" | Out-Null
        }
        & dism.exe /online /enable-feature /featurename:Microsoft-Hyper-V-All /LimitAccess /ALL /NoRestart | Out-Null
        & dism.exe /online /enable-feature /featurename:Microsoft-Hyper-V /LimitAccess /ALL /NoRestart | Out-Null
        $code = $LASTEXITCODE
        if ($code -eq 0) {
            Write-Output "OK"
        } elseif ($code -eq 3010) {
            Write-Output "REBOOT_NEEDED"
        } else {
            Write-Output "ERROR: dism enable-feature exit code $code"
        }
    }
    """, timeout=300)
    output = (result.stdout or "").strip()
    if "REBOOT_NEEDED" in output:
        return "reboot"
    if "OK" in output:
        return "ok"
    logger.error(f"[Network] Bật Hyper-V qua DISM package (Windows Home) thất bại: {output or result.stderr}")
    return "error"


def _ensure_hyperv_enabled() -> str:
    """Tự bật tính năng Windows "Hyper-V" nếu chưa bật — XÁC NHẬN TRÊN MÁY THẬT: đây
    mới là nguyên nhân gốc THẬT SỰ của lỗi "Invalid class" (class WMI MSFT_NetNat phụ
    thuộc nền tảng Hyper-V, không chỉ riêng service WinNat/BFE như các chẩn đoán trước).
    Trước đây phải hướng dẫn khách tự vào Settings → Windows Features → tick Hyper-V,
    rất bất tiện với khách không rành kỹ thuật — nên tự động hoá bằng DISM. Đa số máy
    khách triển khai thực tế chạy Windows Home nên có thêm nhánh bật qua DISM package
    trực tiếp (xem _enable_hyperv_via_dism_packages) khi cách chính thức không áp dụng.

    Trả về: "" nếu đã bật sẵn hoặc bật thành công không cần khởi động lại; "reboot" nếu
    vừa bật xong và CẦN khởi động lại máy trước khi NAT hoạt động được; "error" nếu bật
    thất bại (kể cả trên Home).
    """
    try:
        os_check = _run_ps_script(
            "(Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction SilentlyContinue).Caption",
            timeout=10,
        )
        is_home = "Home" in (os_check.stdout or "")

        if is_home:
            # Trên Home chỉ check Microsoft-Hyper-V
            check = _run_ps_script(
                "(Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V "
                "-ErrorAction SilentlyContinue).State",
                timeout=15,
            )
            if (check.stdout or "").strip() == "Enabled":
                return ""

            logger.warning(
                "[Network] Windows Home phát hiện — Hyper-V chưa bật, đang thử bật qua "
                "DISM package trực tiếp (thủ thuật không chính thức, xem code comment)..."
            )
            status = _enable_hyperv_via_dism_packages()
            if status == "reboot":
                logger.warning("[Network] ✓ Đã bật Hyper-V trên Windows Home — CẦN KHỞI ĐỘNG LẠI MÁY.")
                return "reboot"
            elif status == "ok":
                logger.info("[Network] ✓ Đã bật Hyper-V trên Windows Home thành công.")
                return ""
            else:
                return "error"

        # Với Windows Pro/Enterprise: Kiểm tra cả 3 tính năng bắt buộc
        check_features = _run_ps_script(
            "$features = @('Microsoft-Hyper-V-All', 'Microsoft-Hyper-V', 'Microsoft-Hyper-V-Tools-All'); "
            "$missing = @(); "
            "foreach ($f in $features) { "
            "  $feat = Get-WindowsOptionalFeature -Online -FeatureName $f -ErrorAction SilentlyContinue; "
            "  if (-not $feat -or $feat.State -ne 'Enabled') { $missing += $f } "
            "}; "
            "Write-Output ($missing -join ',')",
            timeout=20,
        )
        missing = (check_features.stdout or "").strip()
        if not missing:
            return ""

        logger.warning(f"[Network] Các tính năng Hyper-V còn thiếu ({missing}) — đang tự động bật bằng DISM...")
        need_reboot = False
        for f in missing.split(","):
            f = f.strip()
            if not f:
                continue
            result = _run_ps_script(
                f"$r = Enable-WindowsOptionalFeature -Online -FeatureName {f} "
                "-All -NoRestart -ErrorAction Stop; "
                f"if ($r.RestartNeeded) {{ Write-Output 'REBOOT_NEEDED' }} else {{ Write-Output 'OK' }}",
                timeout=300,
            )
            output = (result.stdout or "").strip()
            if "REBOOT_NEEDED" in output:
                need_reboot = True
            elif "OK" not in output:
                err = (result.stderr or output or "unknown").strip()
                logger.error(f"[Network] Bật tính năng {f} thất bại: {err}")
                return "error"

        if need_reboot:
            logger.warning(
                "[Network] ✓ Đã bật các tính năng Hyper-V — CẦN KHỞI ĐỘNG LẠI MÁY trước khi NAT hoạt động được."
            )
            return "reboot"
        else:
            logger.info("[Network] ✓ Đã bật các tính năng Hyper-V thành công.")
            return ""
    except Exception as e:
        logger.error(f"[Network] Exception khi bật Hyper-V: {e}")
        return "error"


def _ensure_nat_prereqs():
    """Tự đảm bảo dịch vụ cần thiết cho NAT đang chạy trước khi tạo NAT rule.

    QUAN TRỌNG: đã xác nhận trên máy thật — class WMI MSFT_NetNat (nền tảng của
    New-NetNat/Get-NetNat) KHÔNG tồn tại khi service "WinNat" (Windows NAT Driver
    Service — driver NAT thật sự của Windows) chưa chạy. Đây mới là nguyên nhân gốc
    của lỗi "Invalid class", KHÔNG PHẢI BFE/mpssvc như chẩn đoán ban đầu (2 service đó
    còn bị Windows khoá cứng, Admin thường không restart được, nên hướng sửa cũ không
    bao giờ có tác dụng). WinNat là service bình thường, Admin bật/tắt được.
    Đồng thời tự tắt Internet Connection Sharing (ICS) — dùng cơ chế NAT riêng.
    Chạy trong tiến trình PowerShell riêng, tách khỏi bước tạo NAT.
    """
    _run_ps_script("""
    $ErrorActionPreference = 'SilentlyContinue'
    $winnat = Get-Service -Name "WinNat" -ErrorAction SilentlyContinue
    if ($winnat -and $winnat.Status -ne 'Running') {
        Set-Service -Name "WinNat" -StartupType Automatic -ErrorAction SilentlyContinue
        Start-Service -Name "WinNat" -ErrorAction SilentlyContinue
    }
    """, timeout=15)


def _restart_nat_services():
    """Restart WinNat (Windows NAT Driver Service — nền tảng thật của class WMI
    MSFT_NetNat/New-NetNat) trong 1 tiến trình PowerShell RIÊNG, tách khỏi bước tạo NAT
    sau đó — đảm bảo lần gọi New-NetNat kế tiếp không dính CIM session cũ còn sót lại.

    KHÔNG dùng BFE/mpssvc như bản trước — đã xác nhận trên máy thật 2 service đó bị
    Windows khoá cứng (Protected Service), Admin không Stop/Restart được kể cả đã
    elevate ("Cannot open BFE service on computer '.'"), nên hướng cũ chưa từng có tác
    dụng thật sự. WinNat là service bình thường, restart được.
    """
    _run_ps_script("""
    $ErrorActionPreference = 'SilentlyContinue'
    Restart-Service -Name WinNat -Force -ErrorAction SilentlyContinue
    """, timeout=15)


def _create_nat_rule(nat_name: str, lan_subnet: str):
    """Thử tạo 1 lần NAT rule trong tiến trình PowerShell mới. Trả về (ok, output).

    Dùng @(...) ép Get-NetNat trả về mảng đã enumerate đầy đủ NGAY LẬP TỨC thay vì
    pipeline "stream" từng object — pipeline stream + Where-Object rồi sửa đổi collection
    đó ngay sau (Remove-NetNat) là nguyên nhân phổ biến gây lỗi ".NET Collection was
    modified; enumeration operation may not execute" khi provider WMI đứng sau
    Get-NetNat chưa kịp trả hết kết quả.
    """
    result = _run_ps_script(f"""
    $ErrorActionPreference = 'SilentlyContinue'
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8

    $all = @(Get-NetNat -ErrorAction SilentlyContinue)
    foreach ($n in $all) {{
        if ($n.InternalIPInterfaceAddressPrefix -eq '{lan_subnet}' -or $n.Name -eq '{nat_name}') {{
            Remove-NetNat -Name $n.Name -Confirm:$false -ErrorAction SilentlyContinue
        }}
    }}

    try {{
        New-NetNat -Name '{nat_name}' -InternalIPInterfaceAddressPrefix '{lan_subnet}' -ErrorAction Stop
        Write-Host "OK: NAT rule created for {lan_subnet}"
    }} catch {{
        Write-Host "ERROR: $($_.Exception.Message)"
    }}
    """, timeout=15)
    output = result.stdout.strip()
    return ("OK:" in output), (output or result.stderr.strip() or f"rc={result.returncode}")


def setup_nat(wan_interface: str, lan_subnet: str = "192.168.10.0/24"):
    """Cấu hình Windows NAT để masquerade traffic từ LAN subnet ra WAN.

    Đây là bước bắt buộc để thiết bị kết nối Aruba có thể ra internet.
    Nếu không có NAT, packet từ 192.168.10.x sẽ bị drop tại router/ISP
    vì source IP private không được route về từ internet.

    Args:
        wan_interface: Tên interface WAN có kết nối internet (ví dụ: 'Ethernet')
        lan_subnet: Subnet LAN cần NAT (mặc định '192.168.10.0/24')
    """
    if platform.system() != "Windows":
        logger.info("[Network] Non-Windows, skipping NAT setup.")
        return

    NAT_NAME = "GenRouterNAT"
    logger.info(f"[Network] Setting up Windows NAT: {lan_subnet} → {wan_interface}...")

    try:
        from app.error_reporter import report_error

        # Bước 0: đảm bảo Hyper-V đã bật — NGUYÊN NHÂN GỐC THẬT SỰ của lỗi "Invalid
        # class" (đã xác nhận trên máy thật), không phải BFE/mpssvc/WinNat như các
        # chẩn đoán trước. Nếu vừa bật xong cần reboot, hoặc máy là Windows Home (không
        # hỗ trợ Hyper-V), NAT chắc chắn chưa hoạt động được trong phiên này — báo rõ
        # và bỏ qua các bước thử tạo NAT bên dưới thay vì retry vô ích.
        hyperv_status = _ensure_hyperv_enabled()
        if hyperv_status == "reboot":
            msg = "Vừa tự động bật Hyper-V — CẦN KHỞI ĐỘNG LẠI MÁY rồi chạy lại c69-router.exe để NAT hoạt động."
            logger.error(f"[Network] {msg}")
            report_error("NAT", msg, level="critical", context={"reason": "hyperv_just_enabled_needs_reboot"})
            return
        elif hyperv_status == "error":
            logger.warning("[Network] Không tự bật được Hyper-V — vẫn tiếp tục thử tạo NAT (có thể máy đã có sẵn nền tảng cần thiết qua đường khác).")

        # Bước 1: đảm bảo WinNat chạy + tắt ICS (tiến trình PowerShell riêng).
        _ensure_nat_prereqs()

        # Bước 2: thử tạo NAT (tiến trình PowerShell mới, tách khỏi bước 1).
        ok, output = _create_nat_rule(NAT_NAME, lan_subnet)

        # Bước 3: nếu vẫn lỗi — restart WinNat (tiến trình riêng) rồi CHỜ Ở PHÍA PYTHON
        # (không phải Start-Sleep trong cùng 1 script) trước khi thử lại bằng 1 tiến
        # trình PowerShell HOÀN TOÀN MỚI, để không dính CIM session cũ từ trước lúc
        # restart. Thử tối đa 3 lần tổng cộng.
        attempt = 1
        while not ok and attempt < 3:
            attempt += 1
            logger.warning(f"[Network] NAT tạo thất bại (lần {attempt - 1}): {output}. Đang restart WinNat và thử lại...")
            _restart_nat_services()
            time.sleep(5 + attempt * 2)  # 7s rồi 9s
            ok, output = _create_nat_rule(NAT_NAME, lan_subnet)

        if ok:
            logger.info(f"[Network] ✓ NAT rule created: {lan_subnet} ('{NAT_NAME}') sau {attempt} lần thử")
        else:
            logger.error(f"[Network] NAT setup failed sau {attempt} lần thử: {output}")
            report_error(
                "NAT", output, level="critical",
                context={"wan_interface": wan_interface, "lan_subnet": lan_subnet, "attempts": attempt},
            )
    except Exception as e:
        logger.error(f"[Network] NAT setup exception: {e}")
        from app.error_reporter import report_error
        report_error("NAT", str(e), exc=e, context={"wan_interface": wan_interface, "lan_subnet": lan_subnet})


def remove_nat_for_singbox(lan_subnet: str = "192.168.10.0/24"):
    """Xóa Windows NAT rule (GenRouterNAT) để Mihomo/sing-box nhận đúng source IP của phone.

    Khi GenRouterNAT đang chạy, nó masquerade source IP phone (192.168.10.11)
    thành TUN IP (198.18.0.1) TRƯỚC khi packet vào Mihomo. Điều này khiến
    SRC-IP-CIDR rules không match được và bị lọt ra IP gốc.
    """
    if platform.system() != "Windows":
        return
    logger.info("[Network] Removing GenRouterNAT to allow proxy source IP matching...")
    try:
        ps_cmd = (
            "$ErrorActionPreference = 'SilentlyContinue'; "
            "$ConfirmPreference = 'None'; "
            "Get-NetNat | Remove-NetNat; "
            "Write-Host 'REMOVED_OK'"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10
        )
        logger.info(f"[Network] ✓ GenRouterNAT cleanup: {result.stdout.strip()}")
    except Exception as e:
        logger.warning(f"[Network] Failed to remove GenRouterNAT: {e}")


def restore_nat_for_singbox(wan_interface: str, lan_subnet: str = "192.168.10.0/24"):
    """Khôi phục GenRouterNAT sau khi sing-box dừng.

    Khi sing-box stop, direct-traffic devices cần Windows NAT để ra internet.
    """
    if platform.system() != "Windows" or not wan_interface:
        return
    # Tránh conflict với Windows Mobile Hotspot (icssvc)
    # icssvc và WinNAT cùng bind vào NAT stack → chạy song song sẽ phá nát hotspot
    # và làm crash dịch vụ chia sẻ mạng của Windows.
    # Khi hotspot đang bật, Windows tự quản lý NAT cho dải 192.168.137.x qua ICS,
    # KHÔNG ĐƯỢC tạo thêm NetNat rule đè lên.
    from app.config import load_config
    try:
        _cfg = load_config()
        if getattr(_cfg, "wifi_hotspot_enabled", False):
            logger.info("[Network] WiFi hotspot active — skipping restore_nat_for_singbox to protect icssvc.")
            return
    except Exception:
        pass

    logger.info("[Network] Restoring GenRouterNAT after sing-box stop...")
    setup_nat(wan_interface=wan_interface, lan_subnet=lan_subnet)


def verify_nat() -> dict:
    """Kiểm tra xem NAT rule đã được cấu hình chưa."""
    if platform.system() != "Windows":
        return {"ok": True, "rules": []}

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetNat | Select-Object Name, InternalIPInterfaceAddressPrefix, Active | ConvertTo-Json -Compress -ErrorAction SilentlyContinue"],
            capture_output=True, text=True, timeout=5
        )
        import json as _json
        raw = result.stdout.strip()
        if not raw:
            return {"ok": False, "rules": []}
        rules = _json.loads(raw) if raw.startswith("[") else [_json.loads(raw)]
        return {"ok": len(rules) > 0, "rules": rules}
    except Exception as e:
        return {"ok": False, "error": str(e), "rules": []}


# ─── Full Setup ──────────────────────────────────────────

def setup_network(lan_interface: str = "Ethernet 3", wan_interface: str = "",
                  lan_subnet: str = "192.168.10.0/24",
                  lan_gateway_ip: str = "", lan_subnet_mask: str = "255.255.255.0"):
    """Run all network setup steps."""
    logger.info("[Network] Starting network setup...")
    download_binaries()
    # Đảm bảo LAN interface có đúng IP tĩnh — bước này tool desktop luôn tự làm trước
    # khi khởi động c69-router; khi chạy độc lập phải tự làm ở đây để không phụ thuộc tool.
    if lan_gateway_ip:
        ensure_lan_ip_assigned(lan_interface, lan_gateway_ip, lan_subnet_mask)
    # Bật IP Forwarding CHỈ trên LAN interface (không phải toàn bộ adapters)
    enable_ip_forwarding(lan_interface=lan_interface)
    # Tắt forwarding trên WAN để máy chủ không mất internet
    if wan_interface:
        ensure_wan_forwarding_disabled(wan_interface=wan_interface)
    setup_firewall_rules()
    # XÓA sạch mọi NetNat cũ để Mihomo TUN nhận diện đúng IP gốc của từng máy phone (không bị masquerade)
    remove_nat_for_singbox(lan_subnet=lan_subnet)
    logger.info("[Network] Network setup complete.")




# ─── TCP Stack Performance Optimizer ─────────────────────

def optimize_tcp_stack(tun_interface_name: str = "GenRouterTUN") -> dict:
    """Toi uu Windows TCP stack: ICWV=10, RSS, ECN, Chimney, CTCP, Nagle-off."""
    if platform.system() != "Windows":
        logger.info("[PerfOpt] Skipping TCP optimization (non-Windows)")
        return {"skipped": True, "reason": "non-Windows"}

    logger.info("[PerfOpt] Starting Windows TCP stack optimization...")

    ps_script = (
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        "$r = @{}\n"
        "netsh int tcp set supplemental template=custom icwv=10 *>$null\n"
        "$r['icwv_10'] = ($LastExitCode -eq 0)\n"
        "netsh int tcp set global autotuninglevel=normal *>$null\n"
        "$r['autotuning'] = ($LastExitCode -eq 0)\n"
        "netsh int tcp set global rss=enabled *>$null\n"
        "$r['rss'] = ($LastExitCode -eq 0)\n"
        "netsh int tcp set global ecncapability=enabled *>$null\n"
        "$r['ecn'] = ($LastExitCode -eq 0)\n"
        "netsh int tcp set global chimney=enabled *>$null\n"
        "$r['chimney'] = ($LastExitCode -eq 0)\n"
        "netsh int tcp set supplemental template=custom congestionprovider=ctcp *>$null\n"
        "$r['ctcp'] = ($LastExitCode -eq 0)\n"
        "netsh int tcp set global timestamps=disabled *>$null\n"
        "$r['timestamps'] = ($LastExitCode -eq 0)\n"
        "$p = 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters'\n"
        "try { Set-ItemProperty -Path $p -Name TcpAckFrequency -Value 1 -Type DWord -Force; "
        "Set-ItemProperty -Path $p -Name TCPNoDelay -Value 1 -Type DWord -Force; "
        "$r['nagle_global']=$true } catch { $r['nagle_global']=$_.Exception.Message }\n"
        + f"$a = Get-NetAdapter -Name '{tun_interface_name}' -EA SilentlyContinue\n"
        + "if ($a) {"
        + " $rp=\"HKLM:\\\\SYSTEM\\\\CurrentControlSet\\\\Services\\\\Tcpip\\\\Parameters\\\\Interfaces\\\\$($a.InterfaceGuid)\";"
        + " if(Test-Path $rp){"
        + " Set-ItemProperty -Path $rp -Name TcpAckFrequency -Value 1 -Type DWord -Force;"
        + " Set-ItemProperty -Path $rp -Name TCPNoDelay -Value 1 -Type DWord -Force;"
        + " $r['nagle_tun']=$true"
        + "} else {$r['nagle_tun']='iface_not_registered'}}"
        + " else {$r['nagle_tun']='tun_not_started'}\n"
        + "$r | ConvertTo-Json -Compress\n"
    )

    results = {}
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True, text=True, timeout=30
        )
        stdout = res.stdout.strip()
        if stdout:
            try:
                import json as _json
                parsed = _json.loads(stdout)
                for k, v in parsed.items():
                    if v is True or str(v).lower() == "true":
                        results[k] = True
                        logger.info(f"[PerfOpt] OK {k}")
                    else:
                        results[k] = str(v)
                        logger.info(f"[PerfOpt] INFO {k}: {v}")
            except Exception:
                results["raw"] = stdout[:300]
        else:
            results["ps_error"] = (res.stderr or "").strip()[:200]
    except Exception as e:
        results["exception"] = str(e)
    ok = sum(1 for v in results.values() if v is True)
    logger.info(f"[PerfOpt] TCP stack: {ok}/{len(results)} applied")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# WiFi Hotspot (Hosted Network) — netsh wlan hostednetwork
# Dùng cho laptop: card WiFi tích hợp bắt internet (WAN), USB WiFi phát hotspot (LAN).
# Hoặc: chỉ 1 card WiFi duy nhất — bắt WAN + phát hotspot cùng lúc (hiệu suất thấp hơn).
# ─────────────────────────────────────────────────────────────────────────────

def detect_usb_wifi_adapter(exclude_wan_interface: str = "") -> dict:
    """Phát hiện USB WiFi adapter phù hợp nhất để làm LAN hotspot.

    Ưu tiên theo thứ tự:
    1. USB WiFi adapter (BusType=USB, MediaType=NativeWifi) — tốt nhất: tách biệt hoàn toàn
    2. Card WiFi tích hợp KHÁC với WAN interface — nếu laptop có 2 card WiFi
    3. None nếu không tìm thấy

    Returns:
        dict với keys: name, description, mac, is_usb, status
        None nếu không tìm thấy adapter phù hợp.
    """
    if platform.system() != "Windows":
        return None

    try:
        # Lấy tất cả WiFi adapter (NativeWifi / 802.11), bao gồm cả đang Down
        ps_script = r"""
$adapters = Get-NetAdapter | Where-Object {
    $_.PhysicalMediaType -eq 'NativeWifi' -or
    $_.PhysicalMediaType -eq '802.11' -or
    $_.InterfaceDescription -like '*Wi-Fi*' -or
    $_.InterfaceDescription -like '*Wireless*' -or
    $_.InterfaceDescription -like '*802.11*' -or
    $_.InterfaceDescription -like '*WLAN*'
}
foreach ($a in $adapters) {
    $busType = ''
    try {
        $pnp = Get-PnpDevice -InstanceId $a.PnpDeviceID -ErrorAction SilentlyContinue
        if ($pnp) {
            $loc = (Get-PnpDeviceProperty -InstanceId $a.PnpDeviceID -KeyName 'DEVPKEY_Device_LocationInfo' -ErrorAction SilentlyContinue).Data
            if ($loc -like 'USB*' -or $a.PnpDeviceID -like 'USB*') { $busType = 'USB' }
        }
    } catch {}
    Write-Output "$($a.Name)|$($a.InterfaceDescription)|$($a.MacAddress)|$busType|$($a.Status)"
}
"""
        res = _run_ps_script(ps_script, timeout=10)
        lines = [l.strip() for l in res.stdout.strip().splitlines() if '|' in l]

        usb_candidates = []
        other_wifi = []

        for line in lines:
            parts = line.split('|')
            if len(parts) < 5:
                continue
            name, desc, mac, bus_type, status = parts[0], parts[1], parts[2], parts[3], parts[4]

            # Bỏ qua virtual adapters và WAN interface
            if any(x in desc for x in ['Virtual', 'Wintun', 'Hyper-V', 'VMware', 'Loopback', 'Microsoft Wi-Fi Direct']):
                continue
            if exclude_wan_interface and name == exclude_wan_interface:
                continue

            info = {"name": name, "description": desc, "mac": mac, "is_usb": bus_type == "USB", "status": status}

            if bus_type == "USB":
                usb_candidates.append(info)
            else:
                other_wifi.append(info)

        # Ưu tiên USB WiFi, sau đó WiFi tích hợp khác WAN
        if usb_candidates:
            chosen = usb_candidates[0]
            logger.info(f"[Hotspot] Detected USB WiFi adapter: {chosen['name']} ({chosen['description']})")
            return chosen
        if other_wifi:
            chosen = other_wifi[0]
            logger.info(f"[Hotspot] No USB WiFi found. Using built-in WiFi adapter: {chosen['name']} ({chosen['description']})")
            return chosen

        logger.info("[Hotspot] No suitable WiFi adapter found for hotspot.")
        return None

    except Exception as e:
        logger.warning(f"[Hotspot] detect_usb_wifi_adapter error: {e}")
        return None


def setup_hosted_network(ssid: str = "C69-Router", password: str = "matkhau123", wan_interface: str = "") -> bool:
    """Tao WiFi hotspot - tu dong chon phuong an tot nhat.

    Thu theo thu tu:
    1. netsh wlan hostednetwork (neu driver ho tro)
    2. Windows Mobile Hotspot WinRT (fallback cho Intel AX201/AX210, Win11)

    wan_interface: ten adapter WAN de uu tien profile WiFi thay vi Ethernet.
    Returns: True neu thanh cong.
    """
    if platform.system() != "Windows":
        logger.warning("[Hotspot] setup_hosted_network chi ho tro Windows.")
        return False

    if len(password) < 8:
        password = (password + "12345678")[:8]

    hw = check_hosted_network_supported()
    method = hw.get("method")

    if method == "netsh":
        logger.info(f"[Hotspot] Method: netsh hostednetwork. SSID='{ssid}'")
        # Fall through to existing netsh logic below
    elif method == "winrt":
        logger.info(f"[Hotspot] Method: Windows Mobile Hotspot WinRT. SSID='{ssid}'")
        return setup_mobile_hotspot_ps(ssid, password, wan_interface=wan_interface)
    else:
        logger.warning(f"[Hotspot] No supported method: {hw.get('reason')}")
        return False

    logger.info(f"[Hotspot] Setting up Hosted Network: SSID='{ssid}' ...")

    try:
        # Bước 1: Cấu hình SSID + password
        r1 = subprocess.run(
            ["netsh", "wlan", "set", "hostednetwork",
             "mode=allow", f"ssid={ssid}", f"key={password}"],
            capture_output=True, text=True, timeout=10
        )
        if r1.returncode != 0 and "error" in r1.stdout.lower():
            logger.error(f"[Hotspot] netsh set hostednetwork failed: {r1.stdout.strip()}")
            return False
        logger.info(f"[Hotspot] Hosted network configured: {r1.stdout.strip()}")

        # Bước 2: Khởi động hosted network
        r2 = subprocess.run(
            ["netsh", "wlan", "start", "hostednetwork"],
            capture_output=True, text=True, timeout=10
        )
        output = r2.stdout.strip()
        logger.info(f"[Hotspot] netsh start hostednetwork: {output}")

        if "started" in output.lower() or "đã bắt đầu" in output.lower():
            logger.info("[Hotspot] ✓ WiFi hotspot started successfully.")
            return True
        elif "cannot" in output.lower() or "không thể" in output.lower() or "not supported" in output.lower():
            logger.warning(
                f"[Hotspot] ⚠ Hosted network không hỗ trợ trên adapter này. "
                f"Thử: netsh wlan show drivers | grep 'Hosted network supported'. "
                f"Output: {output}"
            )
            return False
        else:
            # Có thể đã chạy rồi hoặc trạng thái không rõ — check lại
            r3 = subprocess.run(
                ["netsh", "wlan", "show", "hostednetwork"],
                capture_output=True, text=True, timeout=5
            )
            if "started" in r3.stdout.lower() or "đã bắt đầu" in r3.stdout.lower():
                logger.info("[Hotspot] ✓ WiFi hotspot already running.")
                return True
            logger.warning(f"[Hotspot] Hotspot status unclear: {output}")
            return False

    except subprocess.TimeoutExpired:
        logger.error("[Hotspot] netsh command timed out.")
        return False
    except Exception as e:
        logger.error(f"[Hotspot] setup_hosted_network error: {e}")
        return False


def resolve_hotspot_topology(
    configured_lan_interface: str,
    hotspot_adapter: str | None,
    hotspot_active: bool,
) -> dict:
    """Return a safe LAN topology for the one-Wi-Fi-card Windows hotspot path.

    ICS must own the active Wi-Fi Direct adapter. Falling back to a stale Ethernet
    adapter after hotspot startup fails makes the router claim it is ready while no
    phone can reliably enter the proxy path.
    """
    if hotspot_active and hotspot_adapter:
        return {"ready": True, "lan_interface": hotspot_adapter, "reason": ""}
    return {
        "ready": False,
        "lan_interface": None,
        "reason": "Windows Mobile Hotspot is not active; ICS adapter is unavailable.",
    }


def get_hosted_network_adapter() -> str:
    """Lấy đúng virtual adapter đang phục vụ Windows Mobile Hotspot/Hosted Network.

    Windows có thể tồn tại nhiều Wi-Fi Direct adapter. Chỉ chọn adapter đang Up và có
    IPv4 thuộc subnet ICS 192.168.137.0/24 để tránh gán LAN interface nhầm adapter cũ.
    """
    if platform.system() != "Windows":
        return None

    try:
        ps_script = (
            "Get-NetAdapter | Where-Object { "
            "$_.Status -eq 'Up' -and ("
            "$_.InterfaceDescription -like '*Hosted Network Virtual*' -or "
            "$_.InterfaceDescription -like '*Wi-Fi Direct Virtual*'"
            ") } | ForEach-Object { "
            "$ips = @(Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
            "Where-Object { $_.IPAddress -like '192.168.137.*' } | Select-Object -ExpandProperty IPAddress); "
            "if ($ips.Count -gt 0) { $_.Name } "
            "} | Select-Object -First 1"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=8
        )
        name = result.stdout.strip()
        if name:
            logger.info(f"[Hotspot] Found active ICS hotspot adapter: '{name}'")
            return name

        logger.warning("[Hotspot] No active ICS hotspot virtual adapter found.")
        return None
    except Exception as e:
        logger.warning(f"[Hotspot] get_hosted_network_adapter error: {e}")
        return None


def is_tethering_active() -> bool:
    """Kiem tra xem Windows Mobile Hotspot co dang o trang thai ON (State=1) khong."""
    if platform.system() != "Windows":
        return False
    try:
        cmd = [
            "powershell", "-NoProfile", "-Command",
            "[void][Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]; "
            "[void][Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]; "
            "$active = $false; "
            "foreach ($p in [Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles()) { "
            "  if ($p.GetNetworkConnectivityLevel() -ge 3) { "
            "    try { "
            "      $m = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($p); "
            "      if ([int]$m.TetheringOperationalState -eq 1) { $active = $true; break } "
            "    } catch {} "
            "  } "
            "}; Write-Host $active"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        return "True" in res.stdout
    except Exception:
        return False


def get_active_tethering_ssid() -> str:
    """Lay SSID hien tai cua Windows Mobile Hotspot."""
    if platform.system() != "Windows":
        return ""
    try:
        cmd = [
            "powershell", "-NoProfile", "-Command",
            "[void][Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]; "
            "[void][Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]; "
            "foreach ($p in [Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles()) { "
            "  if ($p.GetNetworkConnectivityLevel() -ge 3) { "
            "    try { "
            "      $m = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($p); "
            "      $ssid = ($m.GetCurrentAccessPointConfiguration()).Ssid; "
            "      if ($ssid) { Write-Host $ssid; break } "
            "    } catch {} "
            "  } "
            "}"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        return res.stdout.strip()
    except Exception:
        return ""


def stop_hosted_network():
    """Dừng Windows Hosted Network."""
    if platform.system() != "Windows":
        return
    try:
        r = subprocess.run(
            ["netsh", "wlan", "stop", "hostednetwork"],
            capture_output=True, text=True, timeout=10
        )
        logger.info(f"[Hotspot] stop hostednetwork: {r.stdout.strip()}")
    except Exception as e:
        logger.warning(f"[Hotspot] stop_hosted_network error: {e}")


def check_hosted_network_supported() -> dict:
    """Kiem tra WiFi card co ho tro hotspot va phuong an nao dung.

    Thu 2 phuong an:
    1. netsh wlan hostednetwork (driver-level, laptop cu / USB dongle)
    2. Windows Mobile Hotspot WinRT (Intel AX201/AX210, Win10/11 modern)

    Returns: dict {"supported": bool, "method": "netsh"|"winrt"|None, "reason": str}
    """
    if platform.system() != "Windows":
        return {"supported": False, "method": None, "reason": "Windows only"}

    # Phuong an 1: netsh hostednetwork
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "drivers"],
            capture_output=True, text=True, timeout=8
        )
        out = result.stdout.lower()
        if "hosted network supported  : yes" in out or "hosted network supported: yes" in out:
            return {"supported": True, "method": "netsh", "reason": "Driver supports netsh hostednetwork"}
    except Exception:
        pass

    # Phuong an 2: Windows Mobile Hotspot (WinRT / icssvc)
    if _check_icssvc_available():
        return {
            "supported": True,
            "method": "winrt",
            "reason": (
                "Windows Mobile Hotspot (WinRT) available. "
                "netsh hostednetwork not supported by this WiFi driver (common on Intel AX201/AX210)."
            )
        }

    return {
        "supported": False,
        "method": None,
        "reason": (
            "Driver does NOT support netsh hostednetwork, "
            "and Windows Mobile Hotspot service (icssvc) not found. "
            "Try: update WiFi driver, or enable Mobile Hotspot in Windows Settings first."
        )
    }


# ── Windows Mobile Hotspot fallback (Intel AX201/AX210, Win11) ────────────────
def setup_mobile_hotspot_ps(ssid="C69-Router", password="matkhau123", wan_interface=""):
    """Bat Windows Mobile Hotspot qua PowerShell WinRT API.

    Ho tro: Windows 10 1607+ / Windows 11.
    Mot card WiFi co the vua ket noi internet (WAN) vua phat hotspot (AP mode).
    Windows tu tao 'Microsoft Wi-Fi Direct Virtual Adapter' khi bat.

    wan_interface: ten adapter WAN (vi du 'Wi-Fi'). Neu la WiFi, se uu tien
    profile WiFi thay vi Ethernet de tranh dual-tethering conflict.

    Returns: True neu thanh cong.
    """
    if platform.system() != "Windows":
        return False

    # Determine if WAN is WiFi to filter out Ethernet profiles
    _wan_is_wifi = wan_interface.lower().startswith("wi-fi") or wan_interface.lower() == "wifi"
    _wan_iface_ps = wan_interface.replace("\\", "\\\\").replace("'", "''")  # escape for PS

    # Ensure SharedAccess Parameters registry keys are set so Windows ICS DHCP server (UDP 67) starts
    try:
        reg_ps = (
            "$p = 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\SharedAccess\\Parameters'; "
            "Set-ItemProperty $p -Name 'ScopeAddress' -Value '192.168.137.1' -Type String -Force -EA SilentlyContinue; "
            "Set-ItemProperty $p -Name 'StandaloneDhcpAddress' -Value '192.168.137.1' -Type String -Force -EA SilentlyContinue; "
            "Set-ItemProperty $p -Name 'EnableDHCP' -Value 1 -Type DWord -Force -EA SilentlyContinue; "
            "Set-ItemProperty $p -Name 'DhcpDomain' -Value 'mshome.net' -Type String -Force -EA SilentlyContinue"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", reg_ps], capture_output=True, timeout=5)
    except Exception:
        pass
    logger.info(f"[Hotspot] Starting Windows Mobile Hotspot WinRT: SSID='{ssid}' wan={wan_interface}")

    # PS script WinRT hotspot - PS5.1 compatible
    # IAsyncOperation.Status polling: 0=Running, 1=Completed, 2=Canceled, 3=Error
    # ErrorCode la Exception object - dung .Message va .HResult
    # TetheringCapability: 0=OK, 1=GroupPolicy, 2=HardwareLimit, 3=Operator, 4=Sku, 5=Requirements, 6=System
    ps_content = f"""
# Load WinRT namespace types (moi type tren 1 dong - PS5.1)
try {{
    [void][Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
    [void][Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
    [void][Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]
}} catch {{
    Write-Output "ERROR:TypeLoad:$($_.Exception.Message)"
    exit 1
}}

# Await IAsyncOperation bang polling Status
function Await-WinRT($op, $timeoutSec = 30) {{
    $deadline = [System.DateTime]::Now.AddSeconds($timeoutSec)
    while ($op.Status -eq 0 -and [System.DateTime]::Now -lt $deadline) {{
        [System.Threading.Thread]::Sleep(100)
    }}
    if ($op.Status -eq 1) {{
        return $op.GetResults()
    }} elseif ($op.Status -eq 0) {{
        throw "Timeout after $timeoutSec seconds"
    }} elseif ($op.Status -eq 2) {{
        throw "Operation was canceled"
    }} else {{
        # Status 3 = Error: ErrorCode la Exception object, dung .Message va .HResult
        $ec = $op.ErrorCode
        $hresult = if ($null -ne $ec) {{ "0x{{0:X8}}" -f $ec.HResult }} else {{ "unknown" }}
        $ecMsg   = if ($null -ne $ec) {{ $ec.Message }} else {{ "no error code" }}
        throw "WinRT Error (Status=3): HResult=$hresult, Msg=$ecMsg"
    }}
}}

try {{
    # STEP 0: Neu wan_interface la WiFi, uu tien profile WiFi thay vi Ethernet.
    # GetInternetConnectionProfile() co the tra ve Ethernet neu ca hai deu co internet.
    $wanIsWifi = $false; $wanIface = '{_wan_iface_ps}'
    if ($wanIface -match 'Wi-Fi|WiFi|wifi') {{ $wanIsWifi = $true }}
    $profile = $null
    if ($wanIsWifi) {{
        # Tim profile WiFi (khong phai Ethernet) voi connectivity >= InternetAccess (3)
        foreach ($p in [Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles()) {{
            $lvl = [int]$p.GetNetworkConnectivityLevel()
            $pName = $p.ProfileName
            if ($lvl -ge 3 -and $pName -notmatch 'Ethernet') {{
                $profile = $p
                Write-Output "DIAG:SelectedWiFiProfile=$pName"
                break
            }}
        }}
    }}
    if ($null -eq $profile) {{
        $profile = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
    }}
    if ($null -eq $profile) {{ Write-Output "ERROR:NoInternetProfile"; exit 1 }}

    Write-Output "DIAG:Profile=$($profile.ProfileName)"

    # STEP 1: Dung tethering tren TAT CA profile khac truoc khi bat tren target profile.
    # Tranh dual-tethering conflict (Ethernet + WiFi deu State=1 -> InTransition).
    $targetName = $profile.ProfileName
    foreach ($p2 in [Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles()) {{
        if ([int]$p2.GetNetworkConnectivityLevel() -lt 3) {{ continue }}
        if ($p2.ProfileName -eq $targetName) {{ continue }}
        try {{
            $m2 = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($p2)
            $st2 = [int]$m2.TetheringOperationalState
            if ($st2 -eq 1) {{
                Write-Output "DIAG:StopConflict=$($p2.ProfileName) State=$st2"
                try {{ Await-WinRT($m2.StopTetheringAsync()) | Out-Null; Write-Output "DIAG:StopConflict OK" }} catch {{}}
            }}
        }} catch {{}}
    }}
    Start-Sleep -Milliseconds 1000

    $mgr = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)
    if ($null -eq $mgr) {{ Write-Output "ERROR:NoTetheringManager"; exit 1 }}

    # Pre-check TetheringCapability truoc khi bat
    # 0=Enabled, 1=DisabledByGroupPolicy, 2=DisabledByHardwareLimitation
    # 3=DisabledByOperator, 4=DisabledBySku, 5=DisabledByRequirementsNotMet, 6=DisabledBySystemCapability
    $capInt = [int]$mgr.TetheringCapability
    Write-Output "DIAG:TetheringCapability=$capInt"
    if ($capInt -ne 0) {{
        $capNames = @{{0="Enabled";1="DisabledByGroupPolicy";2="DisabledByHardwareLimitation";3="DisabledByOperator";4="DisabledBySku";5="DisabledByRequirementsNotMet";6="DisabledBySystemCapability"}}
        $capName = if ($capNames.ContainsKey($capInt)) {{ $capNames[$capInt] }} else {{ "Unknown($capInt)" }}
        Write-Output "ERROR:CapabilityBlocked:$capName"
        exit 1
    }}

    # Trang thai hien tai: 0=Off, 1=On, 2=InTransition
    $state = [int]$mgr.TetheringOperationalState
    Write-Output "DIAG:OperationalState=$state"
    if ($state -eq 1) {{
        # Kiem tra SSID hien tai dung chua - neu sai thi stop va reconfigure
        $currentSsid = ($mgr.GetCurrentAccessPointConfiguration()).Ssid
        if ($currentSsid -eq '{ssid}') {{ Write-Output "ALREADY_ACTIVE"; exit 0 }}
        Write-Output "DIAG:SSID mismatch ($currentSsid != {ssid}) - stopping old tethering to reconfigure"
        try {{ Await-WinRT ($mgr.StopTetheringAsync()) | Out-Null }} catch {{}}
        Start-Sleep -Milliseconds 1000
        $state = [int]$mgr.TetheringOperationalState
    }}

    # Xu ly InTransition stuck (Windows bug: icssvc bi treo -> tat ca WinRT op fail Status=3)
    # Fix: stop/start icssvc service, doi 4s cho service san sang, lay lai manager tu profile
    if ($state -eq 2) {{
        Write-Output "DIAG:InTransition detected - waiting 4s..."
        $waitDeadline = [System.DateTime]::Now.AddSeconds(4)
        while ([int]$mgr.TetheringOperationalState -eq 2 -and [System.DateTime]::Now -lt $waitDeadline) {{
            [System.Threading.Thread]::Sleep(500)
        }}
        $state = [int]$mgr.TetheringOperationalState
        if ($state -eq 2) {{
            Write-Output "DIAG:Still InTransition - restarting icssvc..."
            Stop-Service -Name icssvc -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
            Start-Service -Name icssvc -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 3
            $mgr = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)
            $state = [int]$mgr.TetheringOperationalState
            Write-Output "DIAG:State after icssvc restart: $state"
        }}
        if ($state -eq 1) {{ Write-Output "ALREADY_ACTIVE"; exit 0 }}
    }}

    # Always ensure fresh manager handle before configuring
    $mgr = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)
    $cfg = $mgr.GetCurrentAccessPointConfiguration()
    $cfg.Ssid = '{ssid}'
    $cfg.Passphrase = '{password}'
    Write-Output "DIAG:ConfiguringAP SSID={ssid}"
    try {{ Await-WinRT ($mgr.ConfigureAccessPointAsync($cfg)) | Out-Null }} catch {{ Write-Output "DIAG:ConfigureAP warning: $_" }}

    # Bat hotspot
    Write-Output "DIAG:StartingTethering"
    $r = Await-WinRT ($mgr.StartTetheringAsync())
    # TetheringOperationStatus: 0=Success, khac=loi
    $rStatus = [int]$r.Status
    if ($rStatus -eq 0) {{
        Write-Output "SUCCESS"
    }} else {{
        # TetheringOperationStatus: 1=Unknown, 2=NetworkLimitedConnectivity, 3=KilledForPerformance,
        # 4=RoamingNotAllowed, 5=OperationInProgress, 6=BluetoothDeviceOff, 7=WifiDeviceOff, 8=EntitlementCheckTimeout, 9=EntitlementCheckInternalError
        $statusNames = @{{0="Success";1="Unknown";2="NetworkLimitedConnectivity";3="KilledForPerformance";4="RoamingNotAllowed";5="OperationInProgress";6="BluetoothOff";7="WifiDeviceOff";8="EntitlementTimeout";9="EntitlementError"}}
        $statusName = if ($statusNames.ContainsKey($rStatus)) {{ $statusNames[$rStatus] }} else {{ "Unknown($rStatus)" }}
        Write-Output "ERROR:TetheringFailed:$statusName"
    }}
}} catch {{
    $errMsg = if ($_.Exception.InnerException) {{ $_.Exception.InnerException.Message }} else {{ $_.Exception.Message }}
    Write-Output "EXCEPTION:$errMsg"
}}
"""
    import tempfile
    import os as _os
    fd, ps_path = tempfile.mkstemp(suffix=".ps1", prefix="c69_hotspot_")
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(ps_content)

        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_path],
            capture_output=True, text=True, timeout=45
        )
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()

        if out:
            logger.info(f"[Hotspot] WinRT PS stdout: {out}")
        if err:
            logger.warning(f"[Hotspot] WinRT PS stderr: {err[:300]}")

        if "SUCCESS" in out or "ALREADY_ACTIVE" in out:
            logger.info("[Hotspot] Windows Mobile Hotspot started OK.")
            return True

        if "ERROR:NoInternetProfile" in out:
            logger.warning("[Hotspot] No internet connection profile - connect WiFi/Ethernet first.")
        elif "0x80070032" in out or "not supported" in out.lower():
            logger.warning(
                "[Hotspot] ✗ HR=0x80070032 'The request is not supported'.\n"
                "          → Common cause: Hyper-V installed and blocking Mobile Hotspot.\n"
                "          → Fix options:\n"
                "            1. Tắt Hyper-V: 'bcdedit /set hypervisorlaunchtype off' → reboot\n"
                "            2. Cắm thêm USB WiFi adapter (card riêng, Hyper-V không block)\n"
                "            3. Bật Mobile Hotspot thủ công từ Windows Settings trước khi chạy app"
            )
        elif "EXCEPTION" in out:
            logger.warning(f"[Hotspot] WinRT exception: {out}")
        elif "CapabilityBlocked" in out:
            reason = out.split("ERROR:CapabilityBlocked:")[-1].strip().split("\n")[0] if "CapabilityBlocked:" in out else out
            logger.warning(f"[Hotspot] Tethering blocked by system: {reason}")
        else:
            logger.warning(f"[Hotspot] Mobile Hotspot failed: {out or err}")
        return False

    except Exception as e:
        logger.warning(f"[Hotspot] setup_mobile_hotspot_ps error: {e}")
        return False
    finally:
        try:
            _os.remove(ps_path)
        except Exception:
            pass



def _check_icssvc_available():
    """Kiem tra Windows Mobile Hotspot service (icssvc) co kha dung khong."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Service icssvc -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Status"],
            capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip().lower() in ("running", "stopped")
    except Exception:
        return False

