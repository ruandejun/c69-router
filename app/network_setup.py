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

    # Remove Python blocking rules
    $pythonPath = "{python_exe}"
    if (Test-Path $pythonPath) {{
        $blockRules = Get-NetFirewallRule -Action Block -ErrorAction SilentlyContinue
        foreach ($rule in $blockRules) {{
            $app = Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $rule -ErrorAction SilentlyContinue
            if ($app -and ($app.Program -like "*python.exe" -or $rule.DisplayName -eq "python.exe")) {{
                Remove-NetFirewallRule -Name $rule.Name -ErrorAction SilentlyContinue
            }}
        }}
    }}

    # API Ports 8000-9099
    if (-not (Get-NetFirewallRule -DisplayName 'GenRouter API Ports' -ErrorAction SilentlyContinue)) {{
        New-NetFirewallRule -DisplayName 'GenRouter API Ports' -Direction Inbound -Protocol TCP -LocalPort 8000-9099 -Action Allow -Enabled True -Profile Any
    }}

    # DHCP UDP 67/68
    if (-not (Get-NetFirewallRule -DisplayName 'GenRouter DHCP 67' -ErrorAction SilentlyContinue)) {{
        New-NetFirewallRule -DisplayName 'GenRouter DHCP 67' -Direction Inbound -Protocol UDP -LocalPort 67 -Action Allow -Enabled True -Profile Any
    }}
    if (-not (Get-NetFirewallRule -DisplayName 'GenRouter DHCP 68' -ErrorAction SilentlyContinue)) {{
        New-NetFirewallRule -DisplayName 'GenRouter DHCP 68' -Direction Inbound -Protocol UDP -LocalPort 68 -Action Allow -Enabled True -Profile Any
    }}

    # DNS UDP 53 (for DHCP clients)
    if (-not (Get-NetFirewallRule -DisplayName 'GenRouter DNS 53' -ErrorAction SilentlyContinue)) {{
        New-NetFirewallRule -DisplayName 'GenRouter DNS 53' -Direction Inbound -Protocol UDP -LocalPort 53 -Action Allow -Enabled True -Profile Any
    }}

    # Sing-Box program
    if (Test-Path '{singbox_exe}') {{
        if (-not (Get-NetFirewallRule -DisplayName 'GenRouter Sing-Box' -ErrorAction SilentlyContinue)) {{
            New-NetFirewallRule -DisplayName 'GenRouter Sing-Box' -Direction Inbound -Program '{singbox_exe}' -Action Allow -Enabled True -Profile Any
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

def detect_wan_interface() -> str:
    """Auto-detect WAN interface (interface with default gateway, lowest metric)."""
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
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetRoute -DestinationPrefix '0.0.0.0/0' | "
             "Where-Object {$_.NextHop -ne '0.0.0.0'} | "
             "Sort-Object @{Expression={$_.RouteMetric + $_.InterfaceMetric}} | "
             "Select-Object -First 1 -ExpandProperty InterfaceAlias"],
            capture_output=True, text=True, timeout=5
        )
        iface = result.stdout.strip()
        if iface:
            logger.info(f"[Network] Auto-detected WAN interface: {iface}")
            return iface
    except Exception as e:
        logger.warning(f"[Network] Failed to auto-detect WAN: {e}")

    return "Wi-Fi"


def detect_lan_interface(exclude_interface: str = "") -> str:
    """Auto-detect card mạng CÓ DÂY (Ethernet vật lý) đang cắm để dùng làm LAN bridge
    tới Aruba AP — loại trừ card đang dùng làm WAN (internet) và các card ảo (Wintun,
    Hyper-V, VMware, Bluetooth, Loopback...). Không chọn Wi-Fi vì LAN bridge cho phone
    phải là card có dây riêng, khác hẳn card ra internet.

    Dùng khi người dùng không tự chọn LAN interface qua UI, hoặc khi config.lan_interface
    lưu sẵn không còn khớp với máy hiện tại (đổi máy, đổi tên card...).
    """
    if platform.system() != "Windows":
        return "eth1"

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetAdapter | Where-Object { "
             "$_.Status -eq 'Up' -and "
             "$_.PhysicalMediaType -eq '802.3' -and "
             f"$_.Name -ne '{exclude_interface}' -and "
             "$_.InterfaceDescription -notlike '*Wintun*' -and "
             "$_.InterfaceDescription -notlike '*Virtual*' -and "
             "$_.InterfaceDescription -notlike '*Loopback*' -and "
             "$_.InterfaceDescription -notlike '*Hyper-V*' -and "
             "$_.InterfaceDescription -notlike '*VMware*' -and "
             "$_.InterfaceDescription -notlike '*Bluetooth*' "
             "} | Sort-Object ifIndex | Select-Object -First 1 -ExpandProperty Name"],
            capture_output=True, text=True, timeout=5
        )
        iface = result.stdout.strip()
        if iface:
            logger.info(f"[Network] Auto-detected LAN interface (wired, non-WAN): {iface}")
            return iface
    except Exception as e:
        logger.warning(f"[Network] Failed to auto-detect LAN interface: {e}")

    return "Ethernet 3"


def is_lan_interface_valid(interface_name: str, wan_interface: str = "") -> bool:
    """Kiểm tra LAN interface đang lưu trong config có còn tồn tại, đang UP, và
    không trùng với WAN interface không — đề phòng config.json lỗi thời (vd copy
    sang máy khác, đổi tên card) khiến LAN bridge trỏ vào card không tồn tại hoặc
    card đang dùng làm WAN.
    """
    if platform.system() != "Windows" or not interface_name:
        return False

    if wan_interface and interface_name == wan_interface:
        return False

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetAdapter -Name '{interface_name}' -ErrorAction SilentlyContinue).Status"],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() == "Up"
    except Exception as e:
        logger.warning(f"[Network] Failed to verify LAN interface '{interface_name}': {e}")
        return True  # Không xác minh được thì tin theo config, tránh false positive


def is_wan_interface_valid(interface_name: str) -> bool:
    """Kiểm tra interface có đang thực sự là đường ra internet không (có default route
    với NextHop thật, không phải APIPA/disconnected).

    Dùng để phát hiện config.wan_interface bị lỗi thời (vd đổi máy, đổi tên card,
    card WiFi đang mất kết nối) — vì trước đây code chỉ tin theo giá trị lưu trong
    data/config.json (mặc định cứng 'Wi-Fi') mà không xác minh, dẫn đến NAT/sing-box
    trỏ ra card chết trong khi card khác mới thực sự có internet.
    """
    if platform.system() != "Windows" or not interface_name:
        return True  # Không xác minh được thì tin theo config, tránh false positive

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-NetRoute -InterfaceAlias '{interface_name}' -DestinationPrefix '0.0.0.0/0' "
             f"-ErrorAction SilentlyContinue | Where-Object {{ $_.NextHop -ne '0.0.0.0' }} | "
             f"Select-Object -First 1 -ExpandProperty NextHop"],
            capture_output=True, text=True, timeout=5
        )
        return bool(result.stdout.strip())
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


def setup_interface_dns(
    lan_interface: str,
    tun_interface: str,
    primary_dns: str = "1.1.1.1",
    secondary_dns: str = "1.0.0.1",
) -> bool:
    """Cai DNS cho chinh Windows adapter (LAN + TUN) cua may host, KHONG lien quan DNS DHCP phat cho phone.

    Phone nhan DNS = lan_gateway_ip tu DHCP (xem main.py), roi duoc hijack-dns trong
    TUN chuyen tiep. 1.1.1.1/1.0.0.1 o day chi la DNS cho ban than may host, va cung
    la 2 IP nam trong route_exclude_address (singbox_manager.py) de sing-box tu resolve
    upstream ma khong bi loop lai vao chinh TUN cua no.

    Args:
        lan_interface: Ten LAN adapter (vi du: "Ethernet 3")
        tun_interface: Ten TUN adapter (vi du: "genrouter-tun-9")
        primary_dns: Primary DNS (default 1.1.1.1 Cloudflare)
        secondary_dns: Secondary DNS (default 1.0.0.1 Cloudflare secondary)
    """
    if platform.system() != "Windows":
        return False

    success = False

    def _set_dns_on(iface: str) -> bool:
        try:
            r1 = subprocess.run(
                ["netsh", "interface", "ipv4", "set", "dns",
                 f"name={iface}", "static", primary_dns, "primary"],
                capture_output=True, text=True, timeout=10
            )
            subprocess.run(
                ["netsh", "interface", "ipv4", "add", "dns",
                 f"name={iface}", secondary_dns, "index=2"],
                capture_output=True, text=True, timeout=10
            )
            ok = (r1.returncode == 0)
            if ok:
                logger.info(f"[DNS] {iface}: {primary_dns} / {secondary_dns} set OK")
            else:
                logger.warning(f"[DNS] {iface} set failed: {r1.stderr.strip()[:80]}")
            return ok
        except Exception as e:
            logger.warning(f"[DNS] Error setting DNS on {iface}: {e}")
            return False

    success |= _set_dns_on(lan_interface)
    success |= _set_dns_on(tun_interface)
    return success


def download_binaries():
    """Download sing-box.exe and wintun.dll if missing (Windows only)."""
    if platform.system() != "Windows":
        return

    singbox_exe = os.path.join(PROJECT_DIR, "sing-box.exe")
    wintun_dll = os.path.join(PROJECT_DIR, "wintun.dll")

    import ssl
    context = ssl._create_unverified_context()

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
    """Set LAN interface metric to 5 to force limited broadcasts out of it on Windows."""
    if platform.system() != "Windows":
        return
    logger.info(f"[Network] Adjusting metric for LAN interface '{interface_name}'...")
    try:
        # Run PowerShell commands to set manual metric of 5 on Active and Persistent stores
        cmd = [
            "powershell", "-NoProfile", "-Command",
            f"Set-NetIPInterface -InterfaceAlias '{interface_name}' -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 5 -PolicyStore ActiveStore -ErrorAction SilentlyContinue; "
            f"Set-NetIPInterface -InterfaceAlias '{interface_name}' -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 5 -PolicyStore PersistentStore -ErrorAction SilentlyContinue"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            logger.info(f"[Network] Successfully adjusted metric for '{interface_name}' to 5.")
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
    $ics = Get-Service -Name "SharedAccess" -ErrorAction SilentlyContinue
    if ($ics -and $ics.Status -eq 'Running') {
        Stop-Service -Name "SharedAccess" -Force -ErrorAction SilentlyContinue
        Set-Service -Name "SharedAccess" -StartupType Disabled -ErrorAction SilentlyContinue
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
    """Xóa Windows NAT rule (GenRouterNAT) để sing-box nhận đúng source IP của phone.

    Khi GenRouterNAT đang chạy, nó masquerade source IP phone (192.168.10.100)
    thành TUN IP (172.19.0.1) TRƯỚC khi packet vào sing-box. Điều này khiến
    source_ip_cidr rules không match được và proxy không hoạt động.

    Sing-box với auto_detect_interface=True sẽ tự NAT ra WAN qua direct-out.
    """
    if platform.system() != "Windows":
        return
    logger.info("[Network] Removing GenRouterNAT to allow sing-box source IP matching...")
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$nat = Get-NetNat -Name 'GenRouterNAT' -ErrorAction SilentlyContinue; "
             "if ($nat) { Remove-NetNat -Name 'GenRouterNAT' -Confirm:$false -ErrorAction SilentlyContinue; "
             "Write-Host 'Removed' } else { Write-Host 'NotFound' }"],
            capture_output=True, text=True, timeout=10
        )
        out = result.stdout.strip()
        if "Removed" in out:
            logger.info("[Network] ✓ GenRouterNAT removed - sing-box will see original phone source IPs.")
        else:
            logger.info("[Network] GenRouterNAT not present (already removed or not created yet).")
    except Exception as e:
        logger.warning(f"[Network] Failed to remove GenRouterNAT: {e}")


def restore_nat_for_singbox(wan_interface: str, lan_subnet: str = "192.168.10.0/24"):
    """Khôi phục GenRouterNAT sau khi sing-box dừng.

    Khi sing-box stop, direct-traffic devices cần Windows NAT để ra internet.
    """
    if platform.system() != "Windows" or not wan_interface:
        return
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
    adjust_lan_interface_metric(lan_interface)
    if wan_interface:
        setup_nat(wan_interface=wan_interface, lan_subnet=lan_subnet)
    else:
        logger.info("[Network] WAN interface not provided, skipping NAT setup (will be called later from main).")
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

