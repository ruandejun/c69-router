"""
c69update.exe — Standalone updater (dùng chung cho C69Automation và C69 Router).

Hiển thị progress bar UI (tkinter), chạy độc lập ngoài tiến trình chính.
Đợi main process kết thúc → giải nén → thay exe → khởi động lại.

Generic theo thiết kế: không hardcode tên app nào — tất cả thông tin (PID cần đợi, file
zip, tên exe cần thay, thư mục cài đặt) đều truyền qua CLI args, nên cùng 1 file này build
ra dùng được cho bất kỳ app đóng gói PyInstaller nào cần tự cập nhật.

Usage (được spawn bởi app chính, ví dụ C69Automation.exe hoặc c69-router.exe):
    c69update.exe --pid=<pid> --zip=<zip_path> --exe=<exe_name> --dir=<app_dir>
"""

import sys
import os
import time
import zipfile
import subprocess
import argparse
import ctypes
import threading
import tkinter as tk
from tkinter import ttk


# ─── Dark theme colors ──────────────────────────────────────────────────────
BG        = "#0d1117"
BG_CARD   = "#161b22"
ACCENT    = "#3b82f6"
TEXT      = "#e2e8f0"
TEXT_MUTED= "#718096"
BORDER    = "#30363d"
SUCCESS   = "#22c55e"
ERROR     = "#ef4444"


class UpdaterWindow:
    """Cửa sổ progress bar đơn giản cho quá trình cập nhật."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("C69 Updater")
        self.root.geometry("460x220")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)
        self.root.overrideredirect(True)   # Frameless window

        # Căn giữa màn hình
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x  = (sw - 460) // 2
        y  = (sh - 220) // 2
        self.root.geometry(f"460x220+{x}+{y}")

        self._build_ui()
        self._make_draggable()

    def _build_ui(self):
        # Outer border frame
        outer = tk.Frame(self.root, bg=BORDER, padx=1, pady=1)
        outer.pack(fill="both", expand=True)

        card = tk.Frame(outer, bg=BG_CARD, padx=28, pady=20)
        card.pack(fill="both", expand=True)

        # Header row
        header = tk.Frame(card, bg=BG_CARD)
        header.pack(fill="x", pady=(0, 4))

        tk.Label(
            header, text="🚀  C69", bg=BG_CARD,
            fg=ACCENT, font=("Segoe UI", 14, "bold")
        ).pack(side="left")

        # Close button (only close after done)
        self._close_btn = tk.Label(
            header, text="✕", bg=BG_CARD, fg=TEXT_MUTED,
            font=("Segoe UI", 12), cursor="hand2"
        )
        self._close_btn.pack(side="right")
        self._close_btn.bind("<Button-1>", lambda e: None)  # Disabled until done

        # Subtitle
        tk.Label(
            card, text="Đang cập nhật phần mềm...", bg=BG_CARD,
            fg=TEXT_MUTED, font=("Segoe UI", 10)
        ).pack(anchor="w", pady=(0, 16))

        # Step label
        self.step_var = tk.StringVar(value="⏳  Đang khởi động updater...")
        tk.Label(
            card, textvariable=self.step_var, bg=BG_CARD,
            fg=TEXT, font=("Segoe UI", 10, "bold"), anchor="w"
        ).pack(fill="x")

        # Progress bar
        style = ttk.Style()
        style.theme_use("default")
        style.configure(
            "C69.Horizontal.TProgressbar",
            troughcolor=BG, background=ACCENT,
            bordercolor=BG, lightcolor=ACCENT, darkcolor=ACCENT,
            thickness=8
        )
        self.progress = ttk.Progressbar(
            card, style="C69.Horizontal.TProgressbar",
            orient="horizontal", length=400, mode="indeterminate"
        )
        self.progress.pack(fill="x", pady=(10, 8))
        self.progress.start(12)

        # Status detail
        self.status_var = tk.StringVar(value="")
        tk.Label(
            card, textvariable=self.status_var, bg=BG_CARD,
            fg=TEXT_MUTED, font=("Segoe UI", 9), anchor="w"
        ).pack(fill="x")

    def _make_draggable(self):
        def on_press(e):  self._x, self._y = e.x, e.y
        def on_drag(e):   self.root.geometry(f"+{self.root.winfo_x()+e.x-self._x}+{self.root.winfo_y()+e.y-self._y}")
        self.root.bind("<ButtonPress-1>", on_press)
        self.root.bind("<B1-Motion>", on_drag)

    def set_step(self, msg: str):
        self.step_var.set(msg)
        self.root.update_idletasks()

    def set_status(self, msg: str):
        self.status_var.set(msg)
        self.root.update_idletasks()

    def set_progress_determinate(self, value: int):
        """Chuyển sang mode xác định (0-100)."""
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100)
        self.progress["value"] = value
        self.root.update_idletasks()

    def set_done(self, success: bool):
        if success:
            self.step_var.set("✅  Cập nhật thành công!")
            self.status_var.set("Đang khởi động lại...")
            self.progress["value"] = 100
        else:
            self.step_var.set("❌  Cập nhật thất bại!")
            self.status_var.set("Vui lòng tải lại thủ công từ cdn.c69.us")
            style = ttk.Style()
            style.configure("C69.Horizontal.TProgressbar", background=ERROR)
        # Bật nút X để đóng
        self._close_btn.bind("<Button-1>", lambda e: self.root.destroy())
        self._close_btn.configure(fg=TEXT)
        self.root.update_idletasks()

    def close_after(self, ms: int):
        self.root.after(ms, self.root.destroy)

    def run(self):
        self.root.mainloop()


# ─── Core update logic ───────────────────────────────────────────────────────

def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[c69update {ts}] {msg}", flush=True)


def wait_for_process_exit(pid: int, timeout: int, window: UpdaterWindow) -> bool:
    try:
        import psutil
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return True
        deadline = time.time() + timeout
        elapsed  = 0
        while time.time() < deadline:
            try:
                proc.status()
            except psutil.NoSuchProcess:
                return True
            window.set_status(f"Đợi ứng dụng đóng... ({int(time.time()-deadline+timeout)}s)")
            time.sleep(0.4)
        return False
    except ImportError:
        # Fallback ctypes
        SYNCHRONIZE  = 0x00100000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            ec = ctypes.c_ulong(0)
            k32.GetExitCodeProcess(handle, ctypes.byref(ec))
            if ec.value != STILL_ACTIVE:
                k32.CloseHandle(handle)
                return True
            window.set_status(f"Đợi ứng dụng đóng... ({int(time.time()-deadline+timeout)}s)")
            time.sleep(0.5)
        k32.CloseHandle(handle)
        return False


def perform_update(zip_path: str, app_dir: str, exe_name: str,
                   window: UpdaterWindow) -> bool:
    cur_exe = os.path.join(app_dir, exe_name)
    old_exe = cur_exe + ".old"
    new_exe = cur_exe + ".new"

    # Giải nén exe mới
    window.set_step("📦  Đang giải nén file cập nhật...")
    window.set_progress_determinate(10)
    log(f"Giải nén {zip_path}...")

    try:
        zip_size = os.path.getsize(zip_path)
        extracted = [0]

        with zipfile.ZipFile(zip_path, "r") as z:
            exe_found = False
            for info in z.infolist():
                if info.filename.lower().endswith(exe_name.lower()):
                    log(f"  Tìm thấy: {info.filename} ({info.file_size/1024/1024:.1f} MB)")
                    window.set_status(f"Giải nén {exe_name} ({info.file_size/1024/1024:.0f} MB)...")
                    # Đọc từng chunk để cập nhật progress
                    total = info.file_size
                    done  = 0
                    with z.open(info) as src, open(new_exe, "wb") as dst:
                        while True:
                            chunk = src.read(65536)
                            if not chunk:
                                break
                            dst.write(chunk)
                            done += len(chunk)
                            pct = 10 + int(done / total * 60) if total else 50
                            window.set_progress_determinate(pct)
                    exe_found = True
                    break

            if not exe_found:
                log(f"LỖI: Không tìm thấy {exe_name} trong zip!")
                return False
    except Exception as e:
        log(f"LỖI giải nén: {e}")
        window.set_status(f"Lỗi giải nén: {e}")
        return False

    # Xóa backup cũ
    window.set_step("🔄  Đang cài đặt phiên bản mới...")
    window.set_progress_determinate(72)
    if os.path.exists(old_exe):
        try: os.remove(old_exe)
        except Exception: pass

    # Backup exe cũ
    if os.path.exists(cur_exe):
        try:
            os.rename(cur_exe, old_exe)
            log(f"Backup: {exe_name} → {os.path.basename(old_exe)}")
            window.set_status(f"Đã backup bản cũ...")
        except Exception as e:
            log(f"LỖI backup: {e}")
            try: os.remove(new_exe)
            except Exception: pass
            return False

    # Cài exe mới
    window.set_progress_determinate(85)
    try:
        os.rename(new_exe, cur_exe)
        log(f"Đã cài: {exe_name}")
    except Exception as e:
        log(f"LỖI cài exe mới: {e}")
        try: os.rename(old_exe, cur_exe)
        except Exception: pass
        return False

    # Dọn dẹp
    window.set_progress_determinate(95)
    for f in [old_exe, zip_path]:
        try:
            if os.path.exists(f): os.remove(f)
        except Exception: pass

    window.set_progress_determinate(100)
    return True


def update_worker(args, window: UpdaterWindow):
    """Chạy trong thread riêng để không block UI."""
    try:
        # Bước 1: Đợi main process kết thúc
        window.set_step("⏳  Đợi ứng dụng đóng hoàn toàn...")
        if not wait_for_process_exit(args.pid, timeout=30, window=window):
            log("Timeout đợi process — tiếp tục...")

        time.sleep(1)  # Đảm bảo file lock đã release

        # Bước 2: Thực hiện cập nhật
        success = perform_update(args.zip, args.dir, args.exe, window)

        # Bước 3: Thông báo kết quả
        window.root.after(0, lambda: window.set_done(success))

        if success:
            log("✅ Cập nhật thành công!")
            time.sleep(2)
            exe_path = os.path.join(args.dir, args.exe)
            try:
                subprocess.Popen([exe_path], cwd=args.dir)
                log(f"Đã khởi động: {exe_path}")
            except Exception as e:
                log(f"Lỗi khởi động: {e}")
                try: os.startfile(exe_path)
                except Exception: pass
            # Đóng window sau 3 giây
            window.root.after(3000, window.root.destroy)
        else:
            log("❌ Cập nhật thất bại!")
            # Hiện messagebox sau khi window đóng (nếu user bấm X)
            ctypes.windll.user32.MessageBoxW(
                0,
                "Cập nhật thất bại!\nVui lòng tải lại thủ công từ https://cdn.c69.us",
                "C69 Updater — Lỗi",
                0x10
            )
            window.root.after(0, window.root.destroy)

    except Exception as e:
        log(f"LỖI không xác định: {e}")
        window.root.after(0, window.root.destroy)


def main():
    parser = argparse.ArgumentParser(description="C69 Updater")
    parser.add_argument("--pid",  type=int, required=True,  help="PID of the running app to wait for")
    parser.add_argument("--zip",  type=str, required=True,  help="Path to downloaded zip file")
    parser.add_argument("--exe",  type=str, required=True,  help="Exe filename to replace")
    parser.add_argument("--dir",  type=str, required=True,  help="App directory")
    args = parser.parse_args()

    log(f"=== C69 Updater | PID={args.pid} | {args.exe} ===")

    # Tạo UI window
    window = UpdaterWindow()

    # Chạy logic cập nhật trong thread riêng (không block UI)
    t = threading.Thread(target=update_worker, args=(args, window), daemon=True)
    t.start()

    # Chạy UI loop (blocking)
    window.run()
    sys.exit(0)


if __name__ == "__main__":
    main()
