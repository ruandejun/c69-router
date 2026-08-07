"""
c69-router Platform Abstraction Layer - Base Interface

All platform implementations (windows.py, linux.py, macos.py) must implement
these functions. The signatures are the public contract used by network_setup.py,
singbox_manager.py, and main.py.
"""

from abc import ABC, abstractmethod


class PlatformBase(ABC):
    """Abstract base defining the platform interface for c69-router."""

    # --- IP Forwarding -------------------------------------------------------

    @abstractmethod
    def enable_ip_forwarding(self, lan_interface: str = "") -> None:
        """Enable IP packet forwarding at OS level."""

    @abstractmethod
    def ensure_wan_forwarding_disabled(self, wan_interface: str) -> None:
        """Ensure WAN interface does NOT forward (prevent accidental poisoning)."""

    # --- Firewall ------------------------------------------------------------

    @abstractmethod
    def setup_firewall_rules(self, lan_interface: str = "", lan_ip: str = "192.168.10.1") -> None:
        """Open necessary firewall ports: DHCP (67), DNS (53), Web UI (80/9000)."""

    # --- Interface Detection -------------------------------------------------

    @abstractmethod
    def detect_wan_interface(self) -> str:
        """Return name of the default gateway (internet-facing) interface."""

    @abstractmethod
    def detect_lan_interface(self, exclude_interface: str = "") -> str:
        """Return name of the best LAN interface (Ethernet preferred)."""

    @abstractmethod
    def smart_detect_lan(self, exclude_interface: str = "") -> tuple:
        """Return (lan_interface, lan_type) where lan_type: 'ethernet'|'hotspot'|'wifi'."""

    @abstractmethod
    def is_wan_interface_valid(self, interface_name: str) -> bool:
        """Return True if interface has a working internet route."""

    @abstractmethod
    def is_lan_interface_valid(self, interface_name: str, wan_interface: str = "",
                                lan_gateway_ip: str = "192.168.10.1") -> bool:
        """Return True if interface can serve as LAN (Up, not WAN, not loopback)."""

    # --- IP Assignment -------------------------------------------------------

    @abstractmethod
    def ensure_lan_ip_assigned(self, lan_interface: str, lan_gateway_ip: str,
                                subnet_mask: str = "255.255.255.0") -> bool:
        """Assign static IP to LAN interface if not already set. Return True on success."""

    @abstractmethod
    def get_lan_ip(self, interface_name: str) -> str:
        """Return current IPv4 address of interface, or '' if none."""

    # --- NAT -----------------------------------------------------------------

    @abstractmethod
    def setup_nat(self, wan_interface: str, lan_subnet: str = "192.168.10.0/24") -> None:
        """Configure NAT/masquerade from lan_subnet out through wan_interface."""

    @abstractmethod
    def remove_nat_for_singbox(self, lan_subnet: str = "192.168.10.0/24") -> None:
        """Remove NAT temporarily so sing-box can see real source IPs."""

    @abstractmethod
    def restore_nat_for_singbox(self, wan_interface: str, lan_subnet: str = "192.168.10.0/24") -> None:
        """Restore NAT after sing-box stop/crash."""

    @abstractmethod
    def verify_nat(self) -> dict:
        """Return dict with nat_active bool and details."""

    # --- DNS / TCP Stack -----------------------------------------------------

    @abstractmethod
    def setup_interface_dns(self, interface_name: str, dns_servers: list) -> None:
        """Set DNS servers on interface."""

    @abstractmethod
    def optimize_tcp_stack(self, tun_interface_name: str = "") -> dict:
        """Apply TCP optimizations. Return dict of applied settings."""

    # --- Hotspot -------------------------------------------------------------

    @abstractmethod
    def check_hotspot_supported(self) -> dict:
        """Return dict: {supported: bool, method: str, reason: str}."""

    @abstractmethod
    def setup_hotspot(self, ssid: str = "C69-Router", password: str = "matkhau123") -> bool:
        """Start hotspot. Return True on success."""

    @abstractmethod
    def stop_hotspot(self) -> None:
        """Stop hotspot if running."""

    @abstractmethod
    def get_hotspot_adapter(self) -> str:
        """Return virtual adapter name created by hotspot, or ''."""

    # --- Binaries ------------------------------------------------------------

    @abstractmethod
    def download_binaries(self) -> None:
        """Download platform-specific binaries (sing-box, wintun, etc.) if missing."""

    @abstractmethod
    def get_singbox_binary_name(self) -> str:
        """Return filename: 'sing-box.exe' on Windows, 'sing-box' on Unix."""

    @abstractmethod
    def get_singbox_start_command(self, config_path: str) -> list:
        """Return command list to start sing-box with given config."""

    # --- Startup / Admin -----------------------------------------------------

    @abstractmethod
    def check_admin_elevation(self) -> bool:
        """Return True if running with admin/root privileges."""

    @abstractmethod
    def setup_os_startup(self, exe_path: str) -> bool:
        """Register c69-router to start on boot. Return True on success."""

    @abstractmethod
    def setup_captive_portproxy(self, lan_ip: str, port: int = 9000) -> None:
        """Redirect HTTP (port 80) on LAN to captive portal."""

    @abstractmethod
    def cleanup_captive_portproxy(self, lan_ip: str) -> None:
        """Remove captive portal redirect."""

    # --- Misc ----------------------------------------------------------------

    @abstractmethod
    def adjust_lan_interface_metric(self, interface_name: str) -> None:
        """Set route metric so LAN interface is preferred for local traffic."""

    @abstractmethod
    def detect_wan_subnet(self) -> str:
        """Return WAN subnet string (e.g. '192.168.1.0/24')."""

    @abstractmethod
    def check_lan_interface_health(self, interface_name: str) -> dict:
        """Return health dict: {ok, ip, gateway, ...}."""

    @abstractmethod
    def detect_usb_wifi_adapter(self, exclude_wan_interface: str = "") -> dict:
        """Detect USB/secondary WiFi adapter. Return {found, name, description}."""
