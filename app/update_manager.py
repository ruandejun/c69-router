"""
PhoneFarm GenRouter v2.0 — Auto-Update Manager

Kiểm tra bản mới từ c69-backend (endpoint riêng /api/router-version/, song song với
/api/tool-version/ mà C69Automation đang dùng), tải zip về và áp dụng bằng cách spawn
c69update.exe — dùng CHUNG cơ chế updater đã có với C69Automation (MunAutomationDesktop/
c69update.py): updater là process độc lập, chỉ nhận --pid/--zip/--exe/--dir qua CLI nên
không quan tâm app nào gọi nó, đợi PID cha thoát rồi mới thay file exe (Windows không cho
rename/xoá exe đang chạy) và khởi động lại app.

Khác C69Automation ở chỗ: c69-router là service nền không có cửa sổ riêng — trạng thái
kiểm tra bản mới được expose qua API (routes/update.py) để dashboard React hiển thị banner,
và việc apply LUÔN cần người dùng bấm xác nhận trên dashboard (không tự động áp dụng ngầm)
vì 1 bản lỗi tự áp dụng sẽ restart router đang phục vụ hàng trăm thiết bị live cùng lúc.
"""

import hashlib
import re
import secrets
import os
import json
import logging
import threading
import subprocess
import ssl
import urllib.request
from typing import Optional, Callable
from urllib.parse import urlparse

from app.config import PROJECT_DIR

logger = logging.getLogger(__name__)

CLIENT_VERSION = "2.1.0"
ROUTER_VERSION_URL = "https://c69.us/api/router-version/"
UPDATE_ALLOWED_HOSTS = {"c69.us", "www.c69.us", "cdn.c69.us"}
EXE_NAME = "c69-router.exe"
UPDATER_EXE_NAME = "c69update.exe"

# Trạng thái lần kiểm tra gần nhất — đọc bởi routes/update.py, ghi bởi check_for_update().
_state = {
    "checked": False,
    "available": False,
    "current_version": CLIENT_VERSION,
    "version": "",
    "download_url": "",
    "sha256": "",
    "changelog": "",
    "error": None,
}
_state_lock = threading.Lock()


def get_state() -> dict:
    with _state_lock:
        return dict(_state)


def _is_newer(server_version: str) -> bool:
    try:
        from packaging.version import parse as _parse
        return _parse(server_version) > _parse(CLIENT_VERSION)
    except Exception:
        return bool(server_version) and server_version != CLIENT_VERSION


def _validate_update_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in UPDATE_ALLOWED_HOSTS:
        raise ValueError("Update URL must use HTTPS and an approved C69 host")
    return url


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_for_update() -> dict:
    """Gọi /api/router-version/, cập nhật _state. Trả về state mới nhất (kể cả khi lỗi)."""
    try:
        req = urllib.request.Request(
            ROUTER_VERSION_URL,
            headers={"User-Agent": f"C69Router/{CLIENT_VERSION}"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        server_version = data.get("version", "")
        download_url = _validate_update_url(data.get("download_url", "")) if data.get("download_url") else ""
        sha256 = str(data.get("sha256", "")).lower().strip()
        changelog = data.get("changelog", "")
        available = bool(server_version and download_url and re.fullmatch(r"[0-9a-f]{64}", sha256) and _is_newer(server_version))

        with _state_lock:
            _state.update({
                "checked": True,
                "available": available,
                "version": server_version,
                "download_url": download_url,
                "sha256": sha256,
                "changelog": changelog,
                "error": None,
            })
        if available:
            logger.info(f"[AutoUpdate] Phát hiện bản mới: v{server_version} (hiện tại v{CLIENT_VERSION})")
    except Exception as e:
        logger.warning(f"[AutoUpdate] Kiểm tra bản mới thất bại: {e}")
        with _state_lock:
            _state["checked"] = True
            _state["error"] = str(e)

    return get_state()


def _download(url: str, dest_path: str, expected_sha256: str):
    _validate_update_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": f"C69Router/{CLIENT_VERSION}"})
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with urllib.request.urlopen(req, timeout=180) as resp:
        with open(dest_path, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
    actual_sha256 = _sha256_file(dest_path)
    if not secrets.compare_digest(actual_sha256, expected_sha256.lower()):
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise ValueError("Downloaded update checksum does not match the signed release metadata")


def apply_update_and_exit(
    download_url: str,
    expected_sha256: str,
    on_before_exit: Optional[Callable[[], None]] = None,
) -> None:
    """Tải bản zip mới, spawn c69update.exe rồi THOÁT tiến trình hiện tại.

    Chạy hàm này trong 1 thread nền (KHÔNG gọi trực tiếp từ request handler) — nó block
    trong lúc tải file rồi cuối cùng gọi os._exit(), phải để response HTTP kịp trả về
    client trước đó (xem routes/update.py).

    on_before_exit: cleanup gọi TRƯỚC khi thoát (vd dừng DHCP/sing-box) — truyền vào từ
    app/main.py vì module này không giữ tham chiếu tới các global đó (tránh circular
    import). c69update.exe vẫn đợi PID thoát bình thường dù cleanup này chạy lâu.
    """
    app_dir = PROJECT_DIR
    zip_path = os.path.join(app_dir, "update", "c69-router.zip")

    logger.info(f"[AutoUpdate] Đang tải bản mới từ {download_url} ...")
    _download(download_url, zip_path, expected_sha256)
    logger.info(f"[AutoUpdate] Tải xong: {zip_path} ({os.path.getsize(zip_path)/1024/1024:.1f} MB)")

    updater_exe = os.path.join(app_dir, UPDATER_EXE_NAME)
    if not os.path.exists(updater_exe):
        raise FileNotFoundError(
            f"Không tìm thấy {UPDATER_EXE_NAME} tại {updater_exe}. "
            f"Vui lòng tải lại bản đầy đủ từ cdn.c69.us"
        )

    pid = os.getpid()
    subprocess.Popen(
        [updater_exe, f"--pid={pid}", f"--zip={zip_path}", f"--exe={EXE_NAME}", f"--dir={app_dir}"],
        creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        close_fds=True,
        cwd=app_dir,
    )
    logger.warning(f"[AutoUpdate] Đã spawn {UPDATER_EXE_NAME} (đợi PID={pid} thoát). Đang dọn dẹp & thoát...")

    if on_before_exit:
        try:
            on_before_exit()
        except Exception as e:
            logger.error(f"[AutoUpdate] Cleanup trước khi thoát lỗi (không chặn update): {e}")

    import time
    time.sleep(1.0)  # Đảm bảo response HTTP của request /api/update/apply đã kịp flush
    os._exit(0)
