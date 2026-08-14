"""
GenRouter v2.0 — PAC File Routes

Serves proxy auto-config (PAC) files for devices.
"""

import os
from fastapi import APIRouter, Request, Response, Depends
from typing import Optional
import logging

from app.dependencies import get_config, get_mac_registry

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/proxy.pac")
@router.get("/wpad.dat")
def get_proxy_pac(
    request: Request,
    ip: Optional[str] = None,
    config=Depends(get_config),
    mac_registry=Depends(get_mac_registry),
):
    client_ip = ip or request.client.host

    # Lookup device by IP
    device = mac_registry.get_device_by_ip(client_ip) if mac_registry else None

    # Gateway IP from Host header
    active_port = os.environ.get("GENROUTER_ACTIVE_PORT", "9000")
    host_header = request.headers.get("host", f"{config.lan_gateway_ip}:{active_port}")
    gateway_ip = host_header.split(":")[0]

    # Build PAC content — khi thiết bị có proxy, dùng sing-box TUN gateway
    # Sing-box lắng nghe tại gateway IP (LAN side), routing theo source IP qua proxy
    if device and device.proxy_id:
        # Thiết bị có proxy → dùng DIRECT để traffic đi qua TUN của sing-box
        # Sing-box sẽ route traffic từ IP này qua đúng proxy (via source_ip_cidr rules)
        pac_content = f"""function FindProxyForURL(url, host) {{
    // Skip local addresses
    if (isPlainHostName(host) ||
        shExpMatch(host, "localhost") ||
        shExpMatch(host, "127.*") ||
        isInNet(host, "10.0.0.0", "255.0.0.0") ||
        isInNet(host, "172.16.0.0", "255.240.0.0") ||
        isInNet(host, "192.168.0.0", "255.255.0.0")) {{
        return "DIRECT";
    }}
    // All other traffic goes through gateway (sing-box TUN handles routing)
    return "DIRECT";
}}"""
        logger.debug(f"[PAC] Device {client_ip} has proxy {device.proxy_id} → routing via TUN")
    else:
        # Không có proxy → direct
        pac_content = """function FindProxyForURL(url, host) {
    return "DIRECT";
}"""
        logger.debug(f"[PAC] Device {client_ip} → DIRECT (no proxy)")

    return Response(
        content=pac_content,
        media_type="application/x-ns-proxy-autoconfig",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
