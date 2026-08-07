"""
c69-router Platform Dispatcher

Auto-detects the current OS and imports all platform-specific functions
into this namespace so callers can do:

    from app.platform import detect_wan_interface, setup_nat, ...

The public API is defined in base.py. Each platform module
(windows.py / linux.py / macos.py) provides the same function names.
"""

import sys
import logging

logger = logging.getLogger(__name__)

_PLATFORM = sys.platform  # 'win32' | 'darwin' | 'linux'

if _PLATFORM == "win32":
    logger.debug("[Platform] Loading Windows platform module.")
    from app.platform.windows import (
        check_admin_elevation,
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
        check_hotspot_supported,
        setup_hotspot,
        stop_hotspot,
        get_hotspot_adapter,
        download_binaries,
        get_singbox_binary_name,
        get_singbox_start_command,
        setup_os_startup,
        setup_captive_portproxy,
        cleanup_captive_portproxy,
        adjust_lan_interface_metric,
        detect_wan_subnet,
        check_lan_interface_health,
        detect_usb_wifi_adapter,
    )
    PLATFORM_NAME = "windows"

elif _PLATFORM == "darwin":
    logger.debug("[Platform] Loading macOS platform module.")
    from app.platform.macos import (
        check_admin_elevation,
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
        check_hotspot_supported,
        setup_hotspot,
        stop_hotspot,
        get_hotspot_adapter,
        download_binaries,
        get_singbox_binary_name,
        get_singbox_start_command,
        setup_os_startup,
        setup_captive_portproxy,
        cleanup_captive_portproxy,
        adjust_lan_interface_metric,
        detect_wan_subnet,
        check_lan_interface_health,
        detect_usb_wifi_adapter,
    )
    PLATFORM_NAME = "macos"

else:
    # Linux and any other Unix-like OS
    logger.debug("[Platform] Loading Linux platform module.")
    from app.platform.linux import (
        check_admin_elevation,
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
        check_hotspot_supported,
        setup_hotspot,
        stop_hotspot,
        get_hotspot_adapter,
        download_binaries,
        get_singbox_binary_name,
        get_singbox_start_command,
        setup_os_startup,
        setup_captive_portproxy,
        cleanup_captive_portproxy,
        adjust_lan_interface_metric,
        detect_wan_subnet,
        check_lan_interface_health,
        detect_usb_wifi_adapter,
    )
    PLATFORM_NAME = "linux"

logger.info(f"[Platform] Active platform: {PLATFORM_NAME} (sys.platform={_PLATFORM})")
