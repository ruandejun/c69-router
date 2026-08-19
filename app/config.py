"""
PhoneFarm GenRouter v2.0 — Configuration Models & Loader

Data models (Pydantic v2) cho toàn bộ hệ thống.
DeviceConfig dùng MAC làm primary key để persist proxy binding qua reconnect.
"""

import json
import os
import time
import ipaddress
import re
import secrets
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import List, Optional

import sys

# ─── Paths ───────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    PROJECT_DIR = os.path.dirname(sys.executable)
    RESOURCE_DIR = getattr(sys, '_MEIPASS', PROJECT_DIR)
else:
    PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    RESOURCE_DIR = PROJECT_DIR

DATA_DIR = os.path.join(PROJECT_DIR, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)


# ─── Pydantic Models ────────────────────────────────────

class ProxyConfig(BaseModel):
    """Cấu hình một proxy server (SOCKS5 hoặc HTTP)."""
    id: str                                      # Unique ID, auto-gen from host:port
    type: str = "socks5"                         # socks5 | http
    host: str
    port: int
    username: str = ""
    password: str = ""
    status: str = "Unknown"                      # Live | Die | Unknown
    latency: int = -1                            # ms, -1 = unknown
    webrtc_bypass: bool = True                   # Apply WebRTC leak protection
    blacklisted: Optional[bool] = None           # None = not checked yet
    blacklisted_on: List[str] = Field(default_factory=list)  # DNSBL names
    blacklist_checked_at: Optional[float] = None # Unix timestamp of last check
    dns_server: str = "1.1.1.1"

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError("Proxy ID must contain only letters, digits, dot, underscore, or dash")
        return value

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        value = value.lower().strip()
        if value not in {"socks", "socks5", "http"}:
            raise ValueError("Proxy type must be socks5 or http")
        return value

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 253 or any(char.isspace() for char in value):
            raise ValueError("Proxy host is invalid")
        return value

    @field_validator("port")
    @classmethod
    def validate_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("Proxy port must be between 1 and 65535")
        return value

    @field_validator("dns_server")
    @classmethod
    def validate_dns_server(cls, value: str) -> str:
        try:
            return str(ipaddress.IPv4Address(value.strip()))
        except ValueError as exc:
            raise ValueError("DNS server must be a valid IPv4 address") from exc


class DeviceConfig(BaseModel):
    """Cấu hình một thiết bị (phone/tablet). MAC là primary key."""
    mac: str                                     # Primary key — MAC address (uppercase)
    ip: str                                      # Current DHCP IP
    name: str = ""                               # Hostname / display name
    proxy_id: Optional[str] = None               # Proxy binding (persist qua reconnect)
    rotate_minutes: int = 0                      # Per-device rotation: 0=theo global, >0=override riêng
    first_seen: int = Field(default_factory=lambda: int(time.time()))
    last_seen: int = Field(default_factory=lambda: int(time.time()))



class AppConfig(BaseModel):
    """Cấu hình chính của GenRouter."""
    # ── Network Interfaces ──
    lan_interface: str = "Ethernet 3"
    lan_gateway_ip: str = "192.168.10.1"
    lan_subnet_mask: str = "255.255.255.0"
    wan_interface: str = "Wi-Fi"
    tun_interface: str = "GenRouterTUN"

    # ── DHCP Server ──
    dhcp_enabled: bool = True
    dhcp_range_start: str = "192.168.10.10"
    dhcp_range_end: str = "192.168.10.250"
    dhcp_lease_time: int = 3600                  # seconds (1 hour)
    # IP label phat cho phone qua DHCP de hijack-dns bat query (KHONG duoc trung voi
    # 1.1.1.1/8.8.8.8/8.8.4.4/1.0.0.1 vi 4 IP do bi exclude khoi TUN trong singbox_manager.py
    # de lam upstream resolver that cua sing-box - trung se lam hijack-dns mat tac dung).
    dns_server: str = "9.9.9.9"       # Quad9 — chi la label cho DHCP, khong phai IP sing-box thuc su goi

    # ── Routing ──
    bypass_cidrs: List[str] = Field(default_factory=lambda: [
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16"
    ])

    # ── Features ──
    auto_rotate_minutes: int = 0
    auto_assign_new_devices: bool = False
    auto_assign_mode: str = "balance"            # balance | exclusive
    block_direct_devices: bool = False
    direct_whitelist: List[str] = Field(default_factory=list)
    # Số phút rotate mặc định cho thiết bị MỚI kết nối vào.
    # Khi user set bulk rotation qua /set-device-rotation (không chỉ định MAC cụ thể),
    # setting này được lưu lại để thiết bị mới join sau đó tự động inherit cùng rotation
    # mà không cần bấm lại. 0 = không rotate per-device (theo global auto_rotate_minutes).
    default_device_rotate_minutes: int = 0

    # ── WiFi Hotspot (Hosted Network) ──
    # Khi bật: c69-router tự động tạo WiFi hotspot khi khởi động (dùng netsh wlan hostednetwork).
    # Laptop chỉ cần 1 USB WiFi (hoặc card WiFi tích hợp) — phone kết nối qua hotspot thay vì dây.
    # Virtual adapter được tạo ra sẽ tự động được dùng làm lan_interface.
    wifi_hotspot_enabled: bool = False
    wifi_hotspot_ssid: str = "C69-Router"
    wifi_hotspot_password: str = "Matkhau123"

    # ── Webshare API Integration ──
    # Cronjob tự động check proxy health và thay thế proxy Die bằng proxy mới từ Webshare.
    # Cần Webshare API key (lấy từ https://proxy.webshare.io/dashboard/ → API)
    webshare_api_key: str = ""                     # Webshare API Token (để trống = tắt)
    webshare_enabled: bool = False                 # Bật/tắt cronjob auto-check & replace
    webshare_check_interval_minutes: int = 5       # Chu kỳ check proxy health (phút)
    webshare_auto_replace: bool = True             # True = tự thay proxy Die bằng proxy Webshare mới

    # ── Data ──
    proxies: List[ProxyConfig] = Field(default_factory=list)

    @field_validator("lan_gateway_ip", "dhcp_range_start", "dhcp_range_end", "dns_server")
    @classmethod
    def validate_ipv4_fields(cls, value: str) -> str:
        val_clean = value.strip()
        if val_clean.lower() == "local" or not val_clean:
            return "1.1.1.1"
        try:
            return str(ipaddress.IPv4Address(val_clean))
        except ValueError as exc:
            raise ValueError("Must be a valid IPv4 address") from exc

    @field_validator("lan_subnet_mask")
    @classmethod
    def validate_subnet_mask(cls, value: str) -> str:
        try:
            ipaddress.IPv4Network(f"0.0.0.0/{value.strip()}")
            return value.strip()
        except ValueError as exc:
            raise ValueError("Must be a valid IPv4 subnet mask") from exc

    @field_validator("dhcp_lease_time")
    @classmethod
    def validate_lease_time(cls, value: int) -> int:
        if not 60 <= value <= 604800:
            raise ValueError("DHCP lease time must be between 60 seconds and 7 days")
        return value

    @field_validator("auto_rotate_minutes", "default_device_rotate_minutes")
    @classmethod
    def validate_rotation_minutes(cls, value: int) -> int:
        if not 0 <= value <= 43200:
            raise ValueError("Rotation interval must be between 0 and 43200 minutes")
        return value

    @field_validator("auto_assign_mode")
    @classmethod
    def validate_auto_assign_mode(cls, value: str) -> str:
        if value not in {"balance", "exclusive"}:
            raise ValueError("auto_assign_mode must be balance or exclusive")
        return value

    @model_validator(mode="after")
    def validate_network_ranges(self):
        network = ipaddress.IPv4Network(f"{self.lan_gateway_ip}/{self.lan_subnet_mask}", strict=False)
        start = ipaddress.IPv4Address(self.dhcp_range_start)
        end = ipaddress.IPv4Address(self.dhcp_range_end)
        gateway = ipaddress.IPv4Address(self.lan_gateway_ip)
        if start not in network or end not in network or start > end:
            raise ValueError("DHCP range must be ordered and belong to the LAN subnet")
        if gateway in {start, end} or start <= gateway <= end:
            raise ValueError("DHCP range must not include the LAN gateway IP")
        if self.wifi_hotspot_enabled and network != ipaddress.IPv4Network("192.168.137.0/24"):
            raise ValueError(
                "Windows Mobile Hotspot uses the ICS subnet 192.168.137.0/24; "
                "disable hotspot mode before configuring a custom LAN subnet"
            )
        return self


# ─── Config Load / Save ─────────────────────────────────

def _generate_hotspot_ssid() -> str:
    """Tạo SSID duy nhất cho WiFi Hotspot dạng 'c69-router-<hostname>'.

    Dùng tên laptop/máy tính để tạo SSID dễ nhận biết:
    - c69-router-admin-pc
    - c69-router-laptop1
    Tránh trùng SSID khi có nhiều máy chạy c69-router trong cùng một khu vực.
    """
    import socket
    import re
    try:
        hostname = socket.gethostname()
        clean = re.sub(r'[^A-Za-z0-9-]', '', hostname).strip('-').lower()
        if clean:
            return f"c69-router-{clean}"
    except Exception:
        pass
    import random
    suffix = '{:04x}'.format(random.randint(0, 0xFFFF))
    return f"c69-router-{suffix}"


def load_config() -> AppConfig:
    """Load config from data/config.json. Creates a safe default if not exists."""
    if not os.path.exists(CONFIG_PATH):
        config = AppConfig()
        save_config(config)
        return config

    needs_save = False
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Tự động mở rộng dải IP nếu phát hiện dải mặc định cũ (100 -> 200) để hỗ trợ 200+ thiết bị
        if data.get("dhcp_range_start") == "192.168.10.100" and data.get("dhcp_range_end") == "192.168.10.200":
            data["dhcp_range_start"] = "192.168.10.10"
            data["dhcp_range_end"] = "192.168.10.250"
            needs_save = True
        
        config = AppConfig(**data)
    except Exception:
        # Nếu file lỗi/corrupt, trả về config mặc định
        config = AppConfig()
        needs_save = True

    if needs_save:
        try:
            save_config(config)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"[Config] Failed to save updated config: {e}")

    return config


def save_config(config: AppConfig):
    """Save config to data/config.json (atomic write)."""
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config.model_dump(), f, indent=2, ensure_ascii=False)
    # Atomic rename (os.replace works on Windows whether destination exists or not)
    os.replace(tmp_path, CONFIG_PATH)



def migrate_old_config():
    """Migrate từ config.json cũ (root) sang data/config.json mới.
    
    Chuyển đổi:
    - proxies: giữ nguyên (bỏ tun_interface, tun_ip, gateway, routing_table)
    - devices: chuyển vào mac_registry.json (không lưu trong config nữa)
    - bypass_domains → bypass_cidrs
    """
    old_path = os.path.join(PROJECT_DIR, "config.json")
    if not os.path.exists(old_path) or os.path.exists(CONFIG_PATH):
        return None  # Nothing to migrate or already migrated

    try:
        with open(old_path, "r", encoding="utf-8") as f:
            old_data = json.load(f)
    except Exception:
        return None

    # Build new config
    new_config = AppConfig(
        lan_interface=old_data.get("lan_interface", "Ethernet 3"),
        wan_interface=old_data.get("wan_interface", "Wi-Fi"),
        dhcp_range_start=old_data.get("dhcp_range_start", "192.168.10.100"),
        dhcp_range_end=old_data.get("dhcp_range_end", "192.168.10.200"),
        dns_server=old_data.get("dns_server", "1.1.1.1"),
        auto_rotate_minutes=old_data.get("auto_rotate_minutes", 0),
        bypass_cidrs=old_data.get("bypass_domains", [
            "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"
        ]),
    )

    # Migrate proxies (strip TUN-specific fields)
    for p in old_data.get("proxies", []):
        new_config.proxies.append(ProxyConfig(
            id=p.get("id", ""),
            type=p.get("type", "socks5"),
            host=p.get("host", ""),
            port=p.get("port", 1080),
            username=p.get("username", ""),
            password=p.get("password", ""),
            status=p.get("status", "Unknown"),
            latency=p.get("latency", -1),
        ))

    save_config(new_config)

    # Return old devices for mac_registry migration
    return old_data.get("devices", [])
