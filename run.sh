#!/bin/bash
# run.sh — Chạy c69-router trực tiếp từ source code (Linux/macOS)
# Không cần build, không cần file .exe
#
# Cách dùng:
#   sudo bash run.sh            # chạy bình thường
#   sudo bash run.sh --dev      # chạy với auto-reload (development)
#   bash run.sh --install-deps  # cài dependencies rồi thoát

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Check root ────────────────────────────────────────────────
if [ "$EUID" -ne 0 ] && [ "$1" != "--install-deps" ]; then
    echo "[!] c69-router yêu cầu quyền root (cho TUN/iptables/NAT)."
    echo "    Chạy lại: sudo bash run.sh $*"
    exit 1
fi

# ── Setup venv nếu chưa có ───────────────────────────────────
if [ ! -d ".venv" ]; then
    echo "[*] Tạo virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

# ── Cài dependencies ─────────────────────────────────────────
if [ "$1" = "--install-deps" ] || [ ! -f ".venv/.deps_installed" ]; then
    echo "[*] Cài dependencies từ requirements.txt..."
    pip install -r requirements.txt -q
    touch .venv/.deps_installed
    echo "[*] Dependencies đã cài xong."
    if [ "$1" = "--install-deps" ]; then exit 0; fi
fi

# ── Enable IP forwarding ─────────────────────────────────────
PLATFORM="$(uname)"
if [ "$PLATFORM" = "Linux" ]; then
    sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || true
elif [ "$PLATFORM" = "Darwin" ]; then
    sysctl -w net.inet.ip.forwarding=1 >/dev/null 2>&1 || true
fi

# ── Chạy app ─────────────────────────────────────────────────
echo ""
echo "========================================"
echo "  c69-router — $(uname -s) $(uname -m)"
echo "  Web UI: http://localhost:9000"
echo "========================================"
echo ""

if [ "$1" = "--dev" ]; then
    # Dev mode: auto-reload khi thay đổi code
    exec python3 -m uvicorn app.main:app \
        --host 0.0.0.0 --port 9000 \
        --reload --reload-dir app
else
    exec python3 -m uvicorn app.main:app \
        --host 0.0.0.0 --port 9000
fi
