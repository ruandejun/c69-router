"""
PhoneFarm GenRouter v2.0 — MAC Registry

Persistent storage: MAC ↔ IP ↔ Proxy mapping.
Lưu trữ tại data/mac_registry.json.

MAC là primary key. Khi thiết bị reconnect với MAC đã biết:
- Cấp lại cùng IP cũ (sticky IP)
- Tự động áp dụng proxy đã gán trước đó
"""

import json
import os
import time
import threading
import ipaddress
import logging
from typing import Optional, List, Dict

from app.config import DeviceConfig, PROJECT_DIR, DATA_DIR

logger = logging.getLogger(__name__)

REGISTRY_PATH = os.path.join(DATA_DIR, "mac_registry.json")


class MACRegistry:
    """Thread-safe MAC ↔ IP ↔ Proxy registry with persistent storage."""

    def __init__(self, filepath: str = REGISTRY_PATH):
        self._filepath = filepath
        self._data: Dict[str, dict] = {}       # MAC → {ip, name, proxy_id, first_seen, last_seen}
        self._ip_to_mac: Dict[str, str] = {}   # Reverse: IP → MAC
        self._lock = threading.Lock()
        self._load()

    # ─── Persistence ─────────────────────────────────────

    def _load(self):
        """Load registry from disk."""
        if not os.path.exists(self._filepath):
            self._data = {}
            self._ip_to_mac = {}
            return

        try:
            with open(self._filepath, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
            self._data = raw if isinstance(raw, dict) else {}

            # Thiết bị đã tồn tại từ trước khi có field này coi như đã "quyết định" rồi
            # (dù proxy_id đang None do người dùng chủ động gỡ) — không cho auto-assign
            # đụng lại. Chỉ thiết bị thực sự mới (tạo sau này) mới có proxy_decided=False.
            for info in self._data.values():
                if "proxy_decided" not in info:
                    info["proxy_decided"] = True

            # Xử lý dọn dẹp trùng lặp IP trong database cũ (giữ IP cho MAC có last_seen mới nhất)
            self._ip_to_mac = {}
            has_duplicates = False
            sorted_entries = sorted(self._data.items(), key=lambda x: x[1].get("last_seen", 0))
            for mac, info in sorted_entries:
                ip = info.get("ip", "")
                if ip:
                    if ip in self._ip_to_mac:
                        old_mac = self._ip_to_mac[ip]
                        if old_mac in self._data:
                            self._data[old_mac]["ip"] = ""
                            has_duplicates = True
                    self._ip_to_mac[ip] = mac
            
            if has_duplicates:
                logger.info("[MACRegistry] Duplicate IPs found on disk and resolved.")
                self.save()
                
            logger.info(f"[MACRegistry] Loaded {len(self._data)} devices from registry.")
        except Exception as e:
            logger.error(f"[MACRegistry] Error loading registry: {e}")
            self._data = {}
            self._ip_to_mac = {}

    def save(self):
        """Persist registry to disk (atomic write)."""
        tmp_path = self._filepath + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            if os.path.exists(self._filepath):
                os.replace(tmp_path, self._filepath)
            else:
                os.rename(tmp_path, self._filepath)
        except Exception as e:
            logger.error(f"[MACRegistry] Error saving registry: {e}")

    # ─── IP Allocation (Sticky) ──────────────────────────

    def get_ip_for_mac(self, mac: str) -> Optional[str]:
        """Trả về IP đã cấp trước đó cho MAC này (sticky IP)."""
        mac = mac.upper()
        with self._lock:
            entry = self._data.get(mac)
            if entry and entry.get("ip"):
                return entry["ip"]
        return None

    def _assign_ip_locked(self, mac: str, ip_str: str) -> str:
        """Gán ip_str cho mac trong bộ nhớ + persist. Caller phải đang giữ self._lock."""
        now = int(time.time())
        if mac not in self._data:
            self._data[mac] = {
                "ip": ip_str,
                "name": "",
                "proxy_id": None,
                "proxy_decided": False,
                "first_seen": now,
                "last_seen": now,
            }
        else:
            self._data[mac]["ip"] = ip_str
            self._data[mac]["last_seen"] = now

        self._ip_to_mac[ip_str] = mac
        self.save()
        return ip_str

    @staticmethod
    def _stale_after_seconds(lease_time: int) -> int:
        """Ngưỡng "offline lâu": 2x lease_time. Thiết bị còn sống bình thường tự renew ở
        T1 (~50% lease_time) nên last_seen luôn được cập nhật trước mốc này rất lâu; im
        lặng suốt 2 chu kỳ lease liên tiếp mới coi là đã mất kết nối hẳn, tránh thu hồi
        nhầm thiết bị chỉ đang ngủ mạng/offline tạm thời."""
        return max(int(lease_time or 3600), 60) * 2

    def _find_stale_ip_holders_locked(
        self, start: "ipaddress.IPv4Address", end: "ipaddress.IPv4Address",
        stale_after: int, exclude_mac: Optional[str] = None,
    ) -> list:
        """Trả về [(last_seen, mac, ip)] của thiết bị đang giữ IP trong [start,end] nhưng
        offline quá stale_after giây, sắp xếp offline lâu nhất trước. Caller phải đang giữ
        self._lock."""
        now = time.time()
        candidates = []
        for candidate_mac, info in self._data.items():
            if candidate_mac == exclude_mac:
                continue
            ip_str = info.get("ip", "")
            if not ip_str:
                continue
            try:
                ip_obj = ipaddress.IPv4Address(ip_str)
            except Exception:
                continue
            if not (start <= ip_obj <= end):
                continue
            last_seen = info.get("last_seen", 0)
            if now - last_seen >= stale_after:
                candidates.append((last_seen, candidate_mac, ip_str))
        candidates.sort(key=lambda x: x[0])
        return candidates

    def allocate_new_ip(self, mac: str, pool_start: str, pool_end: str, lease_time: int = 3600) -> Optional[str]:
        """Cấp IP mới từ pool cho MAC chưa có IP.

        Tránh các IP đã cấp cho MAC khác. Nếu pool đã đầy, thu hồi IP của thiết bị
        offline lâu nhất (last_seen quá cũ so với lease_time) thay vì báo lỗi —
        proxy_id/name/first_seen của thiết bị đó vẫn giữ nguyên, chỉ có IP bị gỡ (thiết
        bị sẽ được cấp IP mới — có thể IP khác — khi nó reconnect trở lại). Điều này
        giải quyết trường hợp pool bị "đầy vĩnh viễn" bởi các thiết bị đã offline hẳn
        (die máy, đổi máy, factory reset...) không bao giờ được người dùng chủ động xoá.
        Returns None nếu pool đầy và không có thiết bị nào đủ "cũ" để thu hồi.
        """
        mac = mac.upper()
        with self._lock:
            start = ipaddress.IPv4Address(pool_start)
            end = ipaddress.IPv4Address(pool_end)
            used_ips = set(self._ip_to_mac.keys())

            current = start
            while current <= end:
                ip_str = str(current)
                if ip_str not in used_ips:
                    return self._assign_ip_locked(mac, ip_str)
                current += 1

            # ── Pool đầy: thử thu hồi IP từ thiết bị offline lâu ──
            stale_after = self._stale_after_seconds(lease_time)
            candidates = self._find_stale_ip_holders_locked(start, end, stale_after, exclude_mac=mac)

            if not candidates:
                logger.error(
                    f"[MACRegistry] IP pool exhausted ({pool_start} - {pool_end}) "
                    f"and no device has been offline long enough (>{stale_after}s) to reclaim!"
                )
                return None

            # Thu hồi từ thiết bị im lặng lâu nhất (LRU thực sự, không phải LRU theo thứ tự cấp phát)
            oldest_last_seen, stale_mac, reclaimed_ip = candidates[0]
            offline_minutes = int((time.time() - oldest_last_seen) / 60)
            logger.warning(
                f"[MACRegistry] Pool full — reclaiming {reclaimed_ip} from offline device "
                f"{stale_mac} (silent for {offline_minutes}m) to grant new device {mac}. "
                f"Proxy binding of {stale_mac} is preserved; it will get a new IP on reconnect."
            )

            # Chỉ gỡ IP — KHÔNG đụng tới proxy_id/name/first_seen của thiết bị offline,
            # vì proxy phải gắn cố định theo MAC (theo đúng thiết kế sticky-proxy hiện tại),
            # chỉ có việc "giữ chỗ" 1 địa chỉ IP trong pool mới cần thu hồi khi offline.
            self._data[stale_mac]["ip"] = ""
            self._ip_to_mac.pop(reclaimed_ip, None)

            return self._assign_ip_locked(mac, reclaimed_ip)

    def clear_stale_ips(self, pool_start: str, pool_end: str, lease_time: int = 3600) -> List[str]:
        """Chủ động gỡ IP (giữ nguyên proxy_id/name/first_seen) của MỌI thiết bị trong pool
        đã offline quá lâu — dùng cho nút "Dọn pool IP" trên Settings và 1 lần lúc khởi
        động app, để pool nhẹ ngay lập tức thay vì chỉ thu hồi từng IP một cách "lazy" khi
        có thiết bị mới xin IP (xem allocate_new_ip). KHÔNG xoá device/proxy binding —
        thiết bị sẽ được cấp IP mới (có thể khác) khi kết nối lại.

        Returns danh sách MAC đã được gỡ IP.
        """
        stale_after = self._stale_after_seconds(lease_time)
        cleared_macs = []
        with self._lock:
            start = ipaddress.IPv4Address(pool_start)
            end = ipaddress.IPv4Address(pool_end)
            candidates = self._find_stale_ip_holders_locked(start, end, stale_after)
            for _, stale_mac, ip_str in candidates:
                self._data[stale_mac]["ip"] = ""
                self._ip_to_mac.pop(ip_str, None)
                cleared_macs.append(stale_mac)
            if cleared_macs:
                self.save()

        if cleared_macs:
            logger.warning(
                f"[MACRegistry] Cleared stale IP reservation for {len(cleared_macs)} "
                f"long-offline device(s) (silent >{stale_after}s): {cleared_macs}"
            )
        return cleared_macs

    # ─── Lease Management ────────────────────────────────

    def update_lease(self, mac: str, ip: str, name: str = ""):
        """Cập nhật/tạo lease khi DHCP ACK."""
        mac = mac.upper()
        now = int(time.time())
        with self._lock:
            # Gỡ IP khỏi thiết bị cũ nếu có trùng lặp để tránh một IP gán cho 2 MAC khác nhau
            old_mac = self._ip_to_mac.get(ip)
            if old_mac and old_mac != mac:
                if old_mac in self._data:
                    self._data[old_mac]["ip"] = ""

            if mac in self._data:
                old_ip = self._data[mac].get("ip", "")
                if old_ip and old_ip != ip:
                    self._ip_to_mac.pop(old_ip, None)
                self._data[mac]["ip"] = ip
                self._data[mac]["last_seen"] = now
                if name:
                    self._data[mac]["name"] = name
            else:
                self._data[mac] = {
                    "ip": ip,
                    "name": name or f"Device {ip}",
                    "proxy_id": None,
                    "proxy_decided": False,
                    "first_seen": now,
                    "last_seen": now,
                }
            self._ip_to_mac[ip] = mac
            self.save()

    def touch_device(self, mac: str):
        """Cập nhật last_seen timestamp."""
        mac = mac.upper()
        with self._lock:
            if mac in self._data:
                self._data[mac]["last_seen"] = int(time.time())
                # Don't save on every touch to reduce I/O

    # ─── Proxy Management ────────────────────────────────

    def set_proxy(self, mac: str, proxy_id: Optional[str]):
        """Gán proxy cho MAC. Persist ngay lập tức."""
        mac = mac.upper()
        with self._lock:
            if mac in self._data:
                self._data[mac]["proxy_id"] = proxy_id
                self._data[mac]["proxy_decided"] = True
                self.save()
                logger.info(f"[MACRegistry] Set proxy for {mac} → {proxy_id}")
            else:
                logger.warning(f"[MACRegistry] Cannot set proxy: MAC {mac} not found")

    def get_proxy_for_mac(self, mac: str) -> Optional[str]:
        """Lấy proxy_id đã gán cho MAC."""
        mac = mac.upper()
        with self._lock:
            entry = self._data.get(mac)
            if entry:
                return entry.get("proxy_id")
        return None

    def get_proxy_for_ip(self, ip: str) -> Optional[str]:
        """Lấy proxy_id dựa trên IP (reverse lookup qua MAC)."""
        with self._lock:
            mac = self._ip_to_mac.get(ip)
            if mac and mac in self._data:
                return self._data[mac].get("proxy_id")
        return None

    # ─── Device Queries ──────────────────────────────────

    def get_all_devices(self) -> List[DeviceConfig]:
        """Trả về tất cả devices dạng DeviceConfig."""
        with self._lock:
            devices = []
            for mac, info in self._data.items():
                devices.append(DeviceConfig(
                    mac=mac,
                    ip=info.get("ip", ""),
                    name=info.get("name", ""),
                    proxy_id=info.get("proxy_id"),
                    first_seen=info.get("first_seen", 0),
                    last_seen=info.get("last_seen", 0),
                ))
            return devices

    def get_device_by_mac(self, mac: str) -> Optional[DeviceConfig]:
        """Lấy device theo MAC."""
        mac = mac.upper()
        with self._lock:
            info = self._data.get(mac)
            if info:
                return DeviceConfig(mac=mac, **{
                    k: info.get(k) for k in ["ip", "name", "proxy_id", "first_seen", "last_seen"]
                    if info.get(k) is not None
                })
        return None

    def get_device_by_ip(self, ip: str) -> Optional[DeviceConfig]:
        """Lấy device theo IP (reverse lookup)."""
        with self._lock:
            mac = self._ip_to_mac.get(ip)
            if mac and mac in self._data:
                info = self._data[mac]
                return DeviceConfig(mac=mac, **{
                    k: info.get(k) for k in ["ip", "name", "proxy_id", "first_seen", "last_seen"]
                    if info.get(k) is not None
                })
        return None

    def set_device_name(self, mac: str, name: str):
        """Đặt tên cho device."""
        mac = mac.upper()
        with self._lock:
            if mac in self._data:
                self._data[mac]["name"] = name
                self.save()

    def remove_device(self, mac: str):
        """Xóa device khỏi registry."""
        mac = mac.upper()
        with self._lock:
            if mac in self._data:
                ip = self._data[mac].get("ip", "")
                del self._data[mac]
                if ip:
                    self._ip_to_mac.pop(ip, None)
                self.save()
                logger.info(f"[MACRegistry] Removed device {mac}")

    def remove_devices_by_macs(self, macs: List[str]):
        """Xóa nhiều devices."""
        with self._lock:
            for mac in macs:
                mac = mac.upper()
                if mac in self._data:
                    ip = self._data[mac].get("ip", "")
                    del self._data[mac]
                    if ip:
                        self._ip_to_mac.pop(ip, None)
            self.save()

    def get_device_count(self) -> int:
        """Tổng số devices."""
        with self._lock:
            return len(self._data)

    def get_all_ips(self) -> List[str]:
        """Lấy tất cả IP đang cấp."""
        with self._lock:
            return list(self._ip_to_mac.keys())

    # ─── Migration ───────────────────────────────────────

    def migrate_from_old_devices(self, old_devices: list):
        """Migrate devices từ config.json cũ sang registry.
        
        old_devices: list of dicts with {mac, ip, name, proxy_id, proxy_port}
        """
        with self._lock:
            for dev in old_devices:
                mac = dev.get("mac", "").upper()
                if not mac or len(mac) < 10:
                    continue
                ip = dev.get("ip", "")
                self._data[mac] = {
                    "ip": ip,
                    "name": dev.get("name", f"Device {ip}"),
                    "proxy_id": dev.get("proxy_id"),
                    "proxy_decided": True,
                    "first_seen": int(time.time()),
                    "last_seen": int(time.time()),
                }
                if ip:
                    self._ip_to_mac[ip] = mac
            self.save()
            logger.info(f"[MACRegistry] Migrated {len(old_devices)} devices from old config.")
