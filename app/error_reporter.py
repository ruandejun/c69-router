"""
GenRouter — Error Reporter

Gửi lỗi quan trọng (NAT, sing-box, startup...) về server tập trung (c69-backend)
để tra cứu nhanh 1 chỗ, thay vì phải xin log thủ công từ người dùng mỗi lần gặp
sự cố. Chạy nền, không bao giờ làm chậm/crash luồng chính dù mạng lỗi hay server
không phản hồi — lỗi khi gửi báo cáo chỉ log debug, không raise ra ngoài.
"""

import os
import platform
import threading
import traceback as tb_module
import uuid
import logging

import requests

from app.config import PROJECT_DIR

logger = logging.getLogger(__name__)

ERROR_REPORT_URL = "https://c69.us/api/error-reports/"
# Phải khớp ERROR_REPORT_SECRET trong storagon/settings_base.py (c69-backend)
ERROR_REPORT_SECRET = "827acde8e50815ba5f96e07f7dea34bc1ea67d440333e687"
MACHINE_ID_FILE = os.path.join(PROJECT_DIR, "data", "machine_id.txt")

_machine_id_cache = None


def _get_machine_id() -> str:
    """UUID cố định sinh 1 lần cho mỗi máy cài, lưu lại để nhận diện qua các lần chạy."""
    global _machine_id_cache
    if _machine_id_cache:
        return _machine_id_cache
    try:
        if os.path.exists(MACHINE_ID_FILE):
            with open(MACHINE_ID_FILE, "r", encoding="utf-8") as f:
                _machine_id_cache = f.read().strip()
        if not _machine_id_cache:
            _machine_id_cache = uuid.uuid4().hex
            os.makedirs(os.path.dirname(MACHINE_ID_FILE), exist_ok=True)
            with open(MACHINE_ID_FILE, "w", encoding="utf-8") as f:
                f.write(_machine_id_cache)
    except Exception:
        _machine_id_cache = "unknown"
    return _machine_id_cache


def report_error(component: str, message: str, level: str = "error", exc: Exception = None, context: dict = None):
    """Gửi 1 lỗi về server tập trung. Không blocking — chạy trên thread riêng.

    component: vd "NAT", "SingBox", "DHCP", "Startup"
    level: "info" | "warning" | "error" | "critical"
    exc: exception gốc nếu có, để gửi kèm traceback đầy đủ
    context: dict bổ sung tuỳ ý (vd wan_interface, lan_gateway_ip...)
    """
    def _send():
        try:
            payload = {
                "source": "c69-router",
                "machine_id": _get_machine_id(),
                "component": component,
                "level": level,
                "message": str(message)[:4000],
                "traceback": (
                    "".join(tb_module.format_exception(type(exc), exc, exc.__traceback__))[:8000]
                    if exc else ""
                ),
                "context": {**(context or {}), "platform": platform.platform()},
            }
            requests.post(
                ERROR_REPORT_URL,
                json=payload,
                headers={"X-Report-Key": ERROR_REPORT_SECRET},
                timeout=5,
            )
        except Exception as e:
            logger.debug(f"[ErrorReporter] Gửi báo cáo lỗi thất bại (không nghiêm trọng): {e}")

    threading.Thread(target=_send, daemon=True, name="error-reporter").start()
