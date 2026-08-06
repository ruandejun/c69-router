"""
PhoneFarm GenRouter v2.0 — Configuration Models & Loader

Data models (Pydantic v2) cho toàn bộ hệ thống.
DeviceConfig dùng MAC làm primary key để persist proxy binding qua reconnect.
"""

import json
import os
import time
from pydantic import BaseModel, Field
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
    # DNS server dùng cho proxy này (khi device được gán proxy — DNS sẽ đi qua proxy đến server này)
    # Ví dụ: "1.1.1.1" (Cloudflare), "8.8.8.8" (Google), "208.67.222.222" (OpenDNS US), etc.
    dns_server: str = "1.1.1.1"


class DeviceConfig(BaseModel):
    """Cấu hình một thiết bị (phone/tablet). MAC là primary key."""
    mac: str                                     # Primary key — MAC address (uppercase)
    ip: str                                      # Current DHCP IP
    name: str = ""                               # Hostname / display name
    proxy_id: Optional[str] = None               # Proxy binding (persist qua reconnect)
    first_seen: int = Field(default_factory=lambda: int(time.time()))
    last_seen: int = Field(default_factory=lambda: int(time.time()))


class AppConfig(BaseModel):
    """Cấu hình chính của GenRouter."""
    # ── Network Interfaces ──
    lan_interface: str = "Ethernet 3"
    lan_gateway_ip: str = "192.168.10.1"
    lan_subnet_mask: str = "255.255.255.0"
    wan_interface: str = "Wi-Fi"
    tun_interface: str = "genrouter-tun-9"

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

    # ── Data ──
    proxies: List[ProxyConfig] = Field(default_factory=list)


# ─── Config Load / Save ─────────────────────────────────

def load_config() -> AppConfig:
    """Load config from data/config.json. Creates default if not exists."""
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
        dns_server=old_data.get("dns_server", "local"),
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
