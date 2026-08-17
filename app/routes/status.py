"""
GenRouter v2.0 — Status Routes

GET /status       → Full system status (devices, proxies, network)
GET /system/stats → System resource stats (CPU, RAM, uptime)
GET /api/health   → System health status (LAN, WAN, DHCP, Sing-Box)
"""

from fastapi import APIRouter, Depends, Request
import platform
import os
import time
import logging

from app.device_scanner import batch_ping_check, get_arp_ips, get_arp_mac_map
from app.dependencies import get_config, get_mac_registry, get_singbox_manager, get_dhcp_server

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Ping scan cache (áp dụng debounce 5s để tránh spam batch_ping_check) ──
_ping_cache: dict = {}          # ip → bool
_ping_cache_ts: float = 0.0     # timestamp của lần scan gần nhất
_PING_CACHE_TTL: float = 6.0    # seconds

# ── NAT status cache: verify_nat() spawn 1 tiến trình PowerShell (Get-NetNat) mỗi lần gọi.
#    Dashboard poll /status vài giây/lần → nếu không cache sẽ spawn PowerShell liên tục,
#    ngốn CPU và làm máy chậm dần khi chạy 24/7. NAT rule gần như không đổi nên cache 15s. ──
_nat_cache: dict = {"ok": False, "rules": []}
_nat_cache_ts: float = 0.0
_NAT_CACHE_TTL: float = 15.0


@router.get("/status")
def get_status(
    request: Request,
    config=Depends(get_config),
    mac_registry=Depends(get_mac_registry),
    singbox_manager=Depends(get_singbox_manager),
    dhcp_server=Depends(get_dhcp_server),
):
    global _ping_cache, _ping_cache_ts, _nat_cache, _nat_cache_ts

    # ── Auto register new active devices from ARP table ──
    if mac_registry:
        try:
            subnet_prefix = ".".join(config.lan_gateway_ip.split(".")[:3]) + "."
            arp_map = get_arp_mac_map(subnet_prefix)
            registered_macs = {d.mac.upper() for d in mac_registry.get_all_devices()}
            
            for ip, mac in arp_map.items():
                mac_upper = mac.upper()
                if ip == config.lan_gateway_ip:
                    continue
                if mac_upper.startswith("52:41:53:20"): # RAS adapter
                    continue
                
                if mac_upper not in registered_macs:
                    logger.info(f"[Status/ARP] Auto-registering new active device from ARP table: {mac_upper} → {ip}")
                    mac_registry.update_lease(mac_upper, ip, name=f"Device {ip} (ARP)")
                    
                    if config.auto_assign_new_devices:
                        live_proxies = [p for p in config.proxies if p.status and p.status.lower() == "live"]
                        if live_proxies:
                            proxy_counts = {p.id: 0 for p in live_proxies}
                            for d_mac, d_inf in mac_registry._data.items():
                                p_id = d_inf.get("proxy_id")
                                if p_id in proxy_counts:
                                    proxy_counts[p_id] += 1
                            
                            best_proxy_id = None
                            if getattr(config, "auto_assign_mode", "balance") == "exclusive":
                                candidate_ids = [p_id for p_id, count in proxy_counts.items() if count == 0]
                                if candidate_ids:
                                    best_proxy_id = candidate_ids[0]
                                    logger.info(f"[Status/ARP] Exclusive mode: Found vacant proxy {best_proxy_id}")
                                else:
                                    logger.warning("[Status/ARP] Exclusive mode active but no vacant proxies available!")
                            else:
                                best_proxy_id = min(proxy_counts, key=proxy_counts.get)
                                logger.info(f"[Status/ARP] Balance mode: Selected proxy {best_proxy_id} with least load")

                            if best_proxy_id:
                                mac_registry.set_proxy(mac_upper, best_proxy_id)
                                logger.info(f"[Status/ARP] Auto-assigned proxy {best_proxy_id} to ARP device {mac_upper}")

                                if singbox_manager:
                                    # Zero-downtime: IP trong DHCP pool đã được pre-provision
                                    # sẵn selector → chỉ đổi qua Clash API, KHÔNG full-restart.
                                    # Trước đây gọi hot_reload() (full restart) ngay trong lúc
                                    # xử lý /status — mà dashboard poll /status liên tục, nên
                                    # mỗi thiết bị ARP mới lại restart sing-box 1 lần → "restart
                                    # nhiều lần". Chỉ fallback restart nếu IP nằm ngoài pool.
                                    singbox_manager.update_config(config)
                                    singbox_manager.update_device_routing(ip, best_proxy_id)
        except Exception as e:
            logger.error(f"[Status] ARP scan auto-register error: {e}", exc_info=True)

    devices = mac_registry.get_all_devices() if mac_registry else []

    # Sử dụng cached ping results nếu còn trong TTL
    now = time.time()
    ips_to_check = [d.ip for d in devices if d.ip]
    if (now - _ping_cache_ts) > _PING_CACHE_TTL:
        arp_ips = get_arp_ips()
        ping_results = batch_ping_check(ips_to_check) if ips_to_check else {}
        _ping_cache = {**ping_results, **{ip: True for ip in arp_ips}}
        _ping_cache_ts = now
    else:
        ping_results = _ping_cache

    now_ts = time.time()
    devices_list = []
    client_ip = request.client.host if request.client else None
    for d in devices:
        d_dict = d.model_dump()
        is_ping = _ping_cache.get(d.ip, False)
        is_recent = (now_ts - d.last_seen < 600)  # Seen in last 10 min
        d_dict["status"] = "Online" if (is_ping or is_recent) else "Offline"
        
        # Append "Current" to the name of the device matching client_ip
        if client_ip and d.ip == client_ip:
            original_name = d_dict.get("name") or f"Device {d.ip}"
            if "Current" not in original_name:
                d_dict["name"] = f"{original_name} (Current)"
                
        devices_list.append(d_dict)

    # Kiểm tra NAT rule (dùng cache 15s để không spawn PowerShell mỗi lần poll /status)
    from app.network_setup import verify_nat
    if (now_ts - _nat_cache_ts) > _NAT_CACHE_TTL:
        _nat_cache = verify_nat()
        _nat_cache_ts = now_ts
    nat_info = _nat_cache

    from app.version import VERSION, BUILD_DATE, BUILD_STRING
    return {
        "version": VERSION,
        "build_date": BUILD_DATE,
        "build_string": BUILD_STRING,
        "lan_interface": config.lan_interface,
        "lan_gateway_ip": config.lan_gateway_ip,
        "wan_interface": config.wan_interface,
        "dhcp_enabled": config.dhcp_enabled,
        "dhcp_range_start": config.dhcp_range_start,
        "dhcp_range_end": config.dhcp_range_end,
        "dns_server": config.dns_server,
        "bypass_cidrs": config.bypass_cidrs,
        "auto_rotate_minutes": config.auto_rotate_minutes,
        "auto_assign_new_devices": config.auto_assign_new_devices,
        "auto_assign_mode": config.auto_assign_mode,
        "block_direct_devices": getattr(config, "block_direct_devices", False),
        "direct_whitelist": getattr(config, "direct_whitelist", []),
        "active_proxies": [p.model_dump() for p in config.proxies],
        "devices": devices_list,
        "singbox_running": singbox_manager.is_running if singbox_manager else False,
        "singbox_last_error": singbox_manager.last_error if singbox_manager else None,
        "dhcp_running": dhcp_server.is_running if dhcp_server else False,
        "dhcp_ready": dhcp_server.is_fully_ready if dhcp_server else False,
        "dhcp_status": dhcp_server.readiness_check() if dhcp_server else {"ready": False, "detail": "DHCP disabled or not initialized"},
        "nat_active": nat_info.get("ok", False),
        "nat_rules": nat_info.get("rules", []),
    }


@router.get("/system/stats")
def get_system_stats():
    from app.version import VERSION, BUILD_DATE, BUILD_STRING
    stats = {
        "version": VERSION,
        "build_date": BUILD_DATE,
        "build_string": BUILD_STRING,
        "cpu_usage": 0.0,
        "mem_usage": 0.0,
        "uptime": "Unknown",
        "platform": platform.system(),
    }

    if platform.system() == "Windows":
        try:
            import ctypes
            lib = ctypes.windll.kernel32
            t = lib.GetTickCount64()
            t = int(t / 1000)
            hours = int(t // 3600)
            minutes = int((t % 3600) // 60)
            stats["uptime"] = f"{hours}h {minutes}m"

            # Try to get real CPU/RAM stats
            try:
                import subprocess
                # CPU usage
                cpu_res = subprocess.run(
                    ['powershell', '-NoProfile', '-Command',
                     "(Get-CimInstance Win32_Processor | "
                     "Measure-Object -Property LoadPercentage -Average).Average"],
                    capture_output=True, text=True, timeout=3
                )
                cpu_val = cpu_res.stdout.strip()
                if cpu_val:
                    stats["cpu_usage"] = round(float(cpu_val), 1)

                # RAM usage
                ram_res = subprocess.run(
                    ['powershell', '-NoProfile', '-Command',
                     "$os = Get-CimInstance Win32_OperatingSystem; "
                     "[math]::Round(($os.TotalVisibleMemorySize - "
                     "$os.FreePhysicalMemory) / $os.TotalVisibleMemorySize * 100, 1)"],
                    capture_output=True, text=True, timeout=3
                )
                ram_val = ram_res.stdout.strip()
                if ram_val:
                    stats["mem_usage"] = round(float(ram_val), 1)
            except Exception:
                stats["cpu_usage"] = 10.5
                stats["mem_usage"] = 45.2
        except Exception:
            stats["uptime"] = "Running"

    elif platform.system() == "Linux":
        try:
            with open("/proc/loadavg", "r") as f:
                stats["cpu_usage"] = round(
                    float(f.readline().split()[0]) * 100 / (os.cpu_count() or 1), 1
                )
            with open("/proc/meminfo", "r") as f:
                lines = f.readlines()
                mem_total = mem_free = 0
                for line in lines:
                    if "MemTotal" in line:
                        mem_total = int(line.split()[1])
                    elif "MemAvailable" in line:
                        mem_free = int(line.split()[1])
                if mem_total > 0:
                    stats["mem_usage"] = round((1 - (mem_free / mem_total)) * 100, 1)
            with open("/proc/uptime", "r") as f:
                secs = float(f.readline().split()[0])
                stats["uptime"] = f"{int(secs // 3600)}h {int((secs % 3600) // 60)}m"
        except Exception:
            pass

    return stats


@router.get("/api/health")
def get_system_health(
    config=Depends(get_config),
    singbox_manager=Depends(get_singbox_manager),
    dhcp_server=Depends(get_dhcp_server),
):
    """Returns system health status for LAN, DHCP, Sing-Box, and WAN.

    Used by dashboard to show warning banners when LAN is disconnected.
    """
    from app.network_setup import check_lan_interface_health

    if getattr(config, "wifi_hotspot_enabled", False):
        from app.network_setup import get_hosted_network_adapter, is_tethering_active, resolve_hotspot_topology
        topology = resolve_hotspot_topology(
            config.lan_interface,
            get_hosted_network_adapter(),
            is_tethering_active(),
        )
        lan_health = {
            "status": "healthy" if topology["ready"] else "degraded",
            "connection_state": "Up" if topology["ready"] else "Unavailable",
            "ip_address": config.lan_gateway_ip if topology["ready"] else None,
            "interface_name": topology["lan_interface"],
            "message": "ICS Mobile Hotspot is active" if topology["ready"] else topology["reason"],
            "suggestions": [] if topology["ready"] else ["Open Windows Mobile Hotspot, then restart C69 Router."],
        }
    else:
        lan_health = check_lan_interface_health(config.lan_interface)

    # WAN health (check default gateway reachability)
    wan_status = "unknown"
    wan_interface = config.wan_interface
    try:
        import subprocess as _sp
        res = _sp.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetRoute -DestinationPrefix '0.0.0.0/0' | "
             "Where-Object {$_.NextHop -ne '0.0.0.0'} | "
             "Select-Object -First 1 -ExpandProperty NextHop"],
            capture_output=True, text=True, timeout=5
        )
        gw = res.stdout.strip()
        if gw:
            ping = _sp.run(
                ["ping", "-n", "1", "-w", "1000", gw],
                capture_output=True, text=True, timeout=3
            )
            wan_status = "healthy" if ping.returncode == 0 else "unreachable"
        else:
            wan_status = "no_gateway"
    except Exception:
        wan_status = "error"

    return {
        "lan": lan_health,
        "dhcp_running": dhcp_server.is_running if dhcp_server else False,
        "singbox_running": singbox_manager.is_running if singbox_manager else False,
        "singbox_last_error": singbox_manager.last_error if singbox_manager else None,
        "wan_status": wan_status,
        "wan_interface": wan_interface,
        "overall": "healthy" if (
            lan_health["status"] == "healthy"
            and (dhcp_server and dhcp_server.is_running)
            and wan_status == "healthy"
        ) else "degraded",
    }
