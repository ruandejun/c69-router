#!/bin/bash
# setup_linux.sh — Linux setup script for c69-router
# Installs dependencies and configures the system

set -e

echo "=================================================="
echo "  c69-router Linux Setup"
echo "=================================================="

# Check root
if [ "$EUID" -ne 0 ]; then
    echo "[!] Please run as root: sudo bash setup_linux.sh"
    exit 1
fi

# Detect distro
if command -v apt-get &>/dev/null; then
    PKG_MGR="apt-get"
    echo "[*] Detected: Debian/Ubuntu"
elif command -v yum &>/dev/null; then
    PKG_MGR="yum"
    echo "[*] Detected: CentOS/RHEL"
elif command -v dnf &>/dev/null; then
    PKG_MGR="dnf"
    echo "[*] Detected: Fedora"
else
    echo "[!] Unsupported distro. Install manually: iptables, hostapd, iproute2"
fi

echo ""
echo "[1] Installing dependencies..."
if [ "$PKG_MGR" = "apt-get" ]; then
    apt-get update -q
    apt-get install -y -q iptables iproute2 hostapd dnsmasq curl wget
elif [ "$PKG_MGR" = "yum" ] || [ "$PKG_MGR" = "dnf" ]; then
    $PKG_MGR install -y iptables iproute hostapd dnsmasq curl wget
fi

echo "[2] Enabling IP forwarding..."
sysctl -w net.ipv4.ip_forward=1
echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-c69router.conf

echo "[3] Making c69-router executable..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chmod +x "$SCRIPT_DIR/c69-router"

echo "[4] Installing systemd service..."
cat > /etc/systemd/system/c69-router.service << EOF
[Unit]
Description=c69-router Network Router
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$SCRIPT_DIR/c69-router
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable c69-router.service
echo "  systemd service installed. Start with: systemctl start c69-router"

echo ""
echo "=================================================="
echo "  Setup complete!"
echo "  Run: sudo $SCRIPT_DIR/c69-router"
echo "  Or:  sudo systemctl start c69-router"
echo "=================================================="
