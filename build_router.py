"""
build_router.py — Cross-platform build, zip, upload cho c69-router

Dùng chung 1 script cho mọi OS:
  Windows  → dist/c69-router.exe  → c69-router.zip    → R2: c69-router.zip
  Linux    → dist/c69-router      → c69-router.zip    → R2: c69-router-linux.zip
  macOS    → dist/c69-router      → c69-router.zip    → R2: c69-router-macos.zip

Cách chạy:
  python build_router.py           # build + upload R2
  python build_router.py --local   # build only, no upload
"""
import hashlib
import json
import os
import re
import sys
import platform
import subprocess
import zipfile
import time
import shutil

_PLATFORM = platform.system()   # 'Windows' | 'Linux' | 'Darwin'
_IS_WIN   = _PLATFORM == "Windows"
_IS_MAC   = _PLATFORM == "Darwin"
_IS_LIN   = _PLATFORM == "Linux"

# ── Binary names ──────────────────────────────────────────────
EXE_NAME        = "c69-router.exe"     if _IS_WIN else "c69-router"
UPDATER_NAME    = "c69update.exe"      if _IS_WIN else "c69update"
DIST_EXE        = os.path.join("dist", EXE_NAME)
DIST_UPDATER    = os.path.join("dist", UPDATER_NAME)

# ── R2 ZIP key per platform ───────────────────────────────────
if _IS_WIN:
    ZIP_KEY = "c69-router.zip"          # Windows: giữ tên cũ để tương thích
elif _IS_MAC:
    ZIP_KEY = "c69-router-macos.dmg"    # macOS: .dmg cho drag-and-drop install
else:
    ZIP_KEY = "c69-router-linux.zip"    # Linux: .zip

ZIP_PATH = os.path.join("dist", ZIP_KEY)
APP_BUNDLE = os.path.join("dist", "c69-router.app")  # macOS only

# ── Cloudflare R2 config (đọc từ .env) ───────────────────────
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

CLOUDFLARE_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CLOUDFLARE_API_TOKEN  = os.environ.get("CF_API_TOKEN", "")
R2_BUCKET_NAME        = os.environ.get("CF_R2_BUCKET", "c69-releases")
R2_PUBLIC_URL         = os.environ.get("CF_R2_PUBLIC_URL", "")

FALLBACK_SFTP_HOST = os.environ.get("C69_FALLBACK_SFTP_HOST", "")
FALLBACK_SFTP_USER = os.environ.get("C69_FALLBACK_SFTP_USER", "")
FALLBACK_SFTP_PASS = os.environ.get("C69_FALLBACK_SFTP_PASS", "")
FALLBACK_REMOTE_PATH = os.environ.get("C69_FALLBACK_REMOTE_PATH", "")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding='utf-8')


# ══════════════════════════════════════════════════════════════
# STEP 1: Kill running process (prevent PermissionError)
# ══════════════════════════════════════════════════════════════

def kill_running():
    print(f"=== [{_PLATFORM}] Kill running c69-router before build ===")
    if _IS_WIN:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process c69-router -ErrorAction SilentlyContinue | Stop-Process -Force; "
             "Start-Sleep -Milliseconds 1500"],
            shell=True, capture_output=True
        )
    else:
        subprocess.run(["pkill", "-f", "c69-router"], capture_output=True)
        time.sleep(0.5)

    if os.path.exists(DIST_EXE):
        try:
            os.remove(DIST_EXE)
            print(f"  Deleted: {DIST_EXE}")
        except Exception as e:
            print(f"  Warning: Could not delete {DIST_EXE}: {e}")


# ══════════════════════════════════════════════════════════════
# STEP 2: Find PyInstaller binary
# ══════════════════════════════════════════════════════════════

def find_pyinstaller():
    if _IS_WIN:
        candidates = [
            os.path.join(".venv", "Scripts", "pyinstaller.exe"),
            "pyinstaller",
        ]
    else:
        candidates = [
            os.path.join(".venv", "bin", "pyinstaller"),
            os.path.join(os.path.expanduser("~"), ".local", "bin", "pyinstaller"),
            "pyinstaller",
        ]
    for c in candidates:
        if os.path.exists(c) or c == "pyinstaller":
            return c
    return "pyinstaller"


# ══════════════════════════════════════════════════════════════
# STEP 3: PyInstaller build
# ══════════════════════════════════════════════════════════════

def build_main(pyinstaller_bin: str):
    print(f"\n=== [{_PLATFORM}] Build c69-router → {EXE_NAME} ===")
    cmd = [pyinstaller_bin, "c69-router.spec", "--clean"]
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, shell=_IS_WIN)
    if result.returncode != 0:
        print("PyInstaller build FAILED!")
        sys.exit(1)
    print(f"=== Build successful: {DIST_EXE} ===")

    # Set executable bit on Linux/macOS
    if not _IS_WIN and os.path.exists(DIST_EXE):
        os.chmod(DIST_EXE, 0o755)


def build_updater(pyinstaller_bin: str):
    """Build c69update helper (Windows only for now — updater is PE-aware)."""
    updater_spec = "c69update.spec"
    if not os.path.exists(updater_spec):
        print(f"  [skip] {updater_spec} not found — skipping updater build.")
        return

    print(f"\n=== [{_PLATFORM}] Build c69update → {UPDATER_NAME} ===")
    cmd = [pyinstaller_bin, updater_spec, "--clean"]
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, shell=_IS_WIN)
    if result.returncode != 0:
        print("c69update build FAILED!")
        sys.exit(1)
    print(f"=== Updater build successful: {DIST_UPDATER} ===")

    if not _IS_WIN and os.path.exists(DIST_UPDATER):
        os.chmod(DIST_UPDATER, 0o755)


# ══════════════════════════════════════════════════════════════
# STEP 4: Zip
# ══════════════════════════════════════════════════════════════

def make_package():
    """Create distributable package:
      Windows  → .zip  (exe + updater + setup scripts)
      Linux    → .zip  (binary + setup_linux.sh)
      macOS    → .dmg  (drag-and-drop .app bundle)
    """
    if _IS_MAC:
        _make_dmg()
    else:
        _make_zip()


def _make_zip():
    """Windows / Linux: create zip archive."""
    # Check both dist/ and dist_output/
    global DIST_EXE
    if not os.path.exists(DIST_EXE) and os.path.exists(os.path.join("dist_output", EXE_NAME)):
        DIST_EXE = os.path.join("dist_output", EXE_NAME)

    if not os.path.exists(DIST_EXE):
        print(f"ERROR: Binary not found at {DIST_EXE}")
        sys.exit(1)

    # Sync to release folder
    os.makedirs("release", exist_ok=True)
    os.makedirs("dist", exist_ok=True)
    os.makedirs("dist_output", exist_ok=True)
    try:
        shutil.copy2(DIST_EXE, os.path.join("dist", EXE_NAME))
        shutil.copy2(DIST_EXE, os.path.join("dist_output", EXE_NAME))
        shutil.copy2(DIST_EXE, os.path.join("release", "c69-router-v2.1.2.exe"))
    except Exception as e:
        print(f"  Notice copying exe: {e}")

    print(f"\n=== Zipping → {ZIP_PATH} ===")
    with zipfile.ZipFile(ZIP_PATH, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.write(DIST_EXE, EXE_NAME)

        updater_src = DIST_UPDATER if os.path.exists(DIST_UPDATER) else os.path.join("release", UPDATER_NAME)
        if os.path.exists(updater_src):
            zf.write(updater_src, UPDATER_NAME)

        if _IS_WIN:
            for f in ("setup_windows.bat", "setup_windows.ps1", "mihomo.exe", "wintun.dll", "geoip.metadb"):
                if os.path.exists(f):
                    zf.write(f, f)
        elif _IS_LIN:
            if os.path.exists("setup_linux.sh"):
                zf.write("setup_linux.sh", "setup_linux.sh")

    # Also copy zip to dist_output
    try:
        shutil.copy2(ZIP_PATH, os.path.join("dist_output", ZIP_KEY))
    except Exception:
        pass

    size_mb = os.path.getsize(ZIP_PATH) / 1024 / 1024
    print(f"=== Zip OK: {ZIP_PATH} ({size_mb:.1f} MB) ===")


def _make_dmg():
    """macOS: create drag-and-drop DMG from .app bundle using hdiutil (built-in)."""
    if not os.path.exists(APP_BUNDLE):
        print(f"ERROR: .app bundle not found at {APP_BUNDLE}")
        print("  Make sure the build succeeded and c69-router.app is in dist/")
        sys.exit(1)

    dmg_path = ZIP_PATH  # c69-router-macos.dmg
    tmp_dmg  = dmg_path.replace(".dmg", "-tmp.dmg")

    # Clean up old files
    for p in (dmg_path, tmp_dmg):
        if os.path.exists(p):
            os.remove(p)

    print(f"\n=== Creating macOS DMG → {dmg_path} ===")

    # Step 1: Create a writable DMG containing the .app
    # Volume size: app size + 20MB buffer
    app_size_mb = int(sum(
        os.path.getsize(os.path.join(root, f))
        for root, _, files in os.walk(APP_BUNDLE)
        for f in files
    ) / 1024 / 1024) + 30

    r = subprocess.run([
        "hdiutil", "create",
        "-size", f"{app_size_mb}m",
        "-volname", "c69-Router",
        "-srcfolder", APP_BUNDLE,
        "-ov", "-format", "UDRW",
        tmp_dmg,
    ], capture_output=True, text=True)

    if r.returncode != 0:
        print(f"hdiutil create failed: {r.stderr}")
        sys.exit(1)

    # Step 2: Convert to compressed read-only DMG (UDZO)
    r2 = subprocess.run([
        "hdiutil", "convert", tmp_dmg,
        "-format", "UDZO",
        "-o", dmg_path,
    ], capture_output=True, text=True)

    if r2.returncode != 0:
        print(f"hdiutil convert failed: {r2.stderr}")
        sys.exit(1)

    # Cleanup temp
    if os.path.exists(tmp_dmg):
        os.remove(tmp_dmg)

    size_mb = os.path.getsize(dmg_path) / 1024 / 1024
    print(f"=== DMG OK: {dmg_path} ({size_mb:.1f} MB) ===")
    print(f"  Users: double-click DMG → drag c69-Router.app to Applications")


# ══════════════════════════════════════════════════════════════
# STEP 5: Upload to R2
# ══════════════════════════════════════════════════════════════

def upload_to_r2(zip_path: str) -> str:
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        print("  [R2] No CF credentials in .env — skipping R2 upload.")
        return ""

    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        print("  [R2] Installing boto3...")
        os.system(f"{sys.executable} -m pip install boto3 -q")
        import boto3
        from botocore.config import Config

    endpoint = f"https://{CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"
    tok = CLOUDFLARE_API_TOKEN
    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=tok.split(":")[0] if ":" in tok else tok,
        aws_secret_access_key=tok.split(":")[1] if ":" in tok else tok,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )

    file_size = os.path.getsize(zip_path)
    print(f"\n=== Upload lên Cloudflare R2 [{R2_BUCKET_NAME}] ===")
    print(f"  File: {os.path.basename(zip_path)} ({file_size / 1024 / 1024:.1f} MB)")
    print(f"  Key:  {ZIP_KEY}")

    uploaded = [0]
    start_time = [time.time()]

    def progress(bytes_transferred):
        uploaded[0] += bytes_transferred
        pct   = uploaded[0] / file_size * 100
        speed = uploaded[0] / (time.time() - start_time[0] + 0.001) / 1024 / 1024
        print(
            f"\r  {uploaded[0]/1024/1024:.1f} MB / {file_size/1024/1024:.1f} MB "
            f"({pct:.0f}%) — {speed:.1f} MB/s",
            end="", flush=True
        )

    s3.upload_file(
        zip_path, R2_BUCKET_NAME, ZIP_KEY,
        Callback=progress,
        ExtraArgs={
            "ContentType": "application/zip",
            "CacheControl": "no-cache, no-store, must-revalidate, max-age=0",
        },
    )
    with open(zip_path, "rb") as package_file:
        sha256 = hashlib.sha256(package_file.read()).hexdigest()
    try:
        from app.version import VERSION as _app_ver
    except Exception:
        _app_ver = "2.1.14"
    app_version = os.environ.get("C69_ROUTER_VERSION", "") or _app_ver
    manifest = {
        "version": app_version,
        "download_url": f"{R2_PUBLIC_URL.rstrip('/')}/{ZIP_KEY}" if R2_PUBLIC_URL else "",
        "sha256": sha256,
        "changelog": f"Cập nhật v{app_version}: Fix Hotspot DHCP và thêm rules bypass Dashboard 192.168.137.1:9000."
    }
    if manifest["version"] and not re.fullmatch(r"\d+(?:\.\d+){1,3}", manifest["version"]):
        raise ValueError("C69_ROUTER_VERSION must be a semantic version such as 2.1.1")
    if manifest["version"] and manifest["download_url"]:
        manifest_key = f"{ZIP_KEY}.json"
        s3.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=manifest_key,
            Body=json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
            CacheControl="no-cache, no-store, must-revalidate, max-age=0",
        )
        print(f"  Manifest SHA-256 uploaded: {manifest_key}")
    print(f"\n  Upload hoàn tất!")
    return f"{R2_PUBLIC_URL.rstrip('/')}/{ZIP_KEY}" if R2_PUBLIC_URL else f"r2://{R2_BUCKET_NAME}/{ZIP_KEY}"


def upload_to_sftp(zip_path: str) -> str:
    if not all([FALLBACK_SFTP_HOST, FALLBACK_SFTP_USER, FALLBACK_SFTP_PASS, FALLBACK_REMOTE_PATH]):
        print("  [SFTP] Fallback credentials are not configured — skipping SFTP upload.")
        return ""
    try:
        import paramiko
    except ImportError:
        print("  [SFTP] paramiko not installed. Run: pip install paramiko")
        return ""

    print(f"\n=== Fallback SFTP: {FALLBACK_SFTP_HOST} ===")
    filename = os.path.basename(zip_path)
    remote_path = f"{FALLBACK_REMOTE_PATH}/{filename}"

    def progress(transferred, total):
        pct = transferred / total * 100
        print(f"\r  {transferred/1024/1024:.2f} MB / {total/1024/1024:.2f} MB ({pct:.1f}%)", end='', flush=True)

    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    try:
        ssh.connect(FALLBACK_SFTP_HOST, username=FALLBACK_SFTP_USER,
                    password=FALLBACK_SFTP_PASS, timeout=30)
        ssh.exec_command(f"mkdir -p {FALLBACK_REMOTE_PATH}")
        sftp = ssh.open_sftp()
        sftp.put(zip_path, remote_path, callback=progress)
        sftp.close()
        print(f"\n=== SFTP upload OK ===")
        return f"https://c69.us/static/{filename}"
    except Exception as e:
        print(f"\n  SFTP failed: {e}")
        return ""
    finally:
        ssh.close()


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def build():
    local_only = "--local" in sys.argv

    _pkg_label = "DMG (.app bundle)" if _IS_MAC else "ZIP"
    print(f"\n{'='*60}")
    print(f"  c69-router Build Script")
    print(f"  Platform : {_PLATFORM} ({'64-bit' if sys.maxsize > 2**32 else '32-bit'})")
    print(f"  Binary   : {EXE_NAME}")
    print(f"  Package  : {ZIP_KEY}  [{_pkg_label}]")
    print(f"  Mode     : {'LOCAL (no upload)' if local_only else 'BUILD + UPLOAD'}")
    print(f"{'='*60}\n")

    pyinstaller_bin = find_pyinstaller()

    kill_running()
    build_main(pyinstaller_bin)
    build_updater(pyinstaller_bin)
    make_package()

    if local_only:
        print(f"\n=== [--local] Done. Package at: {ZIP_PATH} ===")
        return

    url = upload_to_r2(ZIP_PATH)
    if not url:
        print("\n=== R2 unavailable, trying SFTP fallback ===")
        url = upload_to_sftp(ZIP_PATH)

    if url:
        print(f"\n{'='*60}")
        print(f"  DONE — Download URL:")
        print(f"  {url}")
        if _IS_MAC:
            print(f"  Users: download .dmg → double-click → drag to Applications")
        print(f"{'='*60}")
    else:
        print("\n=== ERROR: Both R2 and SFTP failed ===")
        sys.exit(1)


if __name__ == "__main__":
    build()
