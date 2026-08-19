"""
GenRouter — Webshare API Integration

Quản lý kết nối tới Webshare.io API v2:
- Lấy danh sách proxy SOCKS5 (direct mode)
- Check proxy health định kỳ (cronjob)
- Tự động thay thế proxy Die bằng proxy mới từ Webshare

API Docs: https://proxy.webshare.io/api/v2/docs/
Auth: Header `Authorization: Token <API_KEY>`
"""

import logging
import time
import requests
from typing import List, Optional, Dict, Any, Tuple

logger = logging.getLogger(__name__)

# ── Webshare Health Loop State (cho API endpoint /webshare/status) ──
_webshare_state: dict = {
    "last_check_time": 0.0,
    "last_check_result": None,       # "success" | "error" | None
    "last_check_detail": "",
    "proxies_checked": 0,
    "proxies_dead": 0,
    "proxies_replaced": 0,
    "total_checks": 0,
    "total_replacements": 0,
    "is_running": False,
}


def get_webshare_status() -> dict:
    """Trả về trạng thái hiện tại của Webshare health loop."""
    return dict(_webshare_state)


def fetch_webshare_proxy_list(
    api_key: str,
    page: int = 1,
    page_size: int = 100,
) -> Tuple[List[Dict[str, Any]], int]:
    """Gọi Webshare API v2 lấy danh sách proxy (direct mode, SOCKS5).

    Args:
        api_key: Webshare API Token
        page: Trang (phân trang)
        page_size: Số proxy mỗi trang (tối đa 100)

    Returns:
        (list_of_proxy_dicts, total_count)
        Mỗi proxy dict có keys: id, proxy_address, port, username, password, valid, country_code, city_name
    """
    url = "https://proxy.webshare.io/api/v2/proxy/list/"
    headers = {"Authorization": f"Token {api_key}"}
    params = {
        "mode": "direct",
        "page": page,
        "page_size": min(page_size, 100),
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        total = data.get("count", len(results))

        proxies = []
        for p in results:
            proxies.append({
                "id": p.get("id", ""),
                "proxy_address": p.get("proxy_address", ""),
                "port": p.get("port", 0),
                "username": p.get("username", ""),
                "password": p.get("password", ""),
                "valid": p.get("valid", False),
                "country_code": p.get("country_code", ""),
                "city_name": p.get("city_name", ""),
            })

        return proxies, total

    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response else "?"
        logger.error(f"[Webshare] API HTTP error {status_code}: {e}")
        raise
    except Exception as e:
        logger.error(f"[Webshare] API request failed: {e}")
        raise


def fetch_all_webshare_proxies(api_key: str) -> List[Dict[str, Any]]:
    """Lấy TOÀN BỘ danh sách proxy từ Webshare (tự phân trang).

    Returns:
        List of proxy dicts (chỉ lấy proxy có valid=True)
    """
    all_proxies = []
    page = 1
    page_size = 100

    while True:
        proxies, total = fetch_webshare_proxy_list(api_key, page=page, page_size=page_size)
        all_proxies.extend(proxies)

        if len(all_proxies) >= total or not proxies:
            break
        page += 1

    # Chỉ giữ proxy valid
    valid_proxies = [p for p in all_proxies if p.get("valid", False)]
    logger.info(f"[Webshare] Fetched {len(all_proxies)} proxies total, {len(valid_proxies)} valid")
    return valid_proxies


def find_replacement_proxy(
    dead_proxy_host: str,
    dead_proxy_port: int,
    current_proxy_ids: set,
    webshare_proxies: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Tìm proxy Webshare chưa được dùng để thay thế proxy Die.

    Ưu tiên: proxy chưa có trong danh sách config hiện tại.

    Args:
        dead_proxy_host: Host của proxy Die
        dead_proxy_port: Port của proxy Die
        current_proxy_ids: Set các proxy ID đang dùng trong config
        webshare_proxies: Danh sách proxy từ Webshare API

    Returns:
        Proxy dict phù hợp hoặc None
    """
    for wp in webshare_proxies:
        ws_host = wp.get("proxy_address", "")
        ws_port = wp.get("port", 0)

        # Bỏ qua chính proxy Die
        if ws_host == dead_proxy_host and ws_port == dead_proxy_port:
            continue

        # Tạo candidate ID
        candidate_id = f"ws_{ws_host.replace('.', '_')}_{ws_port}"
        if candidate_id in current_proxy_ids:
            continue  # Đã được dùng trong config

        if not wp.get("valid", False):
            continue  # Không valid

        return wp

    return None


def replace_dead_proxy(
    dead_proxy,
    config,
    mac_registry,
    singbox_manager,
    webshare_proxies: List[Dict[str, Any]],
) -> Optional[str]:
    """Thay thế 1 proxy Die bằng proxy mới từ Webshare.

    Quy trình:
    1. Tìm proxy Webshare chưa dùng
    2. Thêm proxy mới vào config (hoặc cập nhật nếu đã tồn tại)
    3. Tìm tất cả thiết bị đang dùng proxy Die → chuyển sang proxy mới
    4. Cập nhật routing

    Args:
        dead_proxy: ProxyConfig object của proxy Die
        config: AppConfig
        mac_registry: MACRegistry
        singbox_manager: SingBoxManager
        webshare_proxies: Danh sách proxy từ Webshare API

    Returns:
        new_proxy_id nếu thay thế thành công, None nếu không
    """
    from app.config import ProxyConfig, save_config

    current_ids = {p.id for p in config.proxies}

    replacement = find_replacement_proxy(
        dead_proxy.host, dead_proxy.port, current_ids, webshare_proxies
    )

    if not replacement:
        logger.warning(f"[Webshare] Không tìm được proxy thay thế cho {dead_proxy.id}")
        return None

    ws_host = replacement["proxy_address"]
    ws_port = replacement["port"]
    ws_user = replacement.get("username", "")
    ws_pass = replacement.get("password", "")
    new_proxy_id = f"ws_{ws_host.replace('.', '_')}_{ws_port}"

    # Thêm proxy mới vào config
    existing = next((p for p in config.proxies if p.id == new_proxy_id), None)
    if not existing:
        new_proxy = ProxyConfig(
            id=new_proxy_id,
            type="socks5",
            host=ws_host,
            port=ws_port,
            username=ws_user,
            password=ws_pass,
            status="Live",
            latency=-1,
            dns_server="1.1.1.1",
        )
        config.proxies.append(new_proxy)
        logger.info(f"[Webshare] Thêm proxy mới: {new_proxy_id} ({ws_host}:{ws_port})")
    else:
        existing.host = ws_host
        existing.port = ws_port
        existing.username = ws_user
        existing.password = ws_pass
        existing.status = "Live"
        logger.info(f"[Webshare] Cập nhật proxy: {new_proxy_id}")

    # Tìm tất cả thiết bị đang dùng proxy Die → chuyển sang proxy mới
    reassigned = []
    if mac_registry:
        all_devices = mac_registry.get_all_devices()
        for dev in all_devices:
            if dev.proxy_id == dead_proxy.id:
                mac_registry.set_proxy(dev.mac, new_proxy_id)
                reassigned.append((dev.ip, new_proxy_id, dev.mac))
                logger.info(
                    f"[Webshare] Chuyển thiết bị {dev.mac} ({dev.ip}): "
                    f"{dead_proxy.id} → {new_proxy_id}"
                )

    # Đánh dấu proxy cũ là Die (giữ lại trong config để audit, không xóa)
    dead_proxy.status = "Die"

    save_config(config)

    # Cập nhật routing cho các thiết bị vừa chuyển proxy
    if singbox_manager and reassigned:
        try:
            assignments = [(ip, pid) for ip, pid, _ in reassigned if ip]
            if assignments:
                singbox_manager.update_config(config)
                singbox_manager.update_multiple_devices_routing(assignments)
                logger.info(f"[Webshare] Đã cập nhật routing cho {len(assignments)} thiết bị")
        except Exception as e:
            logger.error(f"[Webshare] Lỗi cập nhật routing: {e}")

    return new_proxy_id


def run_health_check_and_replace(config, mac_registry, singbox_manager):
    """Chạy 1 vòng check proxy health + thay thế Die từ Webshare.

    Được gọi bởi webshare_health_loop() hoặc API endpoint thủ công.

    Returns:
        dict với kết quả {checked, dead, replaced, details}
    """
    global _webshare_state
    from app.utils import bulk_test_proxies
    from app.config import save_config

    _webshare_state["is_running"] = True
    result = {
        "checked": 0,
        "dead": 0,
        "replaced": 0,
        "details": [],
    }

    try:
        if not config.proxies:
            _webshare_state["last_check_result"] = "success"
            _webshare_state["last_check_detail"] = "Không có proxy nào để kiểm tra"
            return result

        # 1. Kiểm tra tất cả proxy
        proxy_infos = [p.model_dump() for p in config.proxies]
        check_results = bulk_test_proxies(proxy_infos, max_workers=min(len(proxy_infos), 50))
        result["checked"] = len(check_results)

        # Cập nhật status vào config
        for pid, status, latency in check_results:
            for p in config.proxies:
                if p.id == pid:
                    p.status = status
                    p.latency = latency

        save_config(config)

        # 2. Tìm proxy Die
        dead_proxies = [p for p in config.proxies if p.status == "Die"]
        result["dead"] = len(dead_proxies)

        if not dead_proxies:
            _webshare_state["last_check_result"] = "success"
            _webshare_state["last_check_detail"] = (
                f"Tất cả {result['checked']} proxy đều Live"
            )
            logger.info(f"[Webshare] Health check OK: {result['checked']} proxies all Live")
            return result

        logger.warning(f"[Webshare] Phát hiện {len(dead_proxies)} proxy Die: "
                       f"{[p.id for p in dead_proxies]}")

        # 3. Nếu auto_replace enabled và có API key → thay thế từ Webshare
        if not config.webshare_auto_replace:
            _webshare_state["last_check_result"] = "success"
            _webshare_state["last_check_detail"] = (
                f"{len(dead_proxies)} proxy Die (auto-replace tắt)"
            )
            return result

        if not config.webshare_api_key:
            _webshare_state["last_check_result"] = "error"
            _webshare_state["last_check_detail"] = (
                f"{len(dead_proxies)} proxy Die nhưng chưa cấu hình Webshare API key"
            )
            logger.warning("[Webshare] Proxy Die nhưng chưa có API key để lấy proxy mới")
            return result

        # Lấy danh sách proxy Webshare
        try:
            webshare_proxies = fetch_all_webshare_proxies(config.webshare_api_key)
        except Exception as e:
            _webshare_state["last_check_result"] = "error"
            _webshare_state["last_check_detail"] = f"Lỗi gọi Webshare API: {e}"
            return result

        if not webshare_proxies:
            _webshare_state["last_check_result"] = "error"
            _webshare_state["last_check_detail"] = (
                "Webshare API trả về danh sách proxy rỗng"
            )
            return result

        # 4. Thay thế từng proxy Die
        for dead_p in dead_proxies:
            new_id = replace_dead_proxy(
                dead_p, config, mac_registry, singbox_manager, webshare_proxies
            )
            detail = {
                "dead_proxy": dead_p.id,
                "dead_host": f"{dead_p.host}:{dead_p.port}",
            }
            if new_id:
                result["replaced"] += 1
                detail["replacement"] = new_id
                detail["status"] = "replaced"
            else:
                detail["status"] = "no_replacement"
            result["details"].append(detail)

        _webshare_state["last_check_result"] = "success"
        _webshare_state["last_check_detail"] = (
            f"Checked {result['checked']}, "
            f"Dead {result['dead']}, "
            f"Replaced {result['replaced']}"
        )

        # Hot reload sing-box nếu có thay thế để đảm bảo routing mới
        if result["replaced"] > 0 and singbox_manager:
            try:
                singbox_manager.update_config(config)
                singbox_manager.hot_reload()
                logger.info("[Webshare] Hot-reload sing-box sau khi thay proxy")
            except Exception as e:
                logger.error(f"[Webshare] Hot-reload error: {e}")

    except Exception as e:
        _webshare_state["last_check_result"] = "error"
        _webshare_state["last_check_detail"] = f"Lỗi health check: {e}"
        logger.error(f"[Webshare] Health check error: {e}", exc_info=True)
    finally:
        _webshare_state["is_running"] = False
        _webshare_state["last_check_time"] = time.time()
        _webshare_state["proxies_checked"] = result["checked"]
        _webshare_state["proxies_dead"] = result["dead"]
        _webshare_state["proxies_replaced"] = result["replaced"]
        _webshare_state["total_checks"] += 1
        _webshare_state["total_replacements"] += result["replaced"]

    return result
