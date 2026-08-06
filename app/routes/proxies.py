"""
GenRouter v2.0 — Proxy Routes

Endpoints for proxy management (add, remove, bulk import, live/die check).
"""

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks, Request
from pydantic import BaseModel
from typing import List
import logging
import time

from app.config import ProxyConfig, save_config
from app.utils import parse_raw_proxy_string, bulk_test_proxies, check_ip_blacklist, test_single_proxy
from app.dependencies import get_config, get_mac_registry, get_singbox_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/proxies")

# ─── Background check-all state ────────────────────────────────
_check_job: dict = {"running": False, "started_at": 0, "results": None, "error": None}

# ─── DPN Assign Progress Tracking (per-MAC, poll để hiển thị progress bar UI) ────
DPN_MAX_RETRIES = 15
DPN_MAX_LATENCY_MS = 5000  # Loại proxy phản hồi chậm hơn ngưỡng này (đơn vị: ms)
_dpn_progress: dict = {}


def _dpn_set_progress(mac: str, **kwargs):
    entry = _dpn_progress.get(mac, {})
    entry.update(kwargs)
    entry["updated_at"] = time.time()
    _dpn_progress[mac] = entry


class BulkImportPayload(BaseModel):
    text: str
    dns_server: str = "1.1.1.1"   # DNS server áp dụng cho tất cả proxies trong batch này


class BulkAssign1to1Payload(BaseModel):
    """Gán mỗi thiết bị 1 proxy riêng theo thứ tự từ danh sách proxy.
    
    - macs: danh sách MAC address cần gán
    - proxy_ids: danh sách proxy_id (cùng thứ tự với macs)
      Nếu len(proxy_ids) < len(macs) → các device còn lại bị bỏ qua
    """
    macs: List[str]
    proxy_ids: List[str]


class BulkAssignRaw1to1Payload(BaseModel):
    """Gán mỗi thiết bị 1 proxy raw string riêng.
    
    - macs: danh sách MAC address
    - proxy_raw_list: danh sách proxy string (host:port:user:pass hoặc user:pass@host:port)
    """
    macs: List[str]
    proxy_raw_list: List[str]


@router.get("")
def list_proxies(config=Depends(get_config)):
    """Lấy danh sách toàn bộ proxy + số thiết bị đang dùng mỗi proxy."""
    mac_registry = None
    try:
        from app.dependencies import get_mac_registry
        # We'll count from mac_registry if available
    except Exception:
        pass

    proxies = []
    for p in config.proxies:
        pd = p.model_dump()
        pd["device_count"] = 0  # sẽ được tính nếu có registry
        proxies.append(pd)

    return {"proxies": proxies}


@router.get("/with-device-count")
def list_proxies_with_device_count(
    config=Depends(get_config),
    mac_registry=Depends(get_mac_registry),
):
    """Lấy danh sách proxy kèm số thiết bị đang sử dụng."""
    # Đếm số thiết bị cho từng proxy
    device_counts: dict = {}
    if mac_registry:
        for device in mac_registry.get_all_devices():
            if device.proxy_id:
                device_counts[device.proxy_id] = device_counts.get(device.proxy_id, 0) + 1

    proxies = []
    for p in config.proxies:
        pd = p.model_dump()
        pd["device_count"] = device_counts.get(p.id, 0)
        proxies.append(pd)

    return {"proxies": proxies}


@router.post("/add")
def add_proxy(
    proxy: ProxyConfig,
    config=Depends(get_config),
    singbox_manager=Depends(get_singbox_manager),
):
    if any(p.id == proxy.id for p in config.proxies):
        raise HTTPException(status_code=400, detail="Proxy ID already exists")

    config.proxies.append(proxy)
    save_config(config)

    try:
        if singbox_manager:
            singbox_manager.update_config(config)
            singbox_manager.hot_reload()
    except Exception as e:
        logger.error(f"[Proxies] Failed to apply after add: {e}")

    return {"status": "success", "message": f"Proxy {proxy.id} added"}


@router.post("/remove/{proxy_id}")
def remove_proxy(
    proxy_id: str,
    config=Depends(get_config),
    mac_registry=Depends(get_mac_registry),
    singbox_manager=Depends(get_singbox_manager),
):
    proxy = next((p for p in config.proxies if p.id == proxy_id), None)
    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")

    # Reset devices using this proxy
    if mac_registry:
        for device in mac_registry.get_all_devices():
            if device.proxy_id == proxy_id:
                mac_registry.set_proxy(device.mac, None)

    config.proxies = [p for p in config.proxies if p.id != proxy_id]
    save_config(config)

    try:
        if singbox_manager:
            singbox_manager.update_config(config)
            singbox_manager.hot_reload()
    except Exception as e:
        logger.error(f"[Proxies] Failed to apply after remove: {e}")

    return {"status": "success", "message": f"Proxy {proxy_id} removed"}


@router.post("/remove-die")
def remove_die_proxies(
    config=Depends(get_config),
    mac_registry=Depends(get_mac_registry),
    singbox_manager=Depends(get_singbox_manager),
):
    """Xóa tất cả các proxy có trạng thái là Die hoặc Error."""
    die_proxies = [p for p in config.proxies if p.status and p.status.lower() in ("die", "error")]
    if not die_proxies:
        return {"status": "success", "removed_count": 0, "message": "No die/error proxies found"}

    die_ids = {p.id for p in die_proxies}

    # Reset devices using these proxies
    if mac_registry:
        for device in mac_registry.get_all_devices():
            if device.proxy_id in die_ids:
                mac_registry.set_proxy(device.mac, None)

    config.proxies = [p for p in config.proxies if p.id not in die_ids]
    save_config(config)

    try:
        if singbox_manager:
            singbox_manager.update_config(config)
            singbox_manager.hot_reload()
    except Exception as e:
        logger.error(f"[Proxies] Failed to apply after bulk remove: {e}")

    return {
        "status": "success",
        "removed_count": len(die_ids),
        "message": f"Removed {len(die_ids)} die proxies"
    }


class UpdateProxyPayload(BaseModel):
    """Payload để cập nhật thông tin một proxy đã có."""
    type: str = "socks5"
    host: str
    port: int
    username: str = ""
    password: str = ""
    status: str = "Unknown"
    latency: int = -1
    dns_server: str = "1.1.1.1"    # DNS server riêng cho proxy này


@router.post("/update/{proxy_id}")
def update_proxy(
    proxy_id: str,
    payload: UpdateProxyPayload,
    config=Depends(get_config),
    singbox_manager=Depends(get_singbox_manager),
):
    """Cập nhật thông tin proxy (type, host, port, auth)."""
    proxy = next((p for p in config.proxies if p.id == proxy_id), None)
    if not proxy:
        raise HTTPException(status_code=404, detail=f"Proxy '{proxy_id}' not found")

    proxy.type = payload.type
    proxy.host = payload.host
    proxy.port = payload.port
    proxy.username = payload.username or ""
    proxy.password = payload.password or ""
    proxy.dns_server = payload.dns_server or "1.1.1.1"
    if payload.status not in ("Unknown", ""):
        proxy.status = payload.status
    if payload.latency >= 0:
        proxy.latency = payload.latency

    save_config(config)

    try:
        if singbox_manager:
            singbox_manager.update_config(config)
            singbox_manager.hot_reload()
    except Exception as e:
        logger.error(f"[Proxies] Failed to apply after update: {e}")

    return {"status": "success", "message": f"Proxy {proxy_id} updated"}




@router.post("/bulk-import")
def bulk_import(
    payload: BulkImportPayload,
    config=Depends(get_config),
    singbox_manager=Depends(get_singbox_manager),
):
    lines = payload.text.strip().split("\n")
    added_count = 0
    skipped_count = 0
    added_ids = []

    # Optional dns_server nếu caller muốn set ngay khi import
    dns_server = getattr(payload, 'dns_server', None) or '1.1.1.1'

    for line in lines:
        line = line.strip()
        if not line:
            continue

        parsed = parse_raw_proxy_string(line)
        if not parsed["host"]:
            skipped_count += 1
            continue

        proxy_id = f"proxy_{parsed['host'].replace('.', '_')}_{parsed['port']}"
        if any(p.id == proxy_id for p in config.proxies):
            skipped_count += 1
            continue

        new_proxy = ProxyConfig(
            id=proxy_id,
            type=parsed["type"],
            host=parsed["host"],
            port=parsed["port"],
            username=parsed["username"],
            password=parsed["password"],
            dns_server=dns_server,
        )
        config.proxies.append(new_proxy)
        added_count += 1
        added_ids.append(proxy_id)

    save_config(config)

    if added_count > 0 and singbox_manager:
        try:
            singbox_manager.update_config(config)
            singbox_manager.hot_reload()
        except Exception as e:
            logger.error(f"[Proxies] Failed to apply after bulk import: {e}")

    return {
        "status": "success",
        "message": f"Imported {added_count} proxies ({skipped_count} skipped/duplicate)",
        "added_count": added_count,
        "skipped_count": skipped_count,
        "proxy_ids": added_ids,
    }


@router.post("/bulk-assign-1to1")
def bulk_assign_1to1(
    payload: BulkAssign1to1Payload,
    config=Depends(get_config),
    mac_registry=Depends(get_mac_registry),
    singbox_manager=Depends(get_singbox_manager),
):
    """Gán mỗi thiết bị 1 proxy riêng (1 MAC : 1 proxy_id).
    
    Thứ tự: macs[0] → proxy_ids[0], macs[1] → proxy_ids[1], ...
    Nếu proxy_id không tồn tại trong config → bỏ qua thiết bị đó.
    """
    if not mac_registry:
        raise HTTPException(status_code=503, detail="MAC registry not available")

    # Validate proxy IDs
    valid_proxy_ids = {p.id for p in config.proxies}

    success = []
    failed = []
    skipped = []

    for i, mac in enumerate(payload.macs):
        if i >= len(payload.proxy_ids):
            break  # Không đủ proxy cho thiết bị này

        mac = mac.upper()
        proxy_id = payload.proxy_ids[i]

        if not proxy_id:
            # Gán về Direct
            device = mac_registry.get_device_by_mac(mac)
            if device:
                mac_registry.set_proxy(mac, None)
                success.append({"mac": mac, "proxy_id": None})
            else:
                failed.append(mac)
            continue

        if proxy_id not in valid_proxy_ids:
            logger.warning(f"[Proxies] bulk-assign-1to1: proxy_id '{proxy_id}' not found, skipping {mac}")
            skipped.append({"mac": mac, "proxy_id": proxy_id, "reason": "proxy_not_found"})
            continue

        device = mac_registry.get_device_by_mac(mac)
        if not device:
            failed.append(mac)
            continue

        mac_registry.set_proxy(mac, proxy_id)
        success.append({"mac": mac, "proxy_id": proxy_id})

    # Single hot-reload for all changes
    if success and singbox_manager:
        try:
            assignments_to_apply = []
            for item in success:
                d = mac_registry.get_device_by_mac(item["mac"])
                if d and d.ip:
                    assignments_to_apply.append((d.ip, item["proxy_id"]))
            singbox_manager.update_multiple_devices_routing(assignments_to_apply)
        except Exception as e:
            logger.error(f"[Proxies] bulk-assign-1to1 routing error: {e}")

    return {
        "status": "success",
        "assigned_count": len(success),
        "failed_count": len(failed),
        "skipped_count": len(skipped),
        "assignments": success,
        "failed": failed,
        "skipped": skipped,
    }


@router.post("/bulk-import-and-assign-1to1")
def bulk_import_and_assign_1to1(
    payload: BulkAssignRaw1to1Payload,
    config=Depends(get_config),
    mac_registry=Depends(get_mac_registry),
    singbox_manager=Depends(get_singbox_manager),
):
    """Import proxy mới nếu chưa có + gán 1 MAC : 1 proxy theo thứ tự.
    
    Đây là tính năng chính cho bulk workflow:
    - Dán danh sách MAC (1 dòng/MAC)
    - Dán danh sách proxy raw (1 dòng/proxy)  
    - Mỗi MAC được gán đúng proxy tương ứng theo vị trí
    """
    if not mac_registry:
        raise HTTPException(status_code=503, detail="MAC registry not available")

    added_proxies = 0
    assigned = []
    failed = []

    for i, mac in enumerate(payload.macs):
        if i >= len(payload.proxy_raw_list):
            break

        raw = payload.proxy_raw_list[i].strip()
        if not raw:
            continue

        mac = mac.upper()
        device = mac_registry.get_device_by_mac(mac)
        if not device:
            failed.append({"mac": mac, "reason": "device_not_found"})
            continue

        parsed = parse_raw_proxy_string(raw)
        if not parsed["host"]:
            failed.append({"mac": mac, "reason": "invalid_proxy_format", "raw": raw})
            continue

        proxy_id = f"proxy_{parsed['host'].replace('.', '_')}_{parsed['port']}"

        # Import proxy nếu chưa tồn tại
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
            added_proxies += 1

        mac_registry.set_proxy(mac, proxy_id)
        assigned.append({"mac": mac, "proxy_id": proxy_id})

    save_config(config)

    # Single hot-reload
    if assigned and singbox_manager:
        try:
            singbox_manager.update_config(config)
            singbox_manager.hot_reload()
        except Exception as e:
            logger.error(f"[Proxies] bulk-import-and-assign-1to1 error: {e}")

    return {
        "status": "success",
        "message": f"Imported {added_proxies} new proxies, assigned {len(assigned)} devices",
        "added_proxies": added_proxies,
        "assigned_count": len(assigned),
        "failed_count": len(failed),
        "assignments": assigned,
        "failed": failed,
    }


@router.post("/check-all")
def check_all(
    background_tasks: BackgroundTasks,
    config=Depends(get_config),
    singbox_manager=Depends(get_singbox_manager),
):
    """Kiểm tra trạng thái tất cả proxy (non-blocking).
    
    - Nếu không có job đang chạy → bắt đầu check và trả về ngay
    - Kết quả được cập nhật vào config sau khi hoàn tất
    - Frontend có thể poll /api/proxies/check-status để lấy kết quả
    """
    global _check_job

    if not config.proxies:
        return {"status": "success", "message": "No proxies to check", "results": []}

    # Nếu job cũ đã xong, trả kết quả ngay (tránh re-check spam)
    if _check_job["results"] is not None and not _check_job["running"]:
        elapsed = time.time() - _check_job["started_at"]
        if elapsed < 30:  # Kết quả còn mới trong 30s → trả ngay
            return {
                "status": "success",
                "message": f"Cached results ({len(_check_job['results'])} proxies)",
                "results": _check_job["results"],
                "cached": True,
            }

    if _check_job["running"]:
        return {
            "status": "running",
            "message": "Check job already in progress",
            "started_at": _check_job["started_at"],
        }

    # Bắt đầu job mới
    _check_job = {"running": True, "started_at": time.time(), "results": None, "error": None}
    
    # Cập nhật status của tất cả proxy thành "Checking" ngay lập tức để UI hiển thị tiến trình đang chạy
    for p in config.proxies:
        p.status = "Checking"
    save_config(config)

    proxy_infos = [p.model_dump() for p in config.proxies]

    def do_check():
        global _check_job
        try:
            # Tăng max_workers lên 100 để kiểm tra đa luồng (multithreading) cực nhanh
            results = bulk_test_proxies(proxy_infos, max_workers=min(len(proxy_infos), 100))
            _check_job["results"] = [
                {"id": r[0], "status": r[1], "latency": r[2]} for r in results
            ]

            # Cập nhật status vào config (load fresh từ disk để có state mới nhất)
            from app.config import load_config, save_config as _save
            app_config = load_config()
            for pid, status, latency in results:
                for p in app_config.proxies:
                    if p.id == pid:
                        p.status = status
                        p.latency = latency
            _save(app_config)

            # Áp dụng ngay: loại proxy "Die" khỏi routing (tránh device treo chờ timeout
            # tới server chết) và khôi phục routing cho proxy vừa sống lại.
            if singbox_manager:
                try:
                    singbox_manager.update_config(app_config)
                    singbox_manager.hot_reload()
                except Exception as e:
                    logger.error(f"[Proxies] check-all hot_reload error: {e}")
        except Exception as e:
            _check_job["error"] = str(e)
            logger.error(f"[Proxies] check-all background error: {e}")
        finally:
            _check_job["running"] = False

    background_tasks.add_task(do_check)

    return {
        "status": "started",
        "message": f"Checking {len(proxy_infos)} proxies in background...",
        "proxy_count": len(proxy_infos),
    }


@router.get("/check-status")
def check_status():
    """Lấy trạng thái job check-all đang chạy."""
    if _check_job["running"]:
        elapsed = time.time() - _check_job["started_at"]
        return {
            "status": "running",
            "elapsed_seconds": round(elapsed, 1),
        }
    elif _check_job["results"] is not None:
        return {
            "status": "done",
            "results": _check_job["results"],
            "error": _check_job.get("error"),
        }
    else:
        return {"status": "idle"}


@router.post("/rotate/{mac}")
def rotate_proxy_for_device(
    mac: str,
    config=Depends(get_config),
    mac_registry=Depends(get_mac_registry),
    singbox_manager=Depends(get_singbox_manager),
):
    """Đổi proxy ngẫu nhiên cho một device (chống fingerprint correlation).

    Chọn ngẫu nhiên proxy khác với proxy hiện tại của device.
    Ưu tiên proxy chưa bị gán cho device nào (giảm cross-account correlation).
    """
    if not mac_registry:
        raise HTTPException(status_code=503, detail="MAC registry not available")

    mac = mac.upper()
    device = mac_registry.get_device_by_mac(mac)
    if not device:
        raise HTTPException(status_code=404, detail=f"Device {mac} not found")

    if not config.proxies:
        raise HTTPException(status_code=400, detail="No proxies available")

    # Lấy proxy đang dùng bởi các device khác → tránh gán trùng
    all_devices = mac_registry.get_all_devices()
    used_proxy_ids = {
        d.proxy_id for d in all_devices
        if d.proxy_id and d.mac != mac
    }

    current_proxy_id = device.proxy_id

    # Ưu tiên proxy chưa ai dùng; fallback sang proxy ít dùng nhất
    import random
    available_proxies = [p for p in config.proxies if p.id != current_proxy_id]
    if not available_proxies:
        raise HTTPException(status_code=400, detail="Only 1 proxy available, cannot rotate")

    # Thử chọn proxy chưa ai dùng trước
    unused = [p for p in available_proxies if p.id not in used_proxy_ids]
    if unused:
        new_proxy = random.choice(unused)
    else:
        new_proxy = random.choice(available_proxies)

    mac_registry.set_proxy(mac, new_proxy.id)

    # Zero-downtime: đổi qua Clash API (không kill/tạo lại TUN adapter) — trước đây gọi
    # reload_now() (full restart) cho MỖI lần rotate, mà rotate là core feature chống
    # fingerprint nên bị gọi rất thường xuyên → sing-box bị stop/start liên tục. Chỉ
    # update_device_routing() (bên trong tự fallback full restart nếu API thất bại/sing-box
    # chưa chạy) mới khớp với cách assign_proxy() đã làm đúng.
    reload_ok = True
    if singbox_manager:
        try:
            singbox_manager.update_config(config)
            reload_ok = singbox_manager.update_device_routing(device.ip, new_proxy.id)
        except Exception as e:
            reload_ok = False
            logger.error(f"[Proxies] rotate reload error: {e}")

    if not reload_ok:
        err = getattr(singbox_manager, "last_error", None) or "Không xác định được lỗi"
        raise HTTPException(
            status_code=502,
            detail=f"Đã đổi proxy nhưng áp dụng routing thất bại ({err}). "
                   f"Thiết bị {mac} CHƯA được chuyển qua proxy mới — vui lòng thử lại."
        )

    logger.info(f"[Proxies] Rotated {mac}: {current_proxy_id} → {new_proxy.id}")
    return {
        "status": "success",
        "mac": mac,
        "old_proxy_id": current_proxy_id,
        "new_proxy_id": new_proxy.id,
        "new_proxy_host": new_proxy.host,
    }


@router.post("/rotate-all")
def rotate_all_devices(
    config=Depends(get_config),
    mac_registry=Depends(get_mac_registry),
    singbox_manager=Depends(get_singbox_manager),
):
    """Rotate proxy cho tất cả device có gán proxy, tránh correlation pattern.

    Thuật toán:
    - Shuffle danh sách proxy
    - Gán round-robin cho các device đang có proxy (giữ 1:1 ratio)
    - Đảm bảo không có 2 device liền nhau dùng cùng proxy cũ của nhau
    """
    if not mac_registry:
        raise HTTPException(status_code=503, detail="MAC registry not available")

    if not config.proxies:
        raise HTTPException(status_code=400, detail="No proxies available")

    import random
    all_devices = mac_registry.get_all_devices()
    proxy_devices = [d for d in all_devices if d.proxy_id]

    if not proxy_devices:
        return {"status": "success", "message": "No devices with proxy assigned", "rotated": 0}

    if len(config.proxies) < 2:
        return {"status": "skipped", "message": "Need at least 2 proxies to rotate", "rotated": 0}

    # Shuffle proxy list để random hóa thứ tự gán
    proxy_pool = config.proxies.copy()
    random.shuffle(proxy_pool)

    rotated = []
    for i, device in enumerate(proxy_devices):
        # Round-robin qua proxy pool, đảm bảo khác proxy cũ nếu có thể
        for offset in range(len(proxy_pool)):
            candidate = proxy_pool[(i + offset) % len(proxy_pool)]
            if candidate.id != device.proxy_id:
                break

        old_id = device.proxy_id
        mac_registry.set_proxy(device.mac, candidate.id)
        rotated.append({"mac": device.mac, "old": old_id, "new": candidate.id})
        logger.info(f"[Proxies] rotate-all: {device.mac}: {old_id} → {candidate.id}")

    # Raise HTTPException (thay vì trả 200 kèm status="error") vì frontend chỉ check
    # res.ok, không đọc field "status" trong body — trả 200 giả sẽ khiến lỗi bị bỏ qua.
    reload_ok = True
    if rotated and singbox_manager:
        try:
            assignments_to_apply = []
            for item in rotated:
                d = mac_registry.get_device_by_mac(item["mac"])
                if d and d.ip:
                    assignments_to_apply.append((d.ip, item["new"]))
            reload_ok = singbox_manager.update_multiple_devices_routing(assignments_to_apply)
        except Exception as e:
            reload_ok = False
            logger.error(f"[Proxies] rotate-all reload error: {e}")

    if not reload_ok:
        err = getattr(singbox_manager, "last_error", None) or "Không xác định được lỗi"
        raise HTTPException(
            status_code=502,
            detail=f"Đã đổi proxy cho {len(rotated)} thiết bị nhưng áp dụng routing thất bại "
                   f"({err}). Vui lòng thử lại."
        )

    return {
        "status": "success",
        "rotated": len(rotated),
        "assignments": rotated,
    }


@router.get("/check-blacklist/{proxy_id}")
def check_proxy_blacklist(
    proxy_id: str,
    config=Depends(get_config),
):
    """Kiểm tra IP của proxy có trong DNSBL blacklist không.

    Checks against: Spamhaus ZEN, SpamCop, SORBS, Spamhaus XBL, Abuseat CBL.
    Kết quả được lưu vào config để tránh check lại quá thường xuyên.

    Returns: {proxy_id, ip, blacklisted, lists, checked_at}
    """
    proxy = next((p for p in config.proxies if p.id == proxy_id), None)
    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")

    result = check_ip_blacklist(proxy.host)

    # Persist result into config
    proxy.blacklisted = result["blacklisted"]
    proxy.blacklisted_on = result.get("lists", [])
    proxy.blacklist_checked_at = result["checked_at"]
    save_config(config)

    return {
        "proxy_id": proxy_id,
        "ip": proxy.host,
        "blacklisted": result["blacklisted"],
        "lists": result.get("lists", []),
        "checked_at": result["checked_at"],
    }


# ─── API DPN TỰ ĐỘNG CÀO NODE SẠCH & ĐỊNH TUYẾN ──────────────────────

@router.get("/dpn/progress/{mac}")
def get_dpn_progress(mac: str):
    """Lấy tiến trình gán DPN hiện tại cho 1 thiết bị — client poll endpoint này
    để hiển thị progress bar / bước đang chạy trong lúc chờ fetch-and-assign."""
    return _dpn_progress.get(mac.upper(), {"status": "idle"})


@router.get("/dpn/fetch-and-assign")
@router.post("/dpn/fetch-and-assign")
def fetch_and_assign_dpn(
    request: Request,
    country: str = "US",
    ip: str = None,
    mac: str = None,
    check_clean: bool = True,
    config=Depends(get_config),
    mac_registry=Depends(get_mac_registry),
    singbox_manager=Depends(get_singbox_manager)
):
    """
    Tự động cào danh sách DPN proxies của quốc gia mong muốn,
    kiểm tra độ sạch của IP (DNSBL blacklist) và gán cho thiết bị.
    """
    import random
    import requests

    if not mac_registry:
        raise HTTPException(status_code=503, detail="MAC Registry not available")

    # 1. Định danh thiết bị
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

    # 2. Fetch danh sách proxy DPN của quốc gia đã chọn
    country_upper = country.upper()
    _dpn_set_progress(
        target_mac, status="running", step="fetching_list",
        attempt=0, max_retries=DPN_MAX_RETRIES, country=country_upper,
        message=f"Đang lấy danh sách proxy {country_upper}..."
    )
    logger.info(f"[DPN API] Đang lấy danh sách proxy DPN {country_upper} cho thiết bị {target_mac} ({device.ip})...")
    
    # URL Proxyscrape lấy SOCKS5 proxy theo quốc gia
    proxyscrape_url = f"https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=10000&country={country_upper}&ssl=all&anonymity=all"
    
    proxy_list = []
    try:
        resp = requests.get(proxyscrape_url, timeout=12)
        if resp.status_code == 200:
            raw_proxies = resp.text.strip().split("\r\n")
            if not raw_proxies or raw_proxies == ['']:
                raw_proxies = resp.text.strip().split("\n")
            for p in raw_proxies:
                p = p.strip()
                if ":" in p:
                    proxy_list.append(p)
    except Exception as e:
        logger.error(f"[DPN API] Lỗi fetch Proxyscrape: {e}")

    # Fallback list nếu Proxyscrape trống
    if not proxy_list:
        logger.warning("[DPN API] Danh sách DPN Proxyscrape trống, thử nguồn dự phòng...")
        fallback_urls = [
            "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt",
            "https://raw.githubusercontent.com/Hookzof/socks5_list/master/proxy.txt"
        ]
        for furl in fallback_urls:
            try:
                fresp = requests.get(furl, timeout=10)
                if fresp.status_code == 200:
                    raw_p = fresp.text.strip().split("\n")
                    for p in raw_p:
                        p = p.strip()
                        if ":" in p:
                            proxy_list.append(p)
                    break
            except Exception as fe:
                logger.error(f"[DPN API] Fallback fetch error: {fe}")

    if not proxy_list:
        _dpn_set_progress(
            target_mac, status="error", step="no_proxies",
            message=f"Không thể lấy danh sách nút mạng DPN cho quốc gia [{country_upper}]"
        )
        raise HTTPException(
            status_code=502,
            detail=f"Không thể lấy danh sách nút mạng DPN cho quốc gia [{country_upper}]"
        )

    _dpn_set_progress(
        target_mac, status="running", step="checking",
        attempt=0, max_retries=DPN_MAX_RETRIES,
        message=f"Đã tìm thấy {len(proxy_list)} proxy ứng viên, đang kiểm tra..."
    )

    # 3. Lặp tìm proxy đúng quốc gia yêu cầu và đạt chuẩn sạch (không dính DNSBL blacklist)
    #    Nhãn "country" của ProxyScrape/fallback list không đáng tin cậy (dữ liệu tự
    #    thu thập, có thể lỗi thời) — phải xác thực quốc gia thật qua geolocation API
    #    trước khi chấp nhận, nếu không thiết bị có thể bị gán nhầm IP nước khác.
    max_retries = DPN_MAX_RETRIES
    selected_proxy = None
    attempt = 0

    # Xáo trộn danh sách proxy để chọn ngẫu nhiên
    random.shuffle(proxy_list)

    while attempt < max_retries and len(proxy_list) > 0:
        attempt += 1
        proxy_str = proxy_list.pop(0)

        try:
            p_host, p_port = proxy_str.split(":", 1)
            p_port = int(p_port)
        except Exception:
            continue

        logger.info(f"[DPN API] Thử kết nối nháp (Thử lần {attempt}): {proxy_str}")
        _dpn_set_progress(
            target_mac, status="running", step="checking_geo",
            attempt=attempt, max_retries=max_retries,
            message=f"Thử {attempt}/{max_retries}: xác minh quốc gia của {p_host}..."
        )

        # Xác thực quốc gia thực tế của IP — không tin nhãn "country" của nguồn proxy
        try:
            geo_resp = requests.get(f"https://ipinfo.io/{p_host}/json", timeout=6)
            actual_country = geo_resp.json().get("country", "").upper() if geo_resp.status_code == 200 else ""
        except Exception as e:
            logger.warning(f"[DPN API] Không xác định được quốc gia của IP {p_host}: {e}")
            actual_country = ""

        if actual_country != country_upper:
            logger.warning(
                f"-> IP [{p_host}] thực tế ở [{actual_country or 'Không xác định'}], "
                f"không khớp quốc gia yêu cầu [{country_upper}]. Đang đổi nút..."
            )
            _dpn_set_progress(
                target_mac, status="running", step="checking_geo",
                attempt=attempt, max_retries=max_retries,
                message=f"Thử {attempt}/{max_retries}: {p_host} ở {actual_country or '?'}, không khớp {country_upper} — đổi nút..."
            )
            continue

        if check_clean:
            _dpn_set_progress(
                target_mac, status="running", step="checking_blacklist",
                attempt=attempt, max_retries=max_retries,
                message=f"Thử {attempt}/{max_retries}: kiểm tra blacklist cho {p_host}..."
            )
            try:
                bl_result = check_ip_blacklist(p_host)
                is_dirty = bl_result.get("blacklisted", False)
                lists_hit = bl_result.get("lists", [])

                status_label = "Không Đạt" if is_dirty else "Đạt"
                logger.info(
                    f"Đang kiểm tra IP: [{p_host}] - "
                    f"DNSBL: [{', '.join(lists_hit) if lists_hit else 'Sạch'}] - Trạng thái: [{status_label}]"
                )

                if is_dirty:
                    logger.warning(f"-> IP [{p_host}] nằm trong blacklist ({', '.join(lists_hit)}). Đang đổi nút...")
                    continue
            except Exception as e:
                logger.error(f"[DPN API] Lỗi khi đánh giá IP {p_host}: {e}")
                continue

        # Kiểm tra proxy còn sống + đo tốc độ phản hồi thực tế (kết nối SOCKS5 thật tới
        # gstatic.com) trước khi gán — nguồn proxy công khai miễn phí có tỷ lệ chết rất
        # cao, nếu gán trúng proxy chết/quá chậm thiết bị sẽ "treo mạng" gần như vô thời
        # hạn mà không có cảnh báo rõ ràng. attempts=1 vì đã có vòng lặp thử proxy khác
        # ở ngoài, không cần retry nội bộ (tránh nhân đôi thời gian chờ).
        _dpn_set_progress(
            target_mac, status="running", step="checking_speed",
            attempt=attempt, max_retries=max_retries,
            message=f"Thử {attempt}/{max_retries}: đo tốc độ phản hồi của {p_host}:{p_port}..."
        )
        _, live_status, latency_ms = test_single_proxy(
            {"id": proxy_str, "type": "socks5", "host": p_host, "port": p_port, "username": "", "password": ""},
            attempts=1,
        )
        if live_status != "Live":
            logger.warning(f"-> IP [{p_host}:{p_port}] không phản hồi (Die). Đang đổi nút...")
            continue
        if latency_ms > DPN_MAX_LATENCY_MS:
            logger.warning(f"-> IP [{p_host}:{p_port}] phản hồi chậm ({latency_ms}ms > {DPN_MAX_LATENCY_MS}ms). Đang đổi nút...")
            continue

        selected_proxy = (p_host, p_port, latency_ms)
        break

    if not selected_proxy:
        _dpn_set_progress(
            target_mac, status="error", step="not_found",
            attempt=attempt, max_retries=max_retries,
            message=f"Không tìm thấy IP đúng khu vực [{country_upper}] sau {max_retries} lần thử."
        )
        raise HTTPException(
            status_code=502,
            detail=f"Không tìm thấy IP đúng khu vực [{country_upper}] và đạt chuẩn sạch sau {max_retries} lần thử, vui lòng thử lại sau."
        )

    p_host, p_port, selected_latency_ms = selected_proxy
    proxy_id = f"dpn_{p_host.replace('.', '_')}_{p_port}"
    _dpn_set_progress(
        target_mac, status="running", step="assigning",
        attempt=attempt, max_retries=max_retries,
        message=f"Đã tìm IP hợp lệ {p_host}:{p_port} ({selected_latency_ms}ms) — đang áp dụng cấu hình định tuyến..."
    )

    # 4. Thiết lập Full VPN Tunnel (Thêm proxy vào config và gán cho thiết bị)
    existing = next((p for p in config.proxies if p.id == proxy_id), None)
    if not existing:
        new_proxy = ProxyConfig(
            id=proxy_id,
            type="socks5",
            host=p_host,
            port=p_port,
            status="Alive",
            latency=selected_latency_ms,
            dns_server="1.1.1.1"
        )
        config.proxies.append(new_proxy)
        save_config(config)
        logger.info(f"[DPN API] Đã import proxy DPN mới: {proxy_id} (latency={selected_latency_ms}ms)")
    else:
        existing.status = "Alive"
        existing.latency = selected_latency_ms
        save_config(config)

    # Gán proxy cho thiết bị
    mac_registry.set_proxy(target_mac, proxy_id)
    logger.info(f"[DPN API] Gán proxy {proxy_id} cho thiết bị {target_mac} ({device.ip})")

    # Áp dụng routing NGAY (đồng bộ, chờ kết quả thật) — trước đây gọi hot_reload() rồi
    # trả "success" luôn mà không biết reload có thực sự xảy ra không: hot_reload() chỉ
    # hẹn giờ debounce 1s chạy nền, và bên trong nó từng tin vào Clash API trả HTTP 200
    # dù route_rules mới không được nạp thật (đã xác nhận trên máy thật — xem reload_now()
    # trong singbox_manager.py). Kết quả: DPN báo "Thiết lập thành công" nhưng thiết bị
    # vẫn đi IP thật cho tới khi người dùng tự dừng/mở lại routing thủ công.
    _dpn_set_progress(
        target_mac, status="running", step="applying_routing",
        attempt=attempt, max_retries=max_retries,
        message=f"Đang áp dụng định tuyến qua {p_host}:{p_port} (khởi động lại sing-box)..."
    )
    singbox_manager.update_config(config)
    reload_ok = singbox_manager.update_device_routing(device.ip, proxy_id)

    if not reload_ok:
        err = singbox_manager.last_error or "Không xác định được lỗi"
        logger.error(f"[DPN API] Áp dụng routing thất bại cho {target_mac}: {err}")
        _dpn_set_progress(
            target_mac, status="error", step="apply_failed",
            attempt=attempt, max_retries=max_retries,
            message=f"Gán proxy {p_host}:{p_port} vào cấu hình OK nhưng áp dụng routing thất bại: {err}"
        )
        raise HTTPException(
            status_code=502,
            detail=f"Đã chọn được nút sạch {p_host}:{p_port} nhưng áp dụng routing thất bại ({err}). "
                   f"Thiết bị CHƯA được chuyển qua proxy này — vui lòng thử lại."
        )

    logger.info(f"[DPN API] Thiết lập đường truyền DPN thành công cho {target_mac} qua nút sạch {p_host}:{p_port}")
    _dpn_set_progress(
        target_mac, status="done", step="success",
        attempt=attempt, max_retries=max_retries,
        message=f"✓ Đã gán {p_host}:{p_port} ({selected_latency_ms}ms, {country_upper})"
    )

    return {
        "status": "success",
        "mac": target_mac,
        "ip": device.ip,
        "proxy_id": proxy_id,
        "proxy": {
            "type": "socks5",
            "host": p_host,
            "port": p_port,
            "latency_ms": selected_latency_ms,
            "raw": f"socks5://{p_host}:{p_port}",
            "simple": f"{p_host}:{p_port}"
        },
        "message": f"Thiết lập DPN Tunnel thành công cho thiết bị qua nút sạch {p_host}:{p_port} ({selected_latency_ms}ms)"
    }


