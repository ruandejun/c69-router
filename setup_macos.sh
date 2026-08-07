#!/bin/bash
# setup_macos.sh — macOS setup script for c69-router

set -e

echo "=================================================="
echo "  c69-router macOS Setup"
echo "=================================================="

# Check root
if [ "$EUID" -ne 0 ]; then
    echo "[!] Please run as root: sudo bash setup_macos.sh"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[1] Enabling IP forwarding..."
sysctl -w net.inet.ip.forwarding=1

echo "[2] Making c69-router executable..."
chmod +x "$SCRIPT_DIR/c69-router"

echo "[3] Installing LaunchDaemon (auto-start on boot)..."
PLIST_PATH="/Library/LaunchDaemons/com.c69.router.plist"
cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.c69.router</string>
    <key>ProgramArguments</key>
    <array>
        <string>$SCRIPT_DIR/c69-router</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/var/log/c69-router.log</string>
    <key>StandardErrorPath</key>
    <string>/var/log/c69-router.err</string>
</dict>
</plist>
EOF

launchctl load "$PLIST_PATH"
echo "  LaunchDaemon installed: $PLIST_PATH"

echo ""
echo "=================================================="
echo "  Setup complete!"
echo "  Run: sudo $SCRIPT_DIR/c69-router"
echo "  Or:  sudo launchctl start com.c69.router"
echo "=================================================="
