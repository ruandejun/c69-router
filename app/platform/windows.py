"""
c69-router Platform - Windows Implementation

This module re-exports all Windows-specific network functions from network_setup.py
and adds the missing platform interface functions (check_admin_elevation,
get_singbox_binary_name, get_singbox_start_command, setup_os_startup).

The actual implementation lives in app/network_setup.py (Windows-only code).
This file exists to provide the unified platform interface expected by app/platform/__init__.py.
"""

import os
import sys
import logging
import ctypes
import subprocess
import platform

logger = logging.getLogger(__name__)

# Re-export all Windows functions from network_setup.py
# (network_setup.py contains all the Windows-specific implementations)
from app.network_setup import (
    enable_ip_forwarding,
    ensure_wan_forwarding_disabled,
    setup_firewall_rules,
    detect_wan_interface,
    detect_lan_interface,
    smart_detect_lan,
    is_wan_interface_valid,
    is_lan_interface_valid,
    ensure_lan_ip_assigned,
    get_lan_ip,
    setup_nat,
    remove_nat_for_singbox,
    restore_nat_for_singbox,
    verify_nat,
    setup_interface_dns,
    optimize_tcp_stack,
    check_hosted_network_supported as check_hotspot_supported,
    stop_hosted_network as stop_hotspot,
    get_hosted_network_adapter as get_hotspot_adapter,
    download_binaries,
    adjust_lan_interface_metric,
    detect_wan_subnet,
    check_lan_interface_health,
    detect_usb_wifi_adapter,
    setup_mobile_hotspot_ps as _winrt_hotspot,
    setup_hosted_network as _hosted_network_hotspot,
)


# --- Admin Check -------------------------------------------------------------

def check_admin_elevation() -> bool:
    """Return True if running with administrator privileges on Windows."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


# --- Hotspot (dispatch Windows Tier logic) -----------------------------------

def setup_hotspot(ssid: str = "C69-Router", password: str = "matkhau123") -> bool:
    """
    Start hotspot on Windows. Tries WinRT Mobile Hotspot first,
    falls back to legacy netsh hosted network.
    """
    # Try WinRT first (Win10/11 with Intel AX series)
    result = _winrt_hotspot(ssid=ssid, password=password)
    if result:
        logger.info(f"[Hotspot/Win] WinRT hotspot started: SSID={ssid}")
        return True
    # Fallback: legacy hosted network
    logger.warning("[Hotspot/Win] WinRT failed, trying legacy hosted network...")
    return _hosted_network_hotspot(ssid=ssid, password=password)


# --- sing-box binary ---------------------------------------------------------

def get_singbox_binary_name() -> str:
    return "sing-box.exe"


def get_singbox_start_command(config_path: str) -> list:
    """Return PowerShell elevated launch command for sing-box on Windows."""
    from app.config import PROJECT_DIR
    singbox_exe = os.path.join(PROJECT_DIR, "sing-box.exe")
    # sing-box on Windows needs admin for TUN/Wintun
    return [
        "powershell", "-NoProfile", "-Command",
        f"Start-Process -FilePath '{singbox_exe}' "
        f"-ArgumentList 'run', '-c', '{config_path}' "
        f"-Verb RunAs -WindowStyle Hidden"
    ]


# --- OS Startup (Task Scheduler) --------------------------------------------

def setup_os_startup(exe_path: str) -> bool:
    """Register c69-router in Windows Task Scheduler to run at startup."""
    # This is already implemented in main.py as setup_windows_startup()
    # Delegate to it via import to avoid duplication
    try:
        from app.main import setup_windows_startup
        setup_windows_startup()
        return True
    except Exception as e:
        logger.error(f"[Platform/Win] setup_os_startup failed: {e}")
        # Fallback: direct task scheduler command
        task_cmd = (
            f'schtasks /Create /TN "C69Router" /TR "{exe_path}" '
            f'/SC ONLOGON /RL HIGHEST /F'
        )
        r = subprocess.run(task_cmd, shell=True, capture_output=True, text=True)
        if r.returncode == 0:
            logger.info(f"[Platform/Win] Task Scheduler entry created for {exe_path}")
            return True
        logger.error(f"[Platform/Win] schtasks failed: {r.stderr}")
        return False


# --- Captive Portal ----------------------------------------------------------

def setup_captive_portproxy(lan_ip: str, port: int = 9000) -> None:
    """Redirect HTTP port 80 to captive portal via netsh portproxy."""
    try:
        cmd = (
            f"netsh interface portproxy add v4tov4 "
            f"listenaddress={lan_ip} listenport=80 "
            f"connectaddress={lan_ip} connectport={port}"
        )
        subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        logger.info(f"[Platform/Win] Captive portproxy: {lan_ip}:80 -> {port}")
    except Exception as e:
        logger.error(f"[Platform/Win] setup_captive_portproxy: {e}")


def cleanup_captive_portproxy(lan_ip: str) -> None:
    """Remove captive portal portproxy rule."""
    try:
        cmd = f"netsh interface portproxy delete v4tov4 listenaddress={lan_ip} listenport=80"
        subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        logger.info(f"[Platform/Win] Captive portproxy removed for {lan_ip}:80")
    except Exception as e:
        logger.debug(f"[Platform/Win] cleanup_captive_portproxy: {e}")
