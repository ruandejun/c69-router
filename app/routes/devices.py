"""
GenRouter v2.0 — Device Routes

Endpoints for device management (CRUD, assign proxy, bulk operations).
All device operations use MAC as primary key.
"""

from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from typing import List, Optional
import platform
import logging

from app.config import save_config
from app.dependencies import get_config, get_mac_registry, get_singbox_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/devices")


class AssignProxyPayload(BaseModel):
    mac: str                           # MAC address (primary key)
    proxy_id: Optional[str] = None     # None = reset to direct


class BulkAssignPayload(BaseModel):
    macs: List[str]
    proxy_id: Optional[str] = None


class BulkAssignRawPayload(BaseModel):
    macs: List[str]
    proxy_raw_list: List[str]


class RenameDevicePayload(BaseModel):
    mac: str
    name: str


@router.get("")
def list_devices(mac_registry=Depends(get_mac_registry)):
    """List all devices from MAC registry."""
    devices = mac_registry.get_all_devices() if mac_registry else []
    return {"devices": [d.model_dump() for d in devices]}


@router.post("/assign")
def assign_proxy(
    payload: AssignProxyPayload,
    config=Depends(get_config),
    mac_registry=Depends(get_mac_registry),
    singbox_manager=Depends(get_singbox_manager),
):
    """Assign or unassign proxy for a device (by MAC)."""
    mac = payload.mac.upper()
    device = mac_registry.get_device_by_mac(mac) if mac_registry else None

    if not device:
        raise HTTPException(status_code=404, detail=f"Device {mac} not found")

    if payload.proxy_id:
        proxy = next((p for p in config.proxies if p.id == payload.proxy_id), None)
        if not proxy:
            raise HTTPException(status_code=404, detail="Proxy not found")

    mac_registry.set_proxy(mac, payload.proxy_id)

    # Áp dụng routing NGAY, đồng bộ — trước đây chỉ gọi hot_reload() (hẹn giờ debounce
    # 1s chạy nền) rồi trả "success" ngay lập tức mà không chờ/biết kết quả thật, nên
    # nếu áp dụng thất bại sau đó, client vẫn thấy "success" dù thiết bị chưa đi qua
    # proxy. reload_now() chờ full restart sing-box thật xong mới trả về.
    action = f"assigned to {payload.proxy_id}" if payload.proxy_id else "reset to direct"
    reload_ok = True
    try:
        if singbox_manager:
            reload_ok = singbox_manager.update_device_routing(device.ip, payload.proxy_id)
    except Exception as e:
        reload_ok = False
        logger.error(f"[Devices] Routing update error: {e}")

    if not reload_ok:
        err = getattr(singbox_manager, "last_error", None) or "Không xác định được lỗi"
        raise HTTPException(
            status_code=502,
            detail=f"Đã lưu gán proxy nhưng áp dụng routing thất bại ({err}). "
                   f"Thiết bị {mac} CHƯA được chuyển qua proxy — vui lòng thử lại."
        )

    return {"status": "success", "message": f"Device {mac} {action}"}


@router.post("/bulk-assign")
def bulk_assign(
    payload: BulkAssignPayload,
    config=Depends(get_config),
    mac_registry=Depends(get_mac_registry),
    singbox_manager=Depends(get_singbox_manager),
):
    """Bulk assign same proxy to multiple devices."""
    if payload.proxy_id:
        proxy = next((p for p in config.proxies if p.id == payload.proxy_id), None)
        if not proxy:
            raise HTTPException(status_code=404, detail="Proxy not found")

    success = []
    failed = []
    for mac in payload.macs:
        mac = mac.upper()
        device = mac_registry.get_device_by_mac(mac) if mac_registry else None
        if device:
            mac_registry.set_proxy(mac, payload.proxy_id)
            success.append(mac)
        else:
            failed.append(mac)

    # Single reload for all changes — đồng bộ, xem giải thích ở assign_proxy() phía trên.
    # Raise HTTPException (thay vì trả 200 kèm status="error") vì frontend chỉ check
    # res.ok, không đọc field "status" trong body — trả 200 giả sẽ khiến lỗi bị bỏ qua.
    reload_ok = True
    if success and singbox_manager:
        try:
            assignments_to_apply = []
            for m in success:
                d = mac_registry.get_device_by_mac(m)
                if d and d.ip:
                    assignments_to_apply.append((d.ip, payload.proxy_id))
            reload_ok = singbox_manager.update_multiple_devices_routing(assignments_to_apply)
        except Exception as e:
            reload_ok = False
            logger.error(f"[Devices] Bulk routing update error: {e}")

    if not reload_ok:
        err = getattr(singbox_manager, "last_error", None) or "Không xác định được lỗi"
        raise HTTPException(
            status_code=502,
            detail=f"Đã lưu gán proxy cho {len(success)} thiết bị nhưng áp dụng routing thất bại "
                   f"({err}). Vui lòng thử lại."
        )

    return {
        "status": "success",
        "assigned_count": len(success),
        "failed_count": len(failed),
        "failed": failed,
    }


@router.post("/bulk-assign-raw")
def bulk_assign_raw(
    payload: BulkAssignRawPayload,
    config=Depends(get_config),
    mac_registry=Depends(get_mac_registry),
    singbox_manager=Depends(get_singbox_manager),
):
    """Assign different raw proxy strings to each device."""
    from app.utils import parse_raw_proxy_string
    from app.config import ProxyConfig

    assigned_count = 0

    for i, mac in enumerate(payload.macs):
        if i >= len(payload.proxy_raw_list):
            break

        raw = payload.proxy_raw_list[i].strip()
        if not raw:
            continue

        mac = mac.upper()
        parsed = parse_raw_proxy_string(raw)
        if not parsed["host"]:
            continue

        # Find or create proxy
        proxy_id = f"proxy_{parsed['host'].replace('.', '_')}_{parsed['port']}"
        existing = next((p for p in config.proxies if p.id == proxy_id), None)

        if not existing:
            new_proxy = ProxyConfig(
                id=proxy_id,
                type=parsed["type"],
                host=parsed["host"],
                port=parsed["port"],
                username=parsed["username"],
                password=parsed["password"],
            )
            config.proxies.append(new_proxy)

        if mac_registry:
            mac_registry.set_proxy(mac, proxy_id)
        assigned_count += 1

    save_config(config)

    # Single hot-reload
    if assigned_count > 0 and singbox_manager:
        try:
            singbox_manager.update_config(config)
            singbox_manager.hot_reload()
        except Exception as e:
            logger.error(f"[Devices] Bulk raw routing error: {e}")

    return {
        "status": "success",
        "message": f"Configured and assigned {assigned_count} proxies",
    }


@router.post("/remove/{mac}")
def remove_device(
    mac: str,
    mac_registry=Depends(get_mac_registry),
    singbox_manager=Depends(get_singbox_manager),
):
    """Remove a device from registry."""
    mac = mac.upper()
    device = mac_registry.get_device_by_mac(mac) if mac_registry else None
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    mac_registry.remove_device(mac)

    # Clean ARP cache on Windows
    if platform.system() == "Windows" and device.ip:
        import subprocess
        try:
            subprocess.run(
                f"arp -d {device.ip}", shell=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception:
            pass

    try:
        if singbox_manager:
            singbox_manager.hot_reload()
    except Exception as e:
        logger.error(f"[Devices] Routing update after remove: {e}")

    return {"status": "success", "message": f"Device {mac} removed"}


@router.post("/remove-offline")
def remove_offline(
    mac_registry=Depends(get_mac_registry),
    singbox_manager=Depends(get_singbox_manager),
):
    """Remove all offline devices."""
    from app.device_scanner import batch_ping_check, get_arp_ips
    import time

    if not mac_registry:
        return {"status": "success", "removed_count": 0}

    devices = mac_registry.get_all_devices()
    arp_ips = get_arp_ips()
    now = time.time()

    ips_to_check = [d.ip for d in devices if d.ip]
    ping_results = batch_ping_check(ips_to_check) if ips_to_check else {}

    offline_macs = []
    for d in devices:
        is_ping = ping_results.get(d.ip, False)
        is_arp = d.ip in arp_ips
        is_recent = (now - d.last_seen < 600)

        if platform.system() == "Windows":
            is_online = is_ping or is_recent
        else:
            is_online = is_ping or is_arp or is_recent

        if not is_online:
            offline_macs.append(d.mac)

    if offline_macs:
        mac_registry.remove_devices_by_macs(offline_macs)
        try:
            if singbox_manager:
                singbox_manager.hot_reload()
        except Exception as e:
            logger.error(f"[Devices] Routing update after bulk remove: {e}")

    return {
        "status": "success",
        "message": f"Removed {len(offline_macs)} offline devices",
        "removed_count": len(offline_macs),
    }


@router.post("/clear-stale-ips")
def clear_stale_ips(
    config=Depends(get_config),
    mac_registry=Depends(get_mac_registry),
):
    """Gỡ IP (giữ nguyên proxy_id/name) của các thiết bị đã offline quá lâu (>2x lease_time)
    để dọn nhẹ DHCP pool ngay lập tức. KHÔNG xoá thiết bị hay proxy binding — chỉ giải
    phóng slot IP; thiết bị sẽ được cấp IP mới khi kết nối lại. Khác với /remove-offline
    (xoá hẳn thiết bị khỏi danh sách)."""
    if not mac_registry:
        return {"status": "success", "cleared_count": 0, "cleared_macs": []}

    cleared_macs = mac_registry.clear_stale_ips(
        config.dhcp_range_start, config.dhcp_range_end, config.dhcp_lease_time
    )
    return {
        "status": "success",
        "message": f"Đã dọn IP của {len(cleared_macs)} thiết bị offline lâu.",
        "cleared_count": len(cleared_macs),
        "cleared_macs": cleared_macs,
    }


@router.post("/rename")
def rename_device(payload: RenameDevicePayload, mac_registry=Depends(get_mac_registry)):
    """Rename a device."""
    mac = payload.mac.upper()
    device = mac_registry.get_device_by_mac(mac) if mac_registry else None
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    mac_registry.set_device_name(mac, payload.name)
    return {"status": "success", "message": f"Device renamed to {payload.name}"}


# ─── API tiện ích đổi/lấy proxy cho các tool nuôi account/farm ───────────

def _get_formatted_proxy(proxy, format_type="json"):
    if not proxy:
        if format_type in ("raw", "raw_url"):
            return PlainTextResponse(content="DIRECT", status_code=200)
        return {"status": "success", "proxy": None, "proxy_str": "DIRECT"}

    raw_auth = f"{proxy.username}:{proxy.password}@" if proxy.username else ""
    raw = f"{proxy.type}://{raw_auth}{proxy.host}:{proxy.port}"
    
    simple = f"{proxy.host}:{proxy.port}"
    if proxy.username:
        simple += f":{proxy.username}:{proxy.password}"

    if format_type == "raw":
        return PlainTextResponse(content=simple, status_code=200)
    elif format_type == "raw_url":
        return PlainTextResponse(content=raw, status_code=200)
        
    return {
        "status": "success",
        "proxy_id": proxy.id,
        "proxy": {
            "type": proxy.type,
            "host": proxy.host,
            "port": proxy.port,
            "username": proxy.username,
            "password": proxy.password,
            "dns_server": proxy.dns_server,
            "status": proxy.status,
            "latency": proxy.latency,
            "raw": raw,
            "simple": simple
        },
        "proxy_str": simple
    }


@router.get("/my-proxy")
@router.get("/proxy")
def get_device_proxy(
    request: Request,
    ip: Optional[str] = None,
    mac: Optional[str] = None,
    format: Optional[str] = "json", # json, raw (host:port:user:pass), raw_url (socks5://...)
    config=Depends(get_config),
    mac_registry=Depends(get_mac_registry)
):
    """Lấy proxy hiện tại của thiết bị (dựa trên client IP hoặc IP/MAC truyền vào)."""
    if not mac_registry:
        raise HTTPException(status_code=503, detail="MAC Registry not available")

    target_mac = None

    if mac:
        target_mac = mac.upper()
    elif ip:
        target_mac = mac_registry._ip_to_mac.get(ip)
    else:
        # Tự động phát hiện IP của client gửi request
        client_ip = request.client.host
        target_mac = mac_registry._ip_to_mac.get(client_ip)
        if not target_mac:
            # Dự phòng: thử tìm IP trong danh sách devices xem có trùng không
            # (trong trường hợp reverse proxy / NAT làm sai lệch client host)
            for dev in mac_registry.get_all_devices():
                if dev.ip == client_ip:
                    target_mac = dev.mac
                    break

    if not target_mac:
        raise HTTPException(
            status_code=404, 
            detail=f"Device not found for IP: {ip or request.client.host} / MAC: {mac}"
        )

    device = mac_registry.get_device_by_mac(target_mac)
    if not device:
        raise HTTPException(status_code=404, detail=f"Device {target_mac} not found")

    proxy_id = device.proxy_id
    proxy = None
    if proxy_id:
        proxy = next((p for p in config.proxies if p.id == proxy_id), None)

    return _get_formatted_proxy(proxy, format)


@router.get("/rotate")
@router.post("/rotate")
def rotate_device_proxy(
    request: Request,
    ip: Optional[str] = None,
    mac: Optional[str] = None,
    format: Optional[str] = "json",
    config=Depends(get_config),
    mac_registry=Depends(get_mac_registry),
    singbox_manager=Depends(get_singbox_manager)
):
    """Xoay proxy ngẫu nhiên cho thiết bị (dựa trên client IP hoặc IP/MAC truyền vào)."""
    if not mac_registry:
        raise HTTPException(status_code=503, detail="MAC Registry not available")

    target_mac = None

    if mac:
        target_mac = mac.upper()
    elif ip:
        target_mac = mac_registry._ip_to_mac.get(ip)
    else:
        client_ip = request.client.host
        target_mac = mac_registry._ip_to_mac.get(client_ip)
        if not target_mac:
            for dev in mac_registry.get_all_devices():
                if dev.ip == client_ip:
                    target_mac = dev.mac
                    break

    if not target_mac:
        raise HTTPException(
            status_code=404, 
            detail=f"Device not found for IP: {ip or request.client.host} / MAC: {mac}"
        )

    device = mac_registry.get_device_by_mac(target_mac)
    if not device:
        raise HTTPException(status_code=404, detail=f"Device {target_mac} not found")

    if not config.proxies:
        raise HTTPException(status_code=400, detail="No proxies available in system config")

    # Lấy proxy hiện tại
    current_proxy_id = device.proxy_id

    # Lấy danh sách các proxy khác với proxy hiện tại
    import random
    available_proxies = [p for p in config.proxies if p.id != current_proxy_id]
    
    # Nếu không có proxy khác (chỉ có 1 proxy duy nhất trong hệ thống)
    if not available_proxies:
        new_proxy = next((p for p in config.proxies if p.id == current_proxy_id), config.proxies[0])
    else:
        # Ưu tiên các proxy chưa được dùng bởi các thiết bị khác
        all_devices = mac_registry.get_all_devices()
        used_proxy_ids = {
            d.proxy_id for d in all_devices
            if d.proxy_id and d.mac != target_mac
        }
        unused = [p for p in available_proxies if p.id not in used_proxy_ids]
        if unused:
            new_proxy = random.choice(unused)
        else:
            new_proxy = random.choice(available_proxies)

    # Set proxy mới cho device
    mac_registry.set_proxy(target_mac, new_proxy.id)

    # Zero-downtime: đổi qua Clash API (không kill/tạo lại TUN adapter) — trước đây gọi
    # reload_now() (full restart) cho MỖI lần rotate; vì rotate là thao tác "đổi proxy"
    # được gọi rất thường xuyên (tool farm rotate IP định kỳ) nên sing-box bị stop/start
    # liên tục. update_device_routing() tự fallback full restart nếu API thất bại.
    reload_ok = True
    try:
        if singbox_manager:
            singbox_manager.update_config(config)
            reload_ok = singbox_manager.update_device_routing(device.ip, new_proxy.id)
    except Exception as e:
        reload_ok = False
        logger.error(f"[Devices] Rotate proxy routing error: {e}")

    if not reload_ok:
        err = getattr(singbox_manager, "last_error", None) or "Không xác định được lỗi"
        raise HTTPException(
            status_code=502,
            detail=f"Đã đổi proxy nhưng áp dụng routing thất bại ({err}). "
                   f"Thiết bị {target_mac} CHƯA được chuyển qua proxy mới — vui lòng thử lại."
        )

    logger.info(f"[API Rotate] Rotated device {target_mac} ({device.ip}): {current_proxy_id} -> {new_proxy.id}")

    return _get_formatted_proxy(new_proxy, format)
