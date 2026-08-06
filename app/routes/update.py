"""
GenRouter v2.0 — Auto-Update Routes

GET  /api/update/status → trạng thái kiểm tra bản mới gần nhất (cho dashboard hiển thị banner)
POST /api/update/check  → ép kiểm tra lại ngay
POST /api/update/apply  → tải + cài bản mới, LUÔN cần người dùng chủ động bấm (xem
                           app/update_manager.py để biết vì sao không tự động áp dụng ngầm)
"""

import logging
import threading

from fastapi import APIRouter, HTTPException, Depends

from app import update_manager
from app.dependencies import get_dhcp_server, get_singbox_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/update")


@router.get("/status")
def get_update_status():
    """Trạng thái lần kiểm tra bản mới gần nhất (không tự gọi network ở đây — xem vòng lặp
    nền update_check_loop() trong main.py, chạy định kỳ mỗi vài tiếng)."""
    return update_manager.get_state()


@router.post("/check")
def check_update_now():
    """Ép kiểm tra bản mới ngay lập tức (vd người dùng bấm nút "Kiểm tra lại" trên dashboard)."""
    return update_manager.check_for_update()


@router.post("/apply")
def apply_update_now(
    dhcp_server=Depends(get_dhcp_server),
    singbox_manager=Depends(get_singbox_manager),
):
    """Tải bản mới nhất đã phát hiện và cài đặt — router sẽ THOÁT và tự khởi động lại
    (qua c69update.exe) trong vài giây. Yêu cầu đã có bản mới từ lần check gần nhất."""
    state = update_manager.get_state()
    if not state.get("available") or not state.get("download_url"):
        raise HTTPException(status_code=400, detail="Không có bản cập nhật nào để áp dụng.")

    def _cleanup_before_exit():
        # Dừng DHCP (nhả port 67) + sing-box (nhả TUN, khôi phục NAT) để thiết bị vẫn có
        # mạng qua NAT trực tiếp trong lúc chờ tiến trình mới khởi động lại.
        if dhcp_server:
            dhcp_server.stop()
        if singbox_manager:
            singbox_manager.stop()

    def _run():
        try:
            update_manager.apply_update_and_exit(state["download_url"], on_before_exit=_cleanup_before_exit)
        except Exception as e:
            logger.error(f"[AutoUpdate] Apply update thất bại: {e}")

    # Chạy nền: endpoint phải trả response cho client TRƯỚC khi tiến trình thoát hẳn.
    threading.Thread(target=_run, daemon=True, name="apply-update").start()

    return {
        "status": "success",
        "message": f"Đang cập nhật lên v{state['version']}. Router sẽ khởi động lại trong giây lát...",
    }
