"""
GenRouter v2.0 — Settings Routes
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field, field_validator
from typing import List
import logging

from app.config import save_config
from app.dependencies import get_config, get_singbox_manager, get_dhcp_server
from app.network_setup import setup_interface_dns

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings")


class SettingsUpdatePayload(BaseModel):
    lan_interface: str
    lan_gateway_ip: str
    lan_subnet_mask: str = "255.255.255.0"
    wan_interface: str
    dhcp_enabled: bool = True
    dhcp_range_start: str
    dhcp_range_end: str
    dhcp_lease_time: int = 3600
    dns_server: str
    auto_rotate_minutes: int = 0
    default_device_rotate_minutes: int = 0
    auto_assign_new_devices: bool = False
    auto_assign_mode: str = "balance"
    block_direct_devices: bool = False
    direct_whitelist: List[str] = []
    bypass_cidrs: List[str] = []
    wifi_hotspot_enabled: bool = False
    wifi_hotspot_ssid: str = "C69-Router"
    wifi_hotspot_password: str | None = Field(default=None, min_length=8, max_length=63)

    @field_validator("wifi_hotspot_ssid")
    @classmethod
    def validate_hotspot_ssid(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= 32:
            raise ValueError("WiFi hotspot SSID must be 1-32 characters")
        return value


@router.get("")
def get_settings(config=Depends(get_config)):
    """Return non-secret settings for the management UI."""
    return config.model_dump(exclude={"wifi_hotspot_password"})


@router.post("/update")
def update_settings(
    payload: SettingsUpdatePayload,
    config=Depends(get_config),
    singbox_manager=Depends(get_singbox_manager),
    dhcp_server=Depends(get_dhcp_server),
):
    config.lan_interface = payload.lan_interface
    config.lan_gateway_ip = payload.lan_gateway_ip
    config.lan_subnet_mask = payload.lan_subnet_mask
    config.wan_interface = payload.wan_interface
    config.dhcp_enabled = payload.dhcp_enabled
    config.dhcp_range_start = payload.dhcp_range_start
    config.dhcp_range_end = payload.dhcp_range_end
    config.dhcp_lease_time = payload.dhcp_lease_time
    config.dns_server = payload.dns_server
    config.auto_rotate_minutes = payload.auto_rotate_minutes
    config.default_device_rotate_minutes = payload.default_device_rotate_minutes
    config.auto_assign_new_devices = payload.auto_assign_new_devices
    config.auto_assign_mode = payload.auto_assign_mode
    config.block_direct_devices = payload.block_direct_devices
    config.direct_whitelist = payload.direct_whitelist
    config.bypass_cidrs = payload.bypass_cidrs
    config.wifi_hotspot_enabled = payload.wifi_hotspot_enabled
    config.wifi_hotspot_ssid = payload.wifi_hotspot_ssid
    if payload.wifi_hotspot_password is not None:
        config.wifi_hotspot_password = payload.wifi_hotspot_password

    save_config(config)

    # Update DHCP server config
    if dhcp_server and dhcp_server.is_running:
        dhcp_server.update_config(
            server_ip=config.lan_gateway_ip,
            subnet_mask=config.lan_subnet_mask,
            # Xem giải thích trong main.py: gateway LAN on-link nên không thể bị hijack-dns bắt được.
            dns_server=config.dns_server,
            pool_start=config.dhcp_range_start,
            pool_end=config.dhcp_range_end,
            lease_time=config.dhcp_lease_time,
            interface_name=config.lan_interface,
        )

    # Re-apply routing
    try:
        if singbox_manager:
            singbox_manager.update_config(config)
            singbox_manager.hot_reload()
    except Exception as e:
        logger.error(f"[Settings] Failed to update routing: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Settings saved but routing update failed: {e}",
        )

    # Dong bo DNS tren LAN + TUN adapters
    try:
        dns = config.dns_server or "1.1.1.1"
        secondary = "1.0.0.1" if dns == "1.1.1.1" else "8.8.4.4"
        setup_interface_dns(
            lan_interface=config.lan_interface,
            tun_interface=config.tun_interface,
            primary_dns=dns,
            secondary_dns=secondary,
        )
    except Exception as e:
        logger.warning(f"[Settings] DNS sync warning: {e}")

    return {"status": "success", "message": "Settings updated"}


@router.post("/restart-singbox")
def restart_singbox(
    singbox_manager=Depends(get_singbox_manager),
):
    """Restart sing-box để apply config thay đổi (hot-reload)."""
    if not singbox_manager:
        raise HTTPException(status_code=503, detail="Sing-box manager not available")
    try:
        singbox_manager.hot_reload()
        return {"status": "success", "message": "Sing-box restart scheduled (debounced 1s)"}
    except Exception as e:
        logger.error(f"[Settings] Failed to restart singbox: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/singbox-log")
def get_singbox_log(lines: int = 50):
    """Lấy N dòng cuối của sing-box log."""
    import os
    from app.config import PROJECT_DIR
    log_path = os.path.join(PROJECT_DIR, "sing-box.log")
    if not os.path.exists(log_path):
        return {"log": "", "lines": 0}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:]
        return {"log": "".join(tail), "lines": len(tail)}
    except Exception as e:
        return {"log": f"Error reading log: {e}", "lines": 0}


@router.post("/sync-dns")
def sync_dns_interfaces(config=Depends(get_config)):
    """Set DNS dong bo tren LAN va TUN interface.

    Goi khi: doi DNS Server trong Settings, sau khi restart sing-box,
    hoac sau khi doi interface.
    """
    try:
        dns = config.dns_server or "1.1.1.1"
        secondary = "1.0.0.1" if dns == "1.1.1.1" else "8.8.4.4"
        ok = setup_interface_dns(
            lan_interface=config.lan_interface,
            tun_interface=config.tun_interface,
            primary_dns=dns,
            secondary_dns=secondary,
        )
        return {
            "status": "success" if ok else "partial",
            "message": f"DNS {dns} / {secondary} set on {config.lan_interface} + {config.tun_interface}",
            "lan": config.lan_interface,
            "tun": config.tun_interface,
            "primary_dns": dns,
            "secondary_dns": secondary,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/hotspot-check")
def check_hotspot(config=Depends(get_config)):
    """Kiểm tra WiFi Hosted Network: driver có hỗ trợ không, trạng thái hiện tại.

    Dùng để UI hiển thị trước khi user bật wifi_hotspot_enabled.
    """
    import subprocess
    from app.network_setup import check_hosted_network_supported, get_hosted_network_adapter

    hw_check = check_hosted_network_supported()

    # Kiểm tra trạng thái hotspot hiện tại (đang chạy hay không)
    hotspot_running = False
    hotspot_ssid = ""
    try:
        r = subprocess.run(
            ["netsh", "wlan", "show", "hostednetwork"],
            capture_output=True, text=True, timeout=5
        )
        out = r.stdout.lower()
        hotspot_running = "started" in out or "đã bắt đầu" in out
        # Lấy SSID
        for line in r.stdout.splitlines():
            if "ssid" in line.lower() and ":" in line:
                hotspot_ssid = line.split(":", 1)[-1].strip()
                break
    except Exception:
        pass

    hotspot_adapter = get_hosted_network_adapter() if hotspot_running else None

    return {
        "driver_supported": hw_check["supported"],
        "driver_reason": hw_check["reason"],
        "hotspot_running": hotspot_running,
        "hotspot_ssid": hotspot_ssid,
        "hotspot_adapter": hotspot_adapter,
        "wifi_hotspot_enabled": getattr(config, "wifi_hotspot_enabled", False),
        "wifi_hotspot_ssid": getattr(config, "wifi_hotspot_ssid", "C69-Router"),
    }


@router.post("/hotspot-restart")
def restart_hotspot(config=Depends(get_config)):
    """Restart WiFi hotspot thủ công (dùng khi hotspot bị tắt/lỗi sau khi ngủ màn hình)."""
    from app.network_setup import setup_hosted_network, get_hosted_network_adapter

    if not getattr(config, "wifi_hotspot_enabled", False):
        raise HTTPException(status_code=400, detail="wifi_hotspot_enabled=False trong config. Bật trước trong Settings.")

    ssid = getattr(config, "wifi_hotspot_ssid", "C69-Router") or "C69-Router"
    pwd  = getattr(config, "wifi_hotspot_password", "c69router123") or "c69router123"

    ok = setup_hosted_network(ssid=ssid, password=pwd)
    if not ok:
        raise HTTPException(status_code=500, detail="Không thể khởi động hotspot. Kiểm tra driver WiFi.")

    import time
    adapter = None
    for _ in range(10):
        adapter = get_hosted_network_adapter()
        if adapter:
            break
        time.sleep(0.5)

    return {
        "status": "success" if adapter else "warning",
        "ssid": ssid,
        "adapter": adapter,
        "message": (
            f"Hotspot '{ssid}' đã khởi động. Virtual adapter: {adapter}."
            if adapter else
            "Hotspot khởi động nhưng không tìm được virtual adapter."
        ),
    }
