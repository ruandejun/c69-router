"""
GenRouter v2.0 — Settings Routes
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
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
    auto_assign_new_devices: bool = False
    auto_assign_mode: str = "balance"
    block_direct_devices: bool = False
    direct_whitelist: List[str] = []
    bypass_cidrs: List[str] = []


@router.get("")
def get_settings(config=Depends(get_config)):
    return config.model_dump()


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
    config.auto_assign_new_devices = payload.auto_assign_new_devices
    config.auto_assign_mode = payload.auto_assign_mode
    config.block_direct_devices = payload.block_direct_devices
    config.direct_whitelist = payload.direct_whitelist
    config.bypass_cidrs = payload.bypass_cidrs

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
