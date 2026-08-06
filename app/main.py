"""
PhoneFarm GenRouter v2.0 — Main Entry Point

FastAPI application with:
- DHCP Server (Python)
- Sing-Box TUN transparent proxy
- MAC Registry (persistent device tracking)
- WebSocket realtime updates
- REST API dashboard
"""

import os
import sys
import logging
import asyncio
import platform
import subprocess
from contextlib import asynccontextmanager

def check_admin_elevation():
    if platform.system() != "Windows":
        return

    is_frozen = getattr(sys, 'frozen', False)
    # Check if running in a normal uvicorn / main app run context or frozen app
    is_app_run = (
        is_frozen or
        "uvicorn" in sys.argv[0].lower() or 
        (len(sys.argv) > 1 and "uvicorn" in sys.argv[1].lower()) or 
        any("main:app" in arg for arg in sys.argv)
    )
    if not is_app_run:
        return

    import ctypes
    try:
        is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        is_admin = False

    if not is_admin:
        print("=" * 60)
        print(" [!] GenRouter: Quyen Administrator la bat buoc de cau hinh mang.")
        print(" [!] Dang yeu cau cap quyen Administrator (UAC)...")
        print("=" * 60)

        executable = sys.executable
        if is_frozen:
            # For frozen exe, just run itself with original arguments
            params = " ".join(f'"{a}"' for a in sys.argv[1:])
        else:
            args = sys.argv.copy()
            new_args = []
            if len(args) > 1:
                if "-m" in args and "uvicorn" in args:
                    new_args = args
                else:
                    new_args = ["-m", "uvicorn"] + args[1:]
            else:
                new_args = ["-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
            params = " ".join(f'"{a}"' for a in new_args)

        try:
            res = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, os.getcwd(), 1)
            if int(res) > 32:
                sys.exit(0)
            else:
                raise OSError(f"ShellExecuteW returned code {res}")
        except Exception as e:
            print(f" [X] Yeu cau quyen Administrator that bai: {e}")
            print(" [X] Vui long chay lai PowerShell / Command Prompt duoi quyen Administrator thu cong.")
            sys.exit(1)

check_admin_elevation()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response, Depends, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import (
    load_config, save_config, AppConfig, ProxyConfig,
    migrate_old_config, PROJECT_DIR, RESOURCE_DIR
)
from app.mac_registry import MACRegistry
from app.singbox_manager import SingBoxManager
from app.dhcp_server import DHCPServer
from app.dependencies import get_config, get_mac_registry, get_singbox_manager

def setup_captive_portproxy(lan_ip: str):
    import subprocess
    # Xóa cấu hình cũ tránh trùng lặp
    subprocess.run(
        ["netsh", "interface", "portproxy", "delete", "v4tov4", "listenport=80", f"listenaddress={lan_ip}"],
        capture_output=True
    )
    # Thêm cấu hình mới
    subprocess.run(
        ["netsh", "interface", "portproxy", "add", "v4tov4", 
         "listenport=80", f"listenaddress={lan_ip}", 
         "connectport=8000", "connectaddress=127.0.0.1"],
        capture_output=True
    )
    logger.info(f"[Captive] Windows portproxy forward set up: 80 -> 8000 on address {lan_ip}")

def cleanup_captive_portproxy(lan_ip: str):
    import subprocess
    subprocess.run(
        ["netsh", "interface", "portproxy", "delete", "v4tov4", "listenport=80", f"listenaddress={lan_ip}"],
        capture_output=True
    )
    logger.info(f"[Captive] Windows portproxy forward cleaned up for address {lan_ip}")

# ─── Auto-Rotate State (global, queryable từ API) ────────────────────────────
# Cho phép endpoint /api/proxies/rotation-status đọc countdown và
# /api/proxies/rotation-reset reset lại timer mà không cần restart server.
_rotate_state: dict = {
    "last_rotate_time": 0.0,   # Unix timestamp lần xoay gần nhất (0 = chưa xoay)
    "last_rotate_count": 0,    # Số thiết bị đã xoay lần gần nhất
    "total_rotations": 0,      # Tổng số lần xoay kể từ khi khởi động
}


def perform_auto_rotation(config, mac_registry, singbox_manager):
    """Thực hiện xoay proxy cho tất cả thiết bị mà không cần HTTP request.
    
    Được gọi bởi: auto_rotate_loop (tự động), /api/proxies/rotate-all (thủ công)
    và /api/proxies/trigger-rotate (force trigger).
    Cập nhật _rotate_state sau mỗi lần xoay thành công để countdown API có data.
    """
    import time as _time
    if not mac_registry or not config.proxies or len(config.proxies) < 2:
        return 0
        
    all_devices = mac_registry.get_all_devices()
    proxy_devices = [d for d in all_devices if d.proxy_id]
    if not proxy_devices:
        return 0
        
    import random
    proxy_pool = config.proxies.copy()
    random.shuffle(proxy_pool)
    
    rotated = []
    for i, device in enumerate(proxy_devices):
        for offset in range(len(proxy_pool)):
            candidate = proxy_pool[(i + offset) % len(proxy_pool)]
            if candidate.id != device.proxy_id:
                break
        old_id = device.proxy_id
        mac_registry.set_proxy(device.mac, candidate.id)
        rotated.append({"mac": device.mac, "old": old_id, "new": candidate.id})
        
    if rotated and singbox_manager:
        try:
            assignments_to_apply = []
            for item in rotated:
                d = mac_registry.get_device_by_mac(item["mac"])
                if d and d.ip:
                    assignments_to_apply.append((d.ip, item["new"]))
            singbox_manager.update_multiple_devices_routing(assignments_to_apply)
            logger.info(f"[AutoRotate] Rotated {len(rotated)} devices successfully.")
        except Exception as e:
            logger.error(f"[AutoRotate] Failed to apply routing after auto rotate: {e}")

    # Cập nhật state để API endpoint có thể query
    _rotate_state["last_rotate_time"] = _time.time()
    _rotate_state["last_rotate_count"] = len(rotated)
    _rotate_state["total_rotations"] += 1
    return len(rotated)


async def auto_rotate_loop(app):
    """Vòng lặp chạy nền kiểm tra và thực hiện xoay proxy tự động theo chu kỳ phút.
    
    Dùng _rotate_state["last_rotate_time"] thay vì local variable để các API endpoint
    có thể:
    - Đọc countdown (rotation-status)
    - Reset timer (rotation-reset / rotate-all thủ công)
    """
    import time
    # Ghi thời điểm khởi động là last_rotate_time để countdown bắt đầu từ startup
    _rotate_state["last_rotate_time"] = time.time()
    
    while True:
        await asyncio.sleep(10)  # Check mỗi 10 giây
        
        mac_registry = getattr(app.state, "mac_registry", None)
        singbox_manager = getattr(app.state, "singbox_manager", None)
        
        if not mac_registry or not singbox_manager:
            continue

        # Dùng config đang giữ trong SingBoxManager (được các route đồng bộ qua
        # update_config() ngay sau mỗi save_config() liên quan) thay vì tự load_config()
        # đọc lại file JSON mỗi 10s vĩnh viễn — vòng lặp này chạy suốt 24/7 nên I/O thừa
        # tích luỹ theo thời gian, dù nhỏ vẫn là 1 nguồn hao mòn không cần thiết.
        config = singbox_manager.config
        if not config:
            continue

        if config.auto_rotate_minutes > 0:
            last_rotate = _rotate_state["last_rotate_time"]
            elapsed_minutes = (time.time() - last_rotate) / 60.0
            if elapsed_minutes >= config.auto_rotate_minutes:
                logger.info(f"[AutoRotate] Time trigger: {config.auto_rotate_minutes} mins reached. Rotating all proxies...")
                try:
                    perform_auto_rotation(config, mac_registry, singbox_manager)
                except Exception as err:
                    logger.error(f"[AutoRotate] Error performing auto rotation: {err}")

async def update_check_loop():
    """Vòng lặp chạy nền kiểm tra bản cập nhật mới định kỳ — CHỈ kiểm tra, không tự áp
    dụng (áp dụng luôn cần người dùng bấm xác nhận trên dashboard, xem routes/update.py
    và giải thích ở app/update_manager.py)."""
    from app import update_manager

    await asyncio.sleep(30)  # Đợi router khởi động ổn định trước khi check lần đầu
    while True:
        try:
            update_manager.check_for_update()
        except Exception as e:
            logger.warning(f"[AutoUpdate] Lỗi vòng lặp kiểm tra bản mới: {e}")
        await asyncio.sleep(6 * 3600)  # Mỗi 6 tiếng

from app.network_setup import (
    setup_network, setup_nat, verify_nat, ensure_wan_forwarding_disabled,
    optimize_tcp_stack, setup_interface_dns, detect_wan_interface, is_wan_interface_valid,
    detect_lan_interface, is_lan_interface_valid,
)

# ─── Logging Setup ───────────────────────────────────────
# RotatingFileHandler thay cho FileHandler: chạy 24/7 log sẽ phình vô hạn tới khi đầy ổ
# đĩa → treo/crash cả máy. Giới hạn 5MB × 5 file (tối đa ~25MB), tự xoay vòng.
from logging.handlers import RotatingFileHandler
log_file = os.path.join(PROJECT_DIR, "router_manager.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        ),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ─── Global Instances ────────────────────────────────────
_mac_registry: MACRegistry = None
_singbox_manager: SingBoxManager = None
_dhcp_server: DHCPServer = None
_ws_connections: set = set()


def get_mac_registry() -> MACRegistry:
    return _mac_registry


def _on_dhcp_lease(mac: str, ip: str, name: str, ip_changed: bool = True):
    """Callback from DHCP Server when a lease is granted/renewed.

    Chạy trên 1 thread nền riêng của DHCPServer (dhcp-lease-worker), KHÔNG còn chạy trên
    thread lắng nghe UDP nữa — xem giải thích ở DHCPServer._lease_worker_loop(). Vẫn nên
    tránh block quá lâu/không cần thiết vì các lease khác cũng xếp hàng qua cùng 1 worker.

    ip_changed: True nếu IP này KHÁC với IP đã lưu trước đó cho mac này (thiết bị mới,
    hoặc IP vừa được thu hồi/tái cấp — xem MACRegistry.allocate_new_ip/clear_stale_ips).
    False nếu đây chỉ là renew bình thường với ĐÚNG IP cũ.
    """
    logger.info(f"[Main] DHCP lease: {mac} → {ip} ({name})")
    
    # ── Auto-assign proxy to new devices if enabled; regardless, make sing-box aware
    #    of the new device so its captive/welcome routing+DNS rules get generated ──
    try:
        config = load_config()
        device_info = _mac_registry._data.get(mac.upper()) if _mac_registry else None

        assigned_now = False
        best_proxy_id = None
        if config.auto_assign_new_devices and _mac_registry and device_info and not device_info.get("proxy_decided", False):
            if mac.upper() not in getattr(_on_dhcp_lease, "_assigned_macs", set()):
                if not hasattr(_on_dhcp_lease, "_assigned_macs"):
                    _on_dhcp_lease._assigned_macs = set()
                _on_dhcp_lease._assigned_macs.add(mac.upper())

                # Retrieve list of live proxies
                live_proxies = [p for p in config.proxies if p.status and p.status.lower() == "live"]
                if live_proxies:
                    # Load balancing: count devices assigned to each live proxy
                    proxy_counts = {p.id: 0 for p in live_proxies}
                    for d_mac, d_inf in _mac_registry._data.items():
                        p_id = d_inf.get("proxy_id")
                        if p_id in proxy_counts:
                            proxy_counts[p_id] += 1

                    best_proxy_id = None
                    if getattr(config, "auto_assign_mode", "balance") == "exclusive":
                        candidate_ids = [p_id for p_id, count in proxy_counts.items() if count == 0]
                        if candidate_ids:
                            best_proxy_id = candidate_ids[0]
                            logger.info(f"[Main] Exclusive mode: Found vacant proxy {best_proxy_id} for device {mac}")
                        else:
                            logger.warning(f"[Main] Exclusive mode active but no vacant proxies available for device {mac}")
                    else:
                        best_proxy_id = min(proxy_counts, key=proxy_counts.get)
                        logger.info(f"[Main] Balance mode: Selected proxy {best_proxy_id} with least load")

                    if best_proxy_id:
                        _mac_registry.set_proxy(mac, best_proxy_id)
                        logger.info(f"[Main] Auto-assigned proxy {best_proxy_id} to device {mac}")
                        assigned_now = True
                else:
                    logger.warning(f"[Main] Auto-assign enabled but no LIVE proxies found for device {mac}")

        # Thiết bị mới hoặc vừa được gán proxy luôn cần sing-box biết tới NGAY
        if _singbox_manager:
            import ipaddress
            is_in_pool = False
            try:
                ip_addr = ipaddress.IPv4Address(ip)
                start_ip = ipaddress.IPv4Address(config.dhcp_range_start)
                end_ip = ipaddress.IPv4Address(config.dhcp_range_end)
                is_in_pool = start_ip <= ip_addr <= end_ip
            except Exception:
                pass

            if is_in_pool:
                # Chỉ đồng bộ routing qua Clash API khi THỰC SỰ cần: vừa auto-assign proxy
                # mới (assigned_now), HOẶC IP này vừa đổi chủ (ip_changed — thiết bị mới,
                # hoặc IP vừa được thu hồi từ 1 MAC khác đã offline lâu, xem
                # MACRegistry.allocate_new_ip/clear_stale_ips — selector Clash cho IP đó có
                # thể vẫn còn trỏ tới proxy CŨ của chủ trước, cần đẩy lại đúng proxy hiện tại
                # để tránh rò rỉ định tuyến). KHÔNG gọi cho renew bình thường (ip không đổi)
                # — routing chắc chắn vẫn đúng từ trước, gọi lại mỗi lần renew (hàng trăm
                # thiết bị, mỗi ~30-60 phút/lần) chỉ tổ tăng nguy cơ full-restart không cần
                # thiết. Đã xác nhận qua log thực tế: gọi Clash API NGAY sau khi sing-box vừa
                # full-restart (API chưa kịp sẵn sàng) → timeout → lại trigger full-restart
                # → thiết bị renew tiếp theo lặp lại y hệt → cascade restart liên tục, mỗi
                # lần chỉ xử lý được đúng 1 thiết bị, khiến thiết bị mới không bao giờ xin
                # được IP (xem DHCPServer._lease_worker_loop() để biết vì sao việc này giờ
                # không còn chặn hẳn DHCP nữa — nhưng vẫn nên tránh trigger khi không cần).
                if assigned_now or ip_changed:
                    current_proxy_id = best_proxy_id if assigned_now else (
                        _mac_registry.get_proxy_for_mac(mac) if _mac_registry else None
                    )
                    ok = _singbox_manager.update_device_routing(ip, current_proxy_id)
                    if ok:
                        target_desc = f"proxy {current_proxy_id}" if current_proxy_id else "direct"
                        logger.info(f"[Main] Synced routing for device {mac} ({ip}) → {target_desc} via Clash API.")
                    else:
                        logger.warning(f"[Main] Failed dynamic routing sync for {mac} ({ip}), falling back to hot reload.")
                        _singbox_manager.update_config(config)
                        _singbox_manager.hot_reload()
            else:
                # IP nằm ngoài DHCP pool, bắt buộc phải hot-reload để thêm rule routing mới
                reloaded_macs = getattr(_on_dhcp_lease, "_reloaded_macs", set())
                if not hasattr(_on_dhcp_lease, "_reloaded_macs"):
                    _on_dhcp_lease._reloaded_macs = reloaded_macs
                if assigned_now or mac.upper() not in reloaded_macs:
                    reloaded_macs.add(mac.upper())
                    _singbox_manager.update_config(config)
                    _singbox_manager.hot_reload()
                    logger.info(f"[Main] Hot-reloaded routing rules for out-of-pool device {mac} ({ip})")
    except Exception as e:
        logger.error(f"[Main] Error in auto-assigning proxy to device: {e}", exc_info=True)

    # Notify WebSocket clients
    _broadcast_ws({
        "type": "device_update",
        "mac": mac,
        "ip": ip,
        "name": name,
        "proxy_id": _mac_registry.get_proxy_for_mac(mac) if _mac_registry else None,
    })


def _broadcast_ws(data: dict):
    """Broadcast message to all WebSocket clients."""
    import json
    try:
        if not _ws_connections:
            return
        msg = json.dumps(data)
        disconnected = set()
        for ws in list(_ws_connections):
            try:
                asyncio.get_event_loop().call_soon_threadsafe(
                    asyncio.ensure_future, ws.send_text(msg)
                )
            except Exception:
                disconnected.add(ws)
        for ws in disconnected:
            _ws_connections.discard(ws)
    except Exception as e:
        logger.debug(f"[WS] Broadcast error (non-critical): {e}")


def _get_host_macs_helper() -> set:
    """Helper to get all MAC addresses of the host machine."""
    macs = set()
    import platform
    import subprocess
    
    if platform.system() == "Windows":
        try:
            cmd = ["powershell", "-NoProfile", "-Command", "Get-NetAdapter -ErrorAction SilentlyContinue | Select-Object -ExpandProperty MacAddress"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    line = line.strip()
                    if line:
                        clean = line.replace("-", ":").upper()
                        if len(clean) == 17 and all(c in '0123456789ABCDEF:' for c in clean):
                            macs.add(clean)
        except Exception:
            pass

    try:
        import psutil
        addrs = psutil.net_if_addrs()
        for interface, interface_addrs in addrs.items():
            for addr in interface_addrs:
                if hasattr(psutil, 'AF_LINK') and addr.family == psutil.AF_LINK:
                    mac = addr.address.replace("-", ":").upper()
                    if mac:
                        macs.add(mac)
                elif addr.family == -1 or ":" in addr.address or "-" in addr.address:
                    clean = addr.address.replace("-", ":").upper()
                    if len(clean) == 17 and all(c in '0123456789ABCDEF:' for c in clean):
                        macs.add(clean)
    except Exception:
        pass

    try:
        import uuid
        node_mac = ':'.join(['{:02X}'.format((uuid.getnode() >> ele) & 0xff) for ele in range(0,8*6,8)][::-1])
        macs.add(node_mac)
    except Exception:
        pass
        
    return macs


def setup_windows_startup():
    """Tự động đăng ký c69-router chạy cùng Windows Task Scheduler ở quyền cao nhất (bypass UAC khi startup)"""
    if platform.system() != "Windows":
        return
    
    # Chỉ đăng ký khi chạy bản exe đóng gói (frozen)
    if not getattr(sys, "frozen", False):
        logger.info("[Startup] Chạy ở dạng script, bỏ qua đăng ký khởi động cùng Windows.")
        return
        
    exe_path = sys.executable
    exe_dir = os.path.dirname(exe_path)
    task_name = "GenRouterAutoStart"
    
    try:
        # Kiểm tra xem task đã tồn tại chưa
        check_cmd = f'powershell -NoProfile -Command "Get-ScheduledTask -TaskName \'{task_name}\' -ErrorAction SilentlyContinue"'
        res = subprocess.run(check_cmd, shell=True, capture_output=True, text=True)
        if task_name in res.stdout:
            logger.info(f"[Startup] Windows Task '{task_name}' đã được đăng ký trước đó.")
            return
            
        logger.info(f"[Startup] Đang đăng ký Windows Task '{task_name}' để khởi động cùng Windows...")
        
        ps_script = (
            f"$action = New-ScheduledTaskAction -Execute '{exe_path}' -WorkingDirectory '{exe_dir}';\n"
            f"$trigger = New-ScheduledTaskTrigger -AtLogOn;\n"
            f"$principal = New-ScheduledTaskPrincipal -GroupId 'BUILTIN\\Administrators' -RunLevel Highest;\n"
            f"$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero);\n"
            f"Register-ScheduledTask -TaskName '{task_name}' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force;\n"
            f"Write-Host 'OK'\n"
        )
        
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".ps1")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                tmp.write(ps_script)
            cmd = f'powershell -NoProfile -ExecutionPolicy Bypass -File "{path}"'
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
            output = result.stdout.strip()
            if "OK" in output:
                logger.info(f"[Startup] Đăng ký Windows Task '{task_name}' khởi động cùng Windows thành công.")
            else:
                logger.warning(f"[Startup] Đăng ký khởi động thất bại: {result.stderr.strip() or output}")
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"[Startup] Lỗi khi đăng ký khởi động cùng Windows: {e}")


# ─── App Lifespan ────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    global _mac_registry, _singbox_manager, _dhcp_server

    logger.info("=" * 60)
    logger.info("  GenRouter v2.0 — Starting up...")
    logger.info("=" * 60)

    # 1. Migrate old config if exists
    old_devices = migrate_old_config()

    # 2. Load config
    config = load_config()

    # 2.5 Setup auto-startup
    setup_windows_startup()

    # 3. Initialize MAC Registry
    _mac_registry = MACRegistry()
    if old_devices:
        _mac_registry.migrate_from_old_devices(old_devices)
        logger.info(f"[Main] Migrated {len(old_devices)} devices from old config.")

    # Auto-clean host machine interfaces from MAC registry to prevent loop routing and congestion
    try:
        import socket
        my_hostname = socket.gethostname().upper()
        host_macs = _get_host_macs_helper()
        
        cleaned_any = False
        for device in _mac_registry.get_all_devices():
            mac_upper = device.mac.upper()
            name_upper = device.name.upper() if device.name else ""
            
            # Check if it is a RAS adapter belonging to this host
            is_my_ras = False
            if mac_upper.startswith("52:41:53:20"):
                clean_mac = mac_upper.replace(":", "")
                for host_mac in host_macs:
                    clean_host = host_mac.replace(":", "")
                    if clean_host in clean_mac:
                        is_my_ras = True
                        break
            
            is_host = (
                mac_upper in host_macs
                or is_my_ras
                or (device.name and name_upper == my_hostname)
                or device.ip == config.lan_gateway_ip
            )
            
            if is_host:
                logger.info(f"[Main] Auto-removing host machine device from registry: {device.mac} ({device.name} / {device.ip})")
                _mac_registry.remove_device(device.mac)
                cleaned_any = True
                
        if cleaned_any:
            _mac_registry.save()
    except Exception as e:
        logger.warning(f"[Main] Failed to clean host devices from registry: {e}")

    # 3.4 Dọn pool IP 1 lần lúc khởi động: gỡ IP (giữ nguyên proxy_id/name) của các thiết
    # bị đã offline quá lâu (>2x lease_time) — tránh pool bị đầy dần theo thời gian bởi
    # thiết bị đã die/đổi máy mà không ai chủ động xoá. Không xoá device/proxy, chỉ nhẹ pool.
    try:
        cleared = _mac_registry.clear_stale_ips(
            config.dhcp_range_start, config.dhcp_range_end, config.dhcp_lease_time
        )
        if cleared:
            logger.info(f"[Main] Startup pool cleanup: freed {len(cleared)} stale IP(s) from long-offline devices.")
    except Exception as e:
        logger.warning(f"[Main] Startup stale-IP cleanup error: {e}")

    # 3.5 Xác minh WAN interface đang lưu trong config còn thực sự có internet không
    # (đề phòng data/config.json lỗi thời — vd copy sang máy khác, đổi tên card, card
    # WiFi đang mất kết nối — nếu không xác minh, NAT/sing-box sẽ trỏ ra card chết
    # trong khi máy có card khác đang hoạt động tốt, khiến thiết bị kết nối vào có IP
    # nhưng không có internet).
    if not is_wan_interface_valid(config.wan_interface):
        detected_wan = detect_wan_interface()
        logger.warning(
            f"[Main] ⚠ WAN interface '{config.wan_interface}' không có route internet hợp lệ "
            f"— tự động chuyển sang '{detected_wan}'."
        )
        config.wan_interface = detected_wan
        save_config(config)
    else:
        logger.info(f"[Main] ✓ WAN interface '{config.wan_interface}' có route internet hợp lệ.")

    # 3.6 Autodetect LAN interface: nếu config.lan_interface không tồn tại/không UP/trùng
    # WAN (đổi máy mới, tên card khác "Ethernet 3" mặc định...), tự quét card mạng CÓ
    # DÂY nào đang cắm mà KHÔNG PHẢI card internet (WAN) để dùng làm LAN bridge — không
    # cần người dùng tự chọn tay trên máy mới nữa.
    if not is_lan_interface_valid(config.lan_interface, wan_interface=config.wan_interface):
        detected_lan = detect_lan_interface(exclude_interface=config.wan_interface)
        logger.warning(
            f"[Main] ⚠ LAN interface '{config.lan_interface}' không hợp lệ (không tồn tại/"
            f"không UP/trùng WAN) — tự động chuyển sang '{detected_lan}'."
        )
        config.lan_interface = detected_lan
        save_config(config)
    else:
        logger.info(f"[Main] ✓ LAN interface '{config.lan_interface}' hợp lệ.")

    # 4. Setup network (LAN static IP, IP forwarding, firewall, binaries)
    try:
        setup_network(
            lan_interface=config.lan_interface,
            wan_interface=config.wan_interface,
            lan_subnet=f"{config.lan_gateway_ip.rsplit('.', 1)[0]}.0/24",
            lan_gateway_ip=config.lan_gateway_ip,
            lan_subnet_mask=config.lan_subnet_mask,
        )
    except Exception as e:
        logger.error(f"[Main] Network setup error: {e}")
        from app.error_reporter import report_error
        report_error("Startup", f"Network setup error: {e}", level="critical", exc=e)

    # 4.0 TCP Stack Optimization — apply in background to avoid blocking startup
    # Optimize Windows TCP: Nagle off, ICWV=10, RSS, DCA, CTCP, ECN
    import threading
    def _run_tcp_optimize():
        try:
            results = optimize_tcp_stack(tun_interface_name=config.tun_interface or "GenRouterTUN")
            ok = sum(1 for v in results.values() if v is True)
            logger.info(f"[Main] TCP optimization complete: {ok}/{len(results)} applied")
        except Exception as e:
            logger.warning(f"[Main] TCP optimization non-critical error: {e}")
    threading.Thread(target=_run_tcp_optimize, daemon=True, name="tcp-optimizer").start()

    # 4.1 LAN Interface Health Check — detect disconnected Aruba cable early
    from app.network_setup import check_lan_interface_health
    lan_health = check_lan_interface_health(config.lan_interface)
    if lan_health["status"] != "healthy":
        logger.warning("=" * 60)
        logger.warning(f"  🔴 LAN INTERFACE PROBLEM DETECTED")
        logger.warning(f"  {lan_health['message']}")
        if lan_health.get("suggestions"):
            for suggestion in lan_health["suggestions"]:
                logger.warning(f"  → {suggestion}")
        logger.warning("=" * 60)
    else:
        logger.info(f"[Main] ✓ LAN Health: {lan_health['message']}")

    # 4.2 Verify NAT rule is active
    nat_status = verify_nat()
    if nat_status.get("ok"):
        logger.info(f"[Main] ✓ NAT active: {[r.get('InternalIPInterfaceAddressPrefix') for r in nat_status['rules']]}")
    else:
        logger.warning(
            "[Main] ⚠ NAT rule not found! "
            "Phông kết nối Aruba sẽ không có internet. "
            "Chạy lại với quyền Administrator."
        )
        from app.error_reporter import report_error
        report_error("NAT", "NAT rule not found after startup", level="warning")

    # 4.5 Auto-detect actual LAN interface IP
    from app.network_setup import get_lan_ip
    actual_lan_ip = get_lan_ip(config.lan_interface)
    if actual_lan_ip != config.lan_gateway_ip:
        logger.warning(
            f"[Main] ⚠ LAN interface '{config.lan_interface}' has IP {actual_lan_ip} "
            f"but config says {config.lan_gateway_ip}. Using actual: {actual_lan_ip}"
        )
        config.lan_gateway_ip = actual_lan_ip
        save_config(config)

    logger.info(f"[Main] LAN Gateway IP: {config.lan_gateway_ip}")
    logger.info(f"[Main] DHCP Pool: {config.dhcp_range_start} → {config.dhcp_range_end}")

    # 5. Initialize Sing-Box Manager
    _singbox_manager = SingBoxManager(config, _mac_registry)
    _singbox_manager.start_watchdog()

    # 6. Start DHCP Server
    if config.dhcp_enabled:
        _dhcp_server = DHCPServer(
            server_ip=config.lan_gateway_ip,
            subnet_mask=config.lan_subnet_mask,
            # LƯU Ý: KHÔNG dùng config.lan_gateway_ip ở đây — gateway nằm cùng subnet LAN với
            # phone (on-link) nên traffic tới nó không bao giờ đi qua route mặc định (TUN capture),
            # bất kể có exclude hay không → hijack-dns sẽ không bao giờ bắt được. Phải dùng 1 IP
            # public off-subnet (config.dns_server) để buộc traffic đi qua default route → vào TUN.
            dns_server=config.dns_server,
            pool_start=config.dhcp_range_start,
            pool_end=config.dhcp_range_end,
            lease_time=config.dhcp_lease_time,
            mac_registry=_mac_registry,
            on_lease_callback=_on_dhcp_lease,
            interface_name=config.lan_interface,
        )
        _dhcp_server.start()
    else:
        _dhcp_server = None
        logger.info("[Main] DHCP Server disabled in config.")

    # 7. Apply routing (start sing-box)
    if platform.system() == "Windows":
        try:
            _singbox_manager.apply_routing(config)
        except Exception as e:
            logger.error(f"[Main] Initial routing error: {e}")
            from app.error_reporter import report_error
            report_error("Startup", f"Initial routing error: {e}", level="critical", exc=e)

    # 8. Set DNS consistent on LAN + TUN interfaces (chỉ áp dụng cho chính máy host)
    # Đảm bảo Ethernet 3 và TUN adapter cùng dùng 1.1.1.1 / 1.0.0.1
    # LƯU Ý: DHCP phát cho PHONE dùng config.lan_gateway_ip (không phải 1.1.1.1),
    # vì 1.1.1.1 bị route_exclude_address loại khỏi TUN (singbox_manager.py) để tránh
    # sing-box tự loop — nếu phone dùng thẳng 1.1.1.1 thì DNS/DoT/DoH sẽ thoát TUN,
    # đi thẳng ra WAN thật thay vì được hijack-dns xử lý.
    if platform.system() == "Windows":
        try:
            setup_interface_dns(
                lan_interface=config.lan_interface,
                tun_interface=config.tun_interface,
                primary_dns="1.1.1.1",
                secondary_dns="1.0.0.1",
            )
        except Exception as e:
            logger.warning(f"[Main] DNS interface setup warning: {e}")

        # Retry Nagle disable after TUN interface is created (5s after sing-box start)
        # Lần đầu có thể fail vì TUN interface chưa tồn tại khi optimize_tcp_stack() chạy
        def _retry_nagle():
            import time
            time.sleep(6)   # Wait for sing-box + Wintun to fully initialize
            try:
                tun_name = config.tun_interface or "GenRouterTUN"
                results = optimize_tcp_stack(tun_interface_name=tun_name)
                nagle_ok = results.get("nagle_disabled") is True
                logger.info(f"[Main] Post-TUN Nagle retry: {'✓ disabled' if nagle_ok else results.get('nagle_disabled', 'skipped')}")
            except Exception as e:
                logger.debug(f"[Main] Nagle retry non-critical error: {e}")
        threading.Thread(target=_retry_nagle, daemon=True, name="nagle-retry").start()

    # Store in app state for FastAPI Dependency Injection
    app.state.mac_registry = _mac_registry
    app.state.singbox_manager = _singbox_manager
    app.state.dhcp_server = _dhcp_server

    # 7. Setup Captive Portal portproxy on LAN IP
    try:
        setup_captive_portproxy(config.lan_gateway_ip)
    except Exception as e:
        logger.error(f"[Main] Setup captive portproxy error: {e}")

    # 8. Start Auto-Rotate background loop
    asyncio.create_task(auto_rotate_loop(app))

    # 9. Start Auto-Update check background loop (chỉ kiểm tra, không tự áp dụng)
    asyncio.create_task(update_check_loop())

    yield  # App is running

    # ── Shutdown ──
    logger.info("[Main] Shutting down...")
    try:
        config = load_config()
        cleanup_captive_portproxy(config.lan_gateway_ip)
    except Exception as e:
        logger.error(f"[Main] Cleanup captive portproxy error: {e}")
        
    if _dhcp_server:
        _dhcp_server.stop()
    if _singbox_manager:
        _singbox_manager.stop()
    if _mac_registry:
        _mac_registry.save()
    logger.info("[Main] Shutdown complete.")


# ─── FastAPI App ─────────────────────────────────────────

from app.update_manager import CLIENT_VERSION

app = FastAPI(
    title=f"GenRouter v{CLIENT_VERSION}",
    description="PhoneFarm GenRouter — Transparent Proxy Router",
    version=CLIENT_VERSION,
    lifespan=lifespan,
)

@app.middleware("http")
async def add_cors_header(request: Request, call_next):
    origin = request.headers.get("Origin", "*")
    if request.method == "OPTIONS":
        response = Response()
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    return response

# Static files
STATIC_DIR = os.path.join(RESOURCE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ─── Register Routes ────────────────────────────────────

from app.routes.status import router as status_router
from app.routes.devices import router as devices_router
from app.routes.proxies import router as proxies_router
from app.routes.settings import router as settings_router
from app.routes.pac import router as pac_router
from app.routes.update import router as update_router

app.include_router(status_router)
app.include_router(devices_router)
app.include_router(proxies_router)
app.include_router(settings_router)
app.include_router(pac_router)
app.include_router(update_router)


# ─── Sing-Box Log Endpoint ──────────────────────────────

from fastapi import Query as QueryParam

@app.get("/api/singbox/log")
def get_singbox_log(lines: int = QueryParam(default=30, ge=1, le=200)):
    """Trả về N dòng cuối của sing-box log để debug routing."""
    log_files = [
        os.path.join(PROJECT_DIR, "sing-box-err.log"),
        os.path.join(PROJECT_DIR, "sing-box-out.log"),
        os.path.join(PROJECT_DIR, "sing-box.log"),
    ]
    for log_path in log_files:
        if os.path.exists(log_path) and os.path.getsize(log_path) > 0:
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()
                tail = "".join(all_lines[-lines:])
                return {"log": tail, "file": os.path.basename(log_path), "total_lines": len(all_lines)}
            except Exception as e:
                return {"log": f"Error reading log: {e}", "file": os.path.basename(log_path)}
    return {"log": "Chua co log nao. Sing-box chua duoc khoi dong hoac log bi xoa.", "file": None}



# ─── WebSocket ───────────────────────────────────────────

@app.websocket("/ws/status")
async def websocket_status(websocket: WebSocket):
    await websocket.accept()
    _ws_connections.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        _ws_connections.discard(websocket)
    except Exception:
        _ws_connections.discard(websocket)


# ─── Captive Portal Page ──────────────────────────────────

# ─── Captive Portal Page ──────────────────────────────────

from pydantic import BaseModel
from typing import Optional

class AssignCustomProxyPayload(BaseModel):
    mac: str
    type: str
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None

@app.post("/api/devices/assign-custom")
def assign_custom_proxy(
    payload: AssignCustomProxyPayload,
    config=Depends(get_config),
    mac_registry=Depends(get_mac_registry),
    singbox_manager=Depends(get_singbox_manager),
):
    mac = payload.mac.upper()
    device = mac_registry.get_device_by_mac(mac) if mac_registry else None
    if not device:
        raise HTTPException(status_code=404, detail=f"Device {mac} not found")
        
    mac_clean = mac.replace(":", "_").replace("-", "_")
    proxy_id = f"proxy_custom_{mac_clean}"
    
    existing = next((p for p in config.proxies if p.id == proxy_id), None)
    if existing:
        existing.type = payload.type
        existing.host = payload.host
        existing.port = payload.port
        existing.username = payload.username
        existing.password = payload.password
        existing.status = "Live"
    else:
        new_proxy = ProxyConfig(
            id=proxy_id,
            type=payload.type,
            host=payload.host,
            port=payload.port,
            username=payload.username,
            password=payload.password,
            status="Live",
            latency=0,
            dns_server="1.1.1.1"
        )
        config.proxies.append(new_proxy)
        
    save_config(config)
    mac_registry.set_proxy(mac, proxy_id)
    
    reload_ok = True
    try:
        if singbox_manager:
            singbox_manager.update_config(config)
            reload_ok = singbox_manager.update_device_routing(device.ip, proxy_id)
    except Exception as e:
        reload_ok = False
        logger.error(f"[Captive API] Custom routing update error: {e}")
        
    if not reload_ok:
        raise HTTPException(status_code=502, detail="Lưu proxy thành công nhưng áp dụng định tuyến thất bại.")
        
    return {"status": "success", "message": f"Custom proxy assigned to {mac}"}

@app.get("/captive", response_class=HTMLResponse)
def captive_portal(request: Request, config=Depends(get_config), mac_registry=Depends(get_mac_registry)):
    client_ip = request.client.host
    
    device = None
    if mac_registry:
        for d in mac_registry.get_all_devices():
            if d.ip == client_ip:
                device = d
                break
                
    client_mac = device.mac if device else "Chưa xác định"
    current_proxy = device.proxy_id if device else None
    
    proxies_options = ""
    for p in config.proxies:
        if p.id.startswith("proxy_custom_"):
            continue
        if p.status == "Live":
            selected = 'selected' if current_proxy == p.id else ''
            proxies_options += f'<option value="{p.id}" {selected}>{p.id.replace("proxy_", "")} ({p.host}:{p.port} - {p.latency}ms)</option>\n'
            
    custom_init_js = ""
    if current_proxy and current_proxy.startswith("proxy_custom_"):
        custom_p = next((p for p in config.proxies if p.id == current_proxy), None)
        if custom_p:
            custom_init_js = f'''
            document.getElementById("customType").value = "{custom_p.type}";
            document.getElementById("customHost").value = "{custom_p.host}";
            document.getElementById("customPort").value = "{custom_p.port}";
            document.getElementById("customUser").value = "{custom_p.username or ''}";
            document.getElementById("customPass").value = "{custom_p.password or ''}";
            document.getElementById("proxyRawInput").value = "{custom_p.host}:{custom_p.port}{':' + custom_p.username if custom_p.username else ''}{':' + custom_p.password if custom_p.password else ''}";
            '''

    html_content = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>GenRouter — Cấu hình kết nối</title>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">
        <style>
            :root {{
                --bg-color: #0b0f19;
                --card-bg: rgba(255, 255, 255, 0.03);
                --card-border: rgba(255, 255, 255, 0.08);
                --text-color: #f3f4f6;
                --text-muted: #9ca3af;
                --accent: #00f2fe;
                --accent-gradient: linear-gradient(135deg, #00f2fe, #4facfe);
                --success: #10b981;
                --info: #3b82f6;
            }}
            body {{
                font-family: 'Outfit', -apple-system, sans-serif;
                background-color: var(--bg-color);
                color: var(--text-color);
                margin: 0;
                padding: 16px;
                display: flex;
                flex-direction: column;
                align-items: center;
                min-height: 100vh;
                box-sizing: border-box;
            }}
            .container {{
                width: 100%;
                max-width: 480px;
                background: rgba(255, 255, 255, 0.02);
                border: 1px solid var(--card-border);
                border-radius: 20px;
                padding: 24px 20px;
                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
                backdrop-filter: blur(10px);
                box-sizing: border-box;
            }}
            .header {{
                text-align: center;
                margin-bottom: 20px;
            }}
            .logo {{
                font-size: 24px;
                font-weight: 700;
                background: var(--accent-gradient);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                margin-bottom: 6px;
                letter-spacing: -0.5px;
            }}
            .subtitle {{
                color: var(--text-muted);
                font-size: 13px;
            }}
            .info-box {{
                background: rgba(255, 255, 255, 0.02);
                border: 1px solid var(--card-border);
                border-radius: 12px;
                padding: 12px 16px;
                margin-bottom: 20px;
                font-size: 13px;
            }}
            .info-row {{
                display: flex;
                justify-content: space-between;
                margin-bottom: 6px;
            }}
            .info-row:last-child {{
                margin-bottom: 0;
            }}
            .info-label {{
                color: var(--text-muted);
            }}
            .info-value {{
                font-family: monospace;
                font-weight: 600;
            }}
            .section-title {{
                font-size: 14px;
                font-weight: 600;
                margin-bottom: 12px;
                color: var(--text-color);
            }}
            .btn {{
                width: 100%;
                padding: 14px;
                border-radius: 12px;
                border: none;
                font-size: 14px;
                font-weight: 600;
                cursor: pointer;
                background: var(--accent-gradient);
                color: white;
                box-shadow: 0 4px 15px rgba(0, 242, 254, 0.3);
            }}
            .loading-overlay {{
                position: fixed;
                top: 0; left: 0; right: 0; bottom: 0;
                background: rgba(11, 15, 25, 0.85);
                display: none;
                flex-direction: column;
                justify-content: center;
                align-items: center;
                z-index: 100;
            }}
            .spinner {{
                width: 32px;
                height: 32px;
                border: 3px solid rgba(255,255,255,0.1);
                border-top-color: var(--accent);
                border-radius: 50%;
                animation: spin 1s linear infinite;
                margin-bottom: 12px;
            }}
            @keyframes spin {{
                0% {{ transform: rotate(0deg); }}
                100% {{ transform: rotate(360deg); }}
            }}
        </style>
        <script>
            function toggleCustomProxyInput() {{
                const val = document.getElementById('proxySelect').value;
                const div = document.getElementById('customProxyDiv');
                if (val === 'Custom') {{
                    div.style.display = 'block';
                }} else {{
                    div.style.display = 'none';
                }}
            }}
            
            function onRawInputChanged() {{
                const raw = document.getElementById('proxyRawInput').value.trim();
                if (!raw) return;
                
                let cleaned = raw;
                let type = "socks5";
                if (cleaned.startsWith("http://")) {{
                    type = "http";
                    cleaned = cleaned.substring(7);
                }} else if (cleaned.startsWith("socks5://")) {{
                    type = "socks5";
                    cleaned = cleaned.substring(9);
                }} else if (cleaned.startsWith("socks://")) {{
                    type = "socks5";
                    cleaned = cleaned.substring(8);
                }}
                
                let host = "";
                let port = "";
                let user = "";
                let pass = "";
                
                if (cleaned.includes("@")) {{
                    let parts = cleaned.split("@");
                    let auth = parts[0];
                    let net = parts[1];
                    
                    if (auth.includes(":")) {{
                        let authParts = auth.split(":");
                        user = authParts[0];
                        pass = authParts[1];
                    }} else {{
                        user = auth;
                    }}
                    
                    let netParts = net.split(":");
                    host = netParts[0];
                    port = netParts[1];
                }} else {{
                    let parts = cleaned.split(":");
                    if (parts.length === 2) {{
                        host = parts[0];
                        port = parts[1];
                    }} else if (parts.length === 4) {{
                        host = parts[0];
                        port = parts[1];
                        user = parts[2];
                        pass = parts[3];
                    }}
                }}
                
                if (host) document.getElementById('customHost').value = host;
                if (port) document.getElementById('customPort').value = port;
                if (user) document.getElementById('customUser').value = user;
                if (pass) document.getElementById('customPass').value = pass;
                document.getElementById('customType').value = type;
            }}

            async function saveConfig() {{
                const val = document.getElementById('proxySelect').value;
                document.getElementById('loading').style.display = 'flex';
                
                let url = '/api/devices/assign';
                let body = {{
                    mac: '{client_mac}',
                    proxy_id: val === 'Direct' ? null : val
                }};
                
                if (val === 'Custom') {{
                    url = '/api/devices/assign-custom';
                    const host = document.getElementById('customHost').value.trim();
                    const portVal = document.getElementById('customPort').value.trim();
                    const port = parseInt(portVal);
                    const type = document.getElementById('customType').value;
                    const username = document.getElementById('customUser').value.trim();
                    const password = document.getElementById('customPass').value.trim();
                    
                    if (!host || !portVal || isNaN(port)) {{
                        document.getElementById('loading').style.display = 'none';
                        alert('Vui lòng nhập đầy đủ Host và Port hợp lệ!');
                        return;
                    }}
                    
                    body = {{
                        mac: '{client_mac}',
                        type: type,
                        host: host,
                        port: port,
                        username: username || null,
                        password: password || null
                    }};
                }}
                
                try {{
                    const res = await fetch(url, {{
                        method: 'POST',
                        headers: {{
                            'Content-Type': 'application/json'
                        }},
                        body: JSON.stringify(body)
                    }});
                    
                    const data = await res.json();
                    document.getElementById('loading').style.display = 'none';
                    
                    if (res.ok) {{
                        alert('Thiết lập kết nối thành công! Hãy tắt cửa sổ này (hoặc bấm Done / Xong ở góc màn hình) để truy cập mạng.');
                        window.location.reload();
                    }} else {{
                        alert('Lỗi: ' + (data.detail || 'Không thể áp dụng cấu hình'));
                    }}
                }} catch (e) {{
                    document.getElementById('loading').style.display = 'none';
                    alert('Lỗi kết nối: ' + e.message);
                }}
            }}

            window.onload = function() {{
                toggleCustomProxyInput();
                {custom_init_js}
            }};
        </script>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="logo">GenRouter Captive</div>
                <div class="subtitle">Thiết lập Proxy kết nối Wi-Fi</div>
            </div>
            
            <div class="info-box">
                <div class="info-row">
                    <span class="info-label">IP:</span>
                    <span class="info-value">{client_ip}</span>
                </div>
                <div class="info-row">
                    <span class="info-label">MAC:</span>
                    <span class="info-value">{client_mac}</span>
                </div>
                <div class="info-row">
                    <span class="info-label">Đang gán:</span>
                    <span class="info-value" style="color: var(--accent);">{current_proxy.replace('proxy_', '') if current_proxy else 'Direct'}</span>
                </div>
            </div>
            
            <div class="section-title">
                🌐 Chọn Proxy kết nối
            </div>
            
            <div style="margin-bottom: 20px;">
                <select id="proxySelect" onchange="toggleCustomProxyInput()" style="width: 100%; padding: 12px; border-radius: 10px; background: var(--card-bg); border: 1px solid var(--card-border); color: var(--text-color); font-family: inherit; font-size: 14px; cursor: pointer;">
                    <option value="Direct" {'selected' if not current_proxy or current_proxy.startswith('proxy_custom_') else ''}>Direct (Không proxy)</option>
                    {proxies_options}
                    <option value="Custom" {'selected' if current_proxy and current_proxy.startswith('proxy_custom_') else ''}>➕ Tự điền proxy thủ công...</option>
                </select>
            </div>

            <div id="customProxyDiv" style="display: none; background: rgba(255,255,255,0.01); border: 1px solid var(--card-border); border-radius: 12px; padding: 16px; margin-bottom: 20px;">
                <div style="font-size: 13px; font-weight: 500; margin-bottom: 10px; color: var(--accent);">Nhập proxy của bạn:</div>
                
                <div style="margin-bottom: 12px;">
                    <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Chuỗi proxy (Host:Port hoặc Host:Port:User:Pass):</label>
                    <input type="text" id="proxyRawInput" placeholder="Ví dụ: 192.168.1.100:8080" style="width: 100%; padding: 10px; border-radius: 8px; background: rgba(0,0,0,0.2); border: 1px solid var(--card-border); color: var(--text-color); box-sizing: border-box; font-family: monospace; font-size: 13px;" oninput="onRawInputChanged()" />
                </div>
                
                <div style="display: flex; gap: 8px; margin-bottom: 12px;">
                    <div style="flex: 2;">
                        <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Loại:</label>
                        <select id="customType" style="width: 100%; padding: 9px; border-radius: 8px; background: rgba(0,0,0,0.2); border: 1px solid var(--card-border); color: var(--text-color); font-size: 12.5px; height: 36px; cursor: pointer;">
                            <option value="socks5">SOCKS5</option>
                            <option value="http">HTTP</option>
                        </select>
                    </div>
                    <div style="flex: 5;">
                        <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Host:</label>
                        <input type="text" id="customHost" style="width: 100%; padding: 9px; border-radius: 8px; background: rgba(0,0,0,0.2); border: 1px solid var(--card-border); color: var(--text-color); font-size: 12.5px; box-sizing: border-box; height: 36px;" />
                    </div>
                    <div style="flex: 3;">
                        <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Port:</label>
                        <input type="number" id="customPort" style="width: 100%; padding: 9px; border-radius: 8px; background: rgba(0,0,0,0.2); border: 1px solid var(--card-border); color: var(--text-color); font-size: 12.5px; box-sizing: border-box; height: 36px;" />
                    </div>
                </div>
                
                <div style="display: flex; gap: 8px;">
                    <div style="flex: 1;">
                        <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">User (Tùy chọn):</label>
                        <input type="text" id="customUser" style="width: 100%; padding: 9px; border-radius: 8px; background: rgba(0,0,0,0.2); border: 1px solid var(--card-border); color: var(--text-color); font-size: 12.5px; box-sizing: border-box; height: 36px;" />
                    </div>
                    <div style="flex: 1;">
                        <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Pass (Tùy chọn):</label>
                        <input type="password" id="customPass" style="width: 100%; padding: 9px; border-radius: 8px; background: rgba(0,0,0,0.2); border: 1px solid var(--card-border); color: var(--text-color); font-size: 12.5px; box-sizing: border-box; height: 36px;" />
                    </div>
                </div>
            </div>
            
            <button class="btn" onclick="saveConfig()">Gán Proxy & Kết nối</button>
        </div>
        
        <div class="loading-overlay" id="loading">
            <div class="spinner"></div>
            <div style="font-weight: 500; font-size: 13px;">Đang thiết lập định tuyến...</div>
        </div>
    </body>
    </html>
    '''
    return html_content


# ─── Root Page & Captive Redirect Guard ──────────────────

from fastapi.responses import JSONResponse, FileResponse, HTMLResponse

@app.get("/{full_path:path}")
def serve_frontend(full_path: str):
    # Trước đây route này tự redirect sang /captive (giao diện HTML/JS cũ, không phải
    # React) bất cứ khi nào Host header không khớp đúng lan_gateway_ip — dùng cho tính
    # năng "welcome popup" đã bị TẮT theo yêu cầu (xem singbox_manager.py, đã xoá DNS
    # hijack). Nhưng redirect này KHÔNG phụ thuộc DNS hijack — nó tự kích hoạt bất cứ
    # khi nào Host header không khớp lan_ip (vd config lỗi tạm thời, hoặc truy cập qua
    # tên/IP khác lan_gateway_ip) khiến giao diện React bị thay bằng trang cũ mà không
    # có lỗi rõ ràng nào. Xoá hẳn nhánh redirect này — route /captive vẫn còn, chỉ
    # không tự động điều hướng tới nữa.
    if full_path.startswith("api/") or full_path.startswith("ws/") or full_path == "status":
        return JSONResponse({"detail": "Not Found"}, status_code=404)
        
    file_path = os.path.join(STATIC_DIR, full_path)
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return HTMLResponse("<h1>Frontend not built</h1>")
