"""
build_router.py — Build, nén và upload c69-router lên Cloudflare R2 CDN
(fallback: SFTP lên server riêng nếu R2 chưa cấu hình hoặc upload lỗi).

Dùng chung Cloudflare R2 bucket với QHTDautomation (đọc từ .env, xem
d:\\Workspace\\Python\\QHTDautomation\\build_and_deploy.py để biết cách setup R2).
"""
import os
import sys
import subprocess
import zipfile
import time

# Tự động load biến từ .env cùng thư mục (chứa CF_ACCOUNT_ID/CF_API_TOKEN/CF_R2_BUCKET/CF_R2_PUBLIC_URL)
_here = os.path.dirname(os.path.abspath(__file__))
_env_path = os.path.join(_here, ".env")
try:
    from dotenv import load_dotenv
    if os.path.exists(_env_path):
        load_dotenv(dotenv_path=_env_path)
except ImportError:
    if os.path.exists(_env_path):
        with open(_env_path) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith('#') and '=' in _line:
                    _k, _v = _line.split('=', 1)
                    os.environ.setdefault(_k.strip(), _v.strip())

sys.stdout.reconfigure(encoding='utf-8')

# ============================================================
# CONFIG
# ============================================================
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CLOUDFLARE_API_TOKEN = os.environ.get("CF_API_TOKEN", "")
R2_BUCKET_NAME = os.environ.get("CF_R2_BUCKET", "c69-releases")
R2_PUBLIC_URL = os.environ.get("CF_R2_PUBLIC_URL", "")

# Fallback: server riêng qua SFTP (dùng khi chưa có R2 config hoặc R2 upload lỗi)
FALLBACK_SFTP_HOST = "167.233.89.198"
FALLBACK_SFTP_USER = "root"
FALLBACK_SFTP_PASS = "fJU9JtkbELfi"
FALLBACK_REMOTE_PATH = "/root/storagon/static"

ZIP_FILENAME = "c69-router.zip"


def build():
    pyinstaller_bin = os.path.join(".venv", "Scripts", "pyinstaller.exe")
    if not os.path.exists(pyinstaller_bin):
        pyinstaller_bin = "pyinstaller"

    print("=== Bắt đầu chạy PyInstaller cho c69-router ===")
    cmd = [pyinstaller_bin, "c69-router.spec", "--clean"]
    print(f"Running command: {' '.join(cmd)}")
    result = subprocess.run(cmd, shell=True)
    if result.returncode != 0:
        print("PyInstaller build failed!")
        sys.exit(1)
    print("=== PyInstaller Build Successful! ===")

    # c69update.exe: helper tự-cập-nhật, dùng chung cơ chế với C69Automation (đợi PID cha
    # thoát rồi thay exe) — build lại cùng lúc để luôn kèm theo trong zip release, không
    # thì router tải bản mới về nhưng không cài được (thiếu updater).
    print("=== Bắt đầu chạy PyInstaller cho c69update (updater helper) ===")
    cmd_updater = [pyinstaller_bin, "c69update.spec", "--clean"]
    print(f"Running command: {' '.join(cmd_updater)}")
    result_updater = subprocess.run(cmd_updater, shell=True)
    if result_updater.returncode != 0:
        print("PyInstaller build failed (c69update)!")
        sys.exit(1)
    print("=== PyInstaller Build Successful (c69update)! ===")

    exe_path = os.path.join("dist", "c69-router.exe")
    updater_path = os.path.join("dist", "c69update.exe")
    zip_path = os.path.join("dist", ZIP_FILENAME)

    if not os.path.exists(exe_path):
        print(f"Lỗi: Không tìm thấy file thực thi tại {exe_path}")
        sys.exit(1)
    if not os.path.exists(updater_path):
        print(f"Lỗi: Không tìm thấy updater tại {updater_path}")
        sys.exit(1)

    print(f"Đang nén {exe_path} thành {zip_path}...")
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(exe_path, "c69-router.exe")
            zipf.write(updater_path, "c69update.exe")
            # Kèm script setup máy mới — dù giờ c69-router.exe đã tự động sửa
            # BFE/Firewall/ICS lúc khởi động, vẫn giữ để chẩn đoán thủ công nếu cần.
            for setup_file in ("setup_windows.bat", "setup_windows.ps1"):
                if os.path.exists(setup_file):
                    zipf.write(setup_file, setup_file)
        print("=== Nén file thành công! ===")
    except Exception as e:
        print(f"Lỗi khi nén file: {e}")
        sys.exit(1)

    url = upload_to_r2(zip_path)
    if not url:
        print("\n=== R2 upload không khả dụng, chuyển sang SFTP (fallback) ===")
        url = upload_to_server_sftp(zip_path)

    if url:
        print(f"\n=== HOÀN TẤT — URL tải: {url} ===")
    else:
        print("\n=== LỖI: Cả R2 lẫn SFTP đều thất bại ===")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────
# CLOUDFLARE R2 UPLOAD (S3-compatible API)
# ─────────────────────────────────────────────────────────────

def _r2_upload_boto3(zip_path: str):
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        print("  [R2] boto3 chưa cài. Đang cài: pip install boto3...")
        os.system(f"{sys.executable} -m pip install boto3 -q")
        import boto3
        from botocore.config import Config

    endpoint = f"https://{CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"
    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=CLOUDFLARE_API_TOKEN.split(":")[0] if ":" in CLOUDFLARE_API_TOKEN else CLOUDFLARE_API_TOKEN,
        aws_secret_access_key=CLOUDFLARE_API_TOKEN.split(":")[1] if ":" in CLOUDFLARE_API_TOKEN else CLOUDFLARE_API_TOKEN,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )

    file_size = os.path.getsize(zip_path)
    print(f"  Bắt đầu upload lên R2 bucket [{R2_BUCKET_NAME}]...")
    print(f"  File: {os.path.basename(zip_path)} ({file_size / 1024 / 1024:.1f} MB)")

    uploaded = [0]
    start_time = [time.time()]

    def progress(bytes_transferred):
        uploaded[0] += bytes_transferred
        pct = uploaded[0] / file_size * 100
        speed = uploaded[0] / (time.time() - start_time[0] + 0.001) / 1024 / 1024
        print(
            f"\r  {uploaded[0]/1024/1024:.1f} MB / {file_size/1024/1024:.1f} MB "
            f"({pct:.0f}%) — {speed:.1f} MB/s",
            end="", flush=True
        )

    s3.upload_file(
        zip_path,
        R2_BUCKET_NAME,
        ZIP_FILENAME,
        Callback=progress,
        ExtraArgs={"ContentType": "application/zip"},
    )
    print(f"\n  Upload hoàn tất!")

    return f"{R2_PUBLIC_URL.rstrip('/')}/{ZIP_FILENAME}" if R2_PUBLIC_URL else None


def upload_to_r2(zip_path: str):
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        print("  [R2] Chưa cấu hình CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN trong .env.")
        return None

    print("=== Upload lên Cloudflare R2 ===")
    try:
        return _r2_upload_boto3(zip_path)
    except Exception as e:
        print(f"\n  [R2] Upload thất bại: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# FALLBACK: Upload lên server riêng qua SFTP
# ─────────────────────────────────────────────────────────────

def upload_to_server_sftp(zip_path: str):
    import paramiko
    print(f"=== Fallback: Upload SFTP lên {FALLBACK_SFTP_HOST} ===")

    filename = os.path.basename(zip_path)
    remote_path = f"{FALLBACK_REMOTE_PATH}/{filename}"

    def upload_progress(transferred, total):
        percentage = (transferred / total) * 100
        print(f"\rUploading: {transferred / (1024*1024):.2f}MB / {total / (1024*1024):.2f}MB ({percentage:.1f}%)", end='', flush=True)

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        print(f"Đang kết nối tới {FALLBACK_SFTP_HOST}...")
        ssh.connect(FALLBACK_SFTP_HOST, username=FALLBACK_SFTP_USER, password=FALLBACK_SFTP_PASS, timeout=30)
        print("Kết nối thành công!")

        ssh.exec_command(f"mkdir -p {FALLBACK_REMOTE_PATH}")

        sftp = ssh.open_sftp()
        print(f"Bắt đầu upload SFTP tới {remote_path}...")
        sftp.put(zip_path, remote_path, callback=upload_progress)
        sftp.close()
        print("\n=== Tải lên thành công! ===")
        return f"https://c69.us/static/{filename}"
    except Exception as e:
        print(f"\nLỗi khi upload file: {e}")
        return None
    finally:
        ssh.close()


if __name__ == "__main__":
    build()
