"""
PhoneFarm GenRouter v2.0 — Sing-Box Manager

Manages Sing-Box process lifecycle:
- Generate sing-box-config.json from MAC Registry state
- Start/Stop/Hot-reload sing-box.exe (elevated, TUN mode)
- Download binaries if missing
"""

import subprocess
import logging
import os
import json
import time
import platform
import ipaddress
import threading
from typing import Optional

from app.config import AppConfig, PROJECT_DIR
from app.mac_registry import MACRegistry
from app.network_setup import detect_wan_interface, detect_wan_subnet, setup_nat, remove_nat_for_singbox, restore_nat_for_singbox

logger = logging.getLogger(__name__)

SINGBOX_EXE = os.path.join(PROJECT_DIR, "sing-box.exe")
SINGBOX_CONFIG = os.path.join(PROJECT_DIR, "sing-box-config.json")
SINGBOX_PID_FILE = os.path.join(PROJECT_DIR, "sing-box.pid")
SINGBOX_LOG = os.path.join(PROJECT_DIR, "sing-box.log")


class SingBoxManager:
    """Manages Sing-Box TUN transparent proxy process."""

    def __init__(self, config: AppConfig, mac_registry: MACRegistry):
        self._config = config
        self._registry = mac_registry
        self._is_running = False
        self._reload_timer = None
        self._reload_lock = threading.Lock()
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = None
        self._on_crash_callback = None   # Callback(event_type, detail) — inject từ main.py
        self.last_error: Optional[str] = None  # Lý do lần start() gần nhất thất bại (None nếu OK)
        # ip -> "dns_{proxy_id}" | "dns_direct": bucket DNS mà TIẾN TRÌNH sing-box đang chạy
        # THỰC SỰ áp dụng cho mỗi IP (chốt lại sau mỗi lần _apply() start() thành công).
        # self._pending_dns_buckets là bucket mà generate_config() VỪA TÍNH (ghi ra file),
        # chưa chắc đã được nạp vào tiến trình đang chạy nếu lần gọi generate_config() đó không
        # đi kèm 1 restart thật (vd nhánh "ghi đè config để lưu trạng thái" trong
        # update_device_routing() sau khi đổi thành công qua Clash API).
        self._live_device_dns_bucket: dict = {}
        self._pending_dns_buckets: dict = {}
        # Tăng mỗi khi _apply() BẮT ĐẦU đọc lại state (generate_config()) — dùng để coalesce
        # các reload_now() chồng lấn, xem giải thích chi tiết ở reload_now().
        self._apply_epoch = 0

    def set_crash_callback(self, callback):
        """Đăng ký callback được gọi khi sing-box crash hoặc recover.

        callback(event: str, detail: str) trong đó:
          event = 'singbox_crash'    — process chết ngoài ý muốn, bắt đầu restart
          event = 'singbox_restart_failed' — restart thất bại
          event = 'singbox_recovered' — restart thành công, sing-box đang chạy lại
        """
        self._on_crash_callback = callback

    def _notify(self, event: str, detail: str = ""):
        """Gọi crash callback nếu đã đăng ký."""
        cb = self._on_crash_callback
        if cb:
            try:
                cb(event, detail)
            except Exception as _ce:
                logger.debug(f"[SingBox] Crash callback error: {_ce}")

    def start_watchdog(self, interval_seconds: int = 20):
        """Giám sát nền: nếu sing-box được kỳ vọng đang chạy (self._is_running == True,
        tức là start() thành công gần nhất và chưa gọi stop() chủ động) nhưng process
        thực tế đã chết (crash muộn không bắt được ngay lúc start — vd Wintun bị kẹt
        sau nhiều lần hot-reload dồn dập), tự động start() lại thay vì để router
        chết im lặng cho tới khi có người vào kiểm tra thủ công.
        """
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return

        def _loop():
            # Backoff: nếu start() thất bại liên tiếp (vd Wintun kẹt trạng thái nửa-tạo-nửa-xoá,
            # config lỗi khiến sing-box crash ngay khi bật), KHÔNG restart lại mỗi 20s vô hạn —
            # mỗi lần thử lại đều kill/tạo lại adapter TUN, chính là thứ đẩy driver Wintun vào
            # lỗi FATAL "create adapter: already exists" và làm cả máy chậm dần ("treo"). Giãn
            # dần chu kỳ theo số lần fail (20s → 40s → ... tối đa 180s), reset khi start OK.
            fails = 0
            while True:
                wait_s = interval_seconds * (1 + min(fails, 8))
                if self._watchdog_stop.wait(wait_s):
                    return  # stop() chủ động
                # Chỉ quan tâm khi sing-box ĐƯỢC KỲ VỌNG chạy mà process thực tế đã chết.
                if not self._is_running or self.is_running:
                    fails = 0
                    continue

                # Giữ _reload_lock trong lúc restart — tránh đè lên reload_now()/_do_hot_reload()
                # đang chạy song song (xem giải thích ở đó).
                with self._reload_lock:
                    # Kiểm tra LẠI sau khi giữ lock: một restart hợp lệ (assign/rotate) có thể
                    # vừa hoàn tất trong lúc watchdog chờ lock — nếu process đã sống lại thì bỏ
                    # qua, tránh restart thừa ngay sau một restart hợp lệ ("restart nhiều lần").
                    if not self._is_running or self.is_running:
                        fails = 0
                        continue

                    self.last_error = "Process died unexpectedly (caught by watchdog); auto-restarting"
                    logger.warning(
                        "[SingBox] Watchdog: process expected running but not alive. Restarting..."
                    )
                    from app.error_reporter import report_error
                    report_error("SingBox", self.last_error, level="warning")

                    # ── Broadcast CRASH event ──
                    self._notify("singbox_crash", self.last_error)

                    try:
                        self.start()
                    except Exception as e:
                        self.last_error = f"Watchdog restart failed: {e}"
                        logger.error(f"[SingBox] Watchdog restart failed: {e}")
                        report_error("SingBox", self.last_error, level="critical", exc=e)
                        self._notify("singbox_restart_failed", self.last_error)

                    if self.is_running:
                        fails = 0
                        logger.info("[SingBox] Watchdog: sing-box recovered successfully.")
                        self._notify("singbox_recovered", "sing-box restarted and is running.")
                    else:
                        fails += 1
                        logger.error(
                            f"[SingBox] Watchdog restart failed (lần thứ {fails} liên tiếp); "
                            f"giãn chu kỳ kiểm tra lên {interval_seconds * (1 + min(fails, 8))}s."
                        )
                        self._notify(
                            "singbox_restart_failed",
                            f"Restart failed (attempt {fails}); next retry in {interval_seconds * (1 + min(fails, 8))}s."
                        )

        self._watchdog_thread = threading.Thread(target=_loop, daemon=True, name="singbox-watchdog")
        self._watchdog_thread.start()
        logger.info(f"[SingBox] Watchdog started (interval={interval_seconds}s).")

    @property
    def is_running(self) -> bool:
        """Check if sing-box process is alive."""
        if platform.system() != "Windows":
            return self._is_running

        if not os.path.exists(SINGBOX_PID_FILE):
            return False

        try:
            with open(SINGBOX_PID_FILE, "r") as f:
                pid_str = f.read().strip()
            if not pid_str.isdigit():
                return False

            pid = int(pid_str)
            try:
                import psutil
                if psutil.pid_exists(pid):
                    proc = psutil.Process(pid)
                    return "sing-box" in proc.name().lower()
                return False
            except Exception:
                # Fallback to os.kill
                try:
                    os.kill(pid, 0)
                    return True
                except OSError:
                    return False
        except Exception:
            return False

    def update_config(self, config: AppConfig):
        """Update config reference."""
        self._config = config

    @property
    def config(self) -> AppConfig:
        """Config hiện tại đang giữ trong bộ nhớ (không đọc lại từ đĩa).

        Được các route cập nhật qua update_config() ngay sau mỗi save_config() làm thay
        đổi auto_rotate_minutes/proxies — đủ mới để dùng cho các vòng lặp nền (vd
        auto_rotate_loop trong main.py) mà không cần tự load_config() từ đĩa mỗi lần.
        """
        return self._config

    # ─── Config Generation ───────────────────────────────

    def generate_config(self) -> bool:
        """Generate sing-box-config.json from current MAC Registry state.
        
        Returns True if there are devices with proxy assignments (need sing-box).
        """
        config = self._config
        devices = self._registry.get_all_devices()

        if not devices:
            # No devices → write empty config
            with open(SINGBOX_CONFIG, "w", encoding="utf-8") as f:
                json.dump({"_note": "No devices, sing-box not needed"}, f, indent=2)
            return False

        # ── Parse bypass CIDRs ──
        # Chỉ exclude các địa chỉ local thực sự (localhost, link-local, multicast)
        # KHÔNG exclude 192.168.0.0/16 vì điều đó sẽ block traffic điện thoại (192.168.10.x) đi internet
        local_cidrs = [
            "127.0.0.0/8",          # Loopback
            "10.0.0.0/8",           # Private class A (giữ lại để không loop proxy)
            "172.16.0.0/12",        # Private class B (Docker/Hyper-V ranges)
            "224.0.0.0/4",          # Multicast
            "255.255.255.255/32"    # Broadcast
        ]
        for bypass in config.bypass_cidrs:
            bypass = bypass.strip()
            if not bypass:
                continue
            # Skip 192.168.0.0/16 — nó sẽ exclude traffic từ điện thoại (192.168.10.x) ra internet
            if bypass == "192.168.0.0/16":
                logger.info(
                    "[SingBox] Skipping bypass CIDR '192.168.0.0/16' to allow phone traffic through TUN. "
                    "Phone IPs (192.168.10.x) need to reach internet via TUN."
                )
                continue
            try:
                ipaddress.ip_network(bypass, strict=False)
                if bypass not in local_cidrs:
                    local_cidrs.append(bypass)
            except ValueError:
                pass

        # ── Determine LAN subnet ──
        lan_subnet = "192.168.10.0/24"
        try:
            ip_obj = ipaddress.ip_interface(f"{config.lan_gateway_ip}/24")
            lan_subnet = str(ip_obj.network)
        except Exception:
            pass

        # ── WAN subnet exclude ──
        wan_subnet = detect_wan_subnet()
        exclude_addresses = [
            "127.0.0.0/8", lan_subnet,
            "224.0.0.0/4", "255.255.255.255/32"
        ]
        if wan_subnet and wan_subnet not in exclude_addresses:
            exclude_addresses.append(wan_subnet)

        # LƯU Ý (đã từng loại 1.1.1.1/8.8.8.8/8.8.4.4/1.0.0.1 khỏi route ở đây để "tránh sing-box tự
        # loop khi resolve" — nhưng route_exclude_address áp dụng cho MỌI nguồn, kể cả traffic của
        # phone: nếu phone/app tự hardcode DNS/kết nối tới các IP này (rất phổ biến — Google 8.8.8.8
        # là target connectivity-check mặc định của nhiều app/OS), traffic đó sẽ bị loại khỏi TUN,
        # rơi xuống routing thường của Windows — lúc này GenRouterNAT đã bị gỡ (để sing-box thấy IP
        # gốc của phone) nên source IP private (192.168.10.x) không được NAT, gói tin ra ngoài mất
        # luôn → phone báo "không có internet". Đã xác nhận thực tế: DNS 9.9.9.9 (không bị exclude)
        # chạy được, DNS 8.8.8.8 (bị exclude) thì không.
        # Không cần exclude các IP này nữa: dns_direct/dns_direct_fallback bên dưới đã có
        # "bind_interface": actual_wan — tự ép socket ra thẳng card WAN thật ở tầng OS trước khi
        # TUN kịp route, nên sing-box vẫn không tự loop mà không cần exclude theo destination IP.

        # Exclude proxy IPs to prevent proxy routing loops
        for proxy in config.proxies:
            proxy_ip = proxy.host.strip()
            if proxy_ip:
                try:
                    ipaddress.ip_address(proxy_ip)
                    proxy_cidr = f"{proxy_ip}/32"
                    if proxy_cidr not in exclude_addresses:
                        exclude_addresses.append(proxy_cidr)
                except ValueError:
                    pass

        # Dùng tên cố định để Wintun tái sử dụng driver đã tạo — tránh mất 1-3s khởi tạo adapter mới mỗi lần restart
        config.tun_interface = "GenRouterTUN"

        # ── TUN Inbound ──
        # lan_subnet cho vào inet4_route_address để sing-box nhận traffic TỪ phone,
        # trong khi vẫn dùng inet4_route_exclude_address để traffic ĐẾN LAN không loop.
        # QUAN TRỌNG: GenRouterNAT phải được XÓA trước khi start sing-box,
        # nếu không NAT sẽ masquerade source IP phone thành 172.19.0.1 trước khi
        # vào TUN, khiến source_ip_cidr rules không match được.
        inbounds = [{ 
            "type": "tun",
            "tag": "tun-in",
            "interface_name": config.tun_interface,
            "address": ["172.19.0.1/30"],
            # MTU default 1500: internet không hỗ trợ jumbo frames (9000) — để mặc định
            "auto_route": True,
            "strict_route": False,
            # gvisor: dùng TCP/IP stack riêng của sing-box (userspace), không phụ thuộc bảng
            # route của Windows cho các luồng của chính nó — tránh nhóm lỗi tự-loop/Wintun đã
            # gặp với "system" stack (đánh đổi: overhead cao hơn 1 chút).
            "stack": "gvisor",
            "sniff": False,              # Tắt sniff: không cần detect protocol vì route dựa theo source IP
            "sniff_override_destination": False,  # Tắt: tránh wait thêm 50-200ms/connection
            "route_exclude_address": exclude_addresses
        }]


        # ── Route Rules ──
        # Quan trọng: thứ tự rules rất quan trọng trong sing-box!
        # Rule khớp đầu tiên sẽ thắng. Thứ tự đúng:
        # 1. DNS → dns-out (high priority)
        # 2. Local CIDRs (lo, 10.x, 172.x) → direct
        # 3. Per-device proxy rules (specific /32 sources) → proxy
        # 4. Fallback: toàn bộ LAN subnet → direct (dùng Windows NAT để ra internet)
        lan_subnet_cidr = f"{config.lan_gateway_ip.rsplit('.', 1)[0]}.0/24"
        
        route_rules = [
            # Match theo port 53 thay vì chỉ "protocol": "dns" — inbound TUN có "sniff": false
            # (tắt để tối ưu tốc độ, xem giải thích ở inbounds bên trên) nên sing-box KHÔNG sniff
            # payload để nhận diện "protocol: dns", khiến rule protocol-based không bao giờ khớp.
            # Đã xác nhận qua log thực tế: DNS của thiết bị chưa gán proxy KHÔNG bị hijack về
            # dns_captive (domain kiểm tra captive-portal như captive.apple.com resolve ra IP thật
            # thay vì lan_gateway_ip) — nên điện thoại không bao giờ tự hiện popup đăng nhập mạng.
            # Match theo port không cần sniff nên hoạt động độc lập với cấu hình sniff.
            {"port": 53, "action": "hijack-dns"},
            {"protocol": "dns", "action": "hijack-dns"},
            {"ip_cidr": local_cidrs, "outbound": "direct-out"},
        ]

        # Per-device routing: source IP → proxy outbound (phải đứng TRƯỚC rule fallback LAN)
        # QUAN TRỌNG: route tới proxy dù status == "Die". Trước đây bỏ qua proxy chết để tránh
        # device bị treo request chờ timeout — nhưng hệ quả là device rơi xuống rule fallback LAN
        # → đi thẳng direct-out bằng IP thật, tức là LỘ IP GỐC ngay khi proxy chết mà không có
        # cảnh báo gì (fail-open). Với mục đích fake-IP/anti-detect, phải fail-closed: proxy chết
        # thì device mất mạng qua đúng proxy đó, không bao giờ được âm thầm dùng IP thật.
        # Whitelist / Reject lists
        block_direct = getattr(config, "block_direct_devices", False)
        whitelist_ips = []
        whitelist_macs = []
        for item in getattr(config, "direct_whitelist", []):
            item_clean = item.strip()
            if not item_clean:
                continue
            if "." in item_clean:
                whitelist_ips.append(item_clean)
            elif ":" in item_clean or "-" in item_clean:
                whitelist_macs.append(item_clean.upper().replace("-", ":"))

        # Add explicit rules for Whitelist IPs (even if not in registry yet)
        if block_direct:
            for ip in whitelist_ips:
                route_rules.append({
                    "source_ip_cidr": [f"{ip}/32"],
                    "outbound": "direct-out"
                })

        # Định nghĩa block-out outbound để chặn traffic
        # (outbound block-out được append vào outbounds ở phần sau)
        proxy_tags = [p.id for p in config.proxies]
        has_proxy_assignment = False

        # Pre-provision selector + DNS rule cho TOÀN BỘ dải DHCP pool, kể cả IP chưa có
        # thiết bị nào thực sự dùng — tránh race condition đã xác nhận trên máy thật: thiết
        # bị mới join được cấp DHCP lease gần như ngay lập tức, và hệ điều hành (đặc biệt
        # iOS) tự probe captive-portal-check chỉ 1-2s sau đó — NHANH HƠN nhiều so với thời
        # gian full-restart sing-box cần để "nhận diện thiết bị mới" (~5-10s, phải tạo lại
        # TUN). Probe đầu tiên chắc chắn chạy TRƯỚC khi restart xong, gặp fallback LAN cũ
        # (chưa có rule riêng cho IP này) → domain kiểm tra captive resolve ra IP thật → hệ
        # điều hành kết luận có internet bình thường và KHÔNG BAO GIỜ tự hiện popup nữa (kể
        # cả sau khi forget & rejoin — lần probe tiếp theo vẫn thua cuộc đua y hệt, vì mỗi
        # lần "thiết bị mới" đều phải chờ full-restart). Tạo sẵn rule cho mọi IP trong pool
        # giải quyết triệt để: gói tin/DNS query ĐẦU TIÊN của bất kỳ thiết bị nào cũng đã
        # khớp rule ngay khi sing-box khởi động, không cần chờ hot-reload nhận diện.
        known_ips = {d.ip for d in devices if d.ip}
        unclaimed_pool_ips = []
        try:
            pool_cursor = ipaddress.IPv4Address(config.dhcp_range_start)
            pool_end = ipaddress.IPv4Address(config.dhcp_range_end)
            while pool_cursor <= pool_end:
                ip_str = str(pool_cursor)
                if ip_str not in known_ips:
                    unclaimed_pool_ips.append(ip_str)
                pool_cursor += 1
        except Exception as e:
            logger.warning(f"[SingBox] Failed to enumerate DHCP pool for pre-provisioning: {e}")
        pool_default_target = "block-out" if block_direct else "direct-out"

        seen_ips = set()
        for device in devices:
            ip_clean = device.ip.strip() if device.ip else ""
            if not ip_clean:
                continue
            if ip_clean in seen_ips:
                logger.warning(f"[SingBox] Skipping duplicate device IP {ip_clean} in route rules to prevent config crash.")
                continue
            seen_ips.add(ip_clean)

            is_whitelisted = (
                ip_clean in whitelist_ips or
                (device.mac and device.mac.upper().replace("-", ":") in whitelist_macs)
            )
            assigned_proxy = next((p for p in config.proxies if p.id == device.proxy_id), None)

            # Xác định outbound mặc định cho selector của thiết bị này
            if device.proxy_id and assigned_proxy:
                default_target = device.proxy_id
                has_proxy_assignment = True
            elif block_direct:
                if is_whitelisted:
                    default_target = "direct-out"
                else:
                    default_target = "block-out"
            else:
                default_target = "direct-out"

            # Ánh xạ IP thiết bị tới selector tương ứng của nó
            route_rules.append({
                "source_ip_cidr": [f"{ip_clean}/32"],
                "outbound": f"select_{ip_clean}"
            })

        for ip_str in unclaimed_pool_ips:
            ip_str_clean = ip_str.strip()
            if not ip_str_clean:
                continue
            route_rules.append({
                "source_ip_cidr": [f"{ip_str_clean}/32"],
                "outbound": f"select_{ip_str_clean}"
            })

        # Fallback: Traffic từ toàn bộ LAN subnet
        if block_direct:
            route_rules.append(
                {"source_ip_cidr": [lan_subnet_cidr], "action": "reject", "method": "default"}
            )
        else:
            route_rules.append(
                {"source_ip_cidr": [lan_subnet_cidr], "outbound": "direct-out"}
            )

        # ── Outbounds ──
        actual_wan = config.wan_interface or detect_wan_interface()
        logger.info(f"[SingBox] Using WAN: {actual_wan} (config: {config.wan_interface})")

        outbounds = [
            {
                "type": "direct",
                "tag": "direct-out",
                # Ép direct-out bind thẳng vào card WAN thật (thay vì chỉ dựa vào
                # route.auto_detect_interface). Vì GenRouterTUN có route metric 0
                # cho gần như toàn bộ dải IPv4 (thấp hơn route WAN thật), auto-detect
                # có thể nhận nhầm chính TUN là interface mặc định → direct-out tự
                # loop vào chính nó → mất mạng hoàn toàn (đã xác nhận bằng curl timeout).
                "bind_interface": actual_wan,
            },
            {
                "type": "block",
                "tag": "block-out"
            }
        ]

        # Bộ lọc tags duy nhất để ngăn ngừa lỗi trùng lặp cấu hình outbound
        seen_tags = {"direct-out", "block-out"}

        # Sinh các selector outbounds cho từng thiết bị
        for device in devices:
            ip_clean = device.ip.strip() if device.ip else ""
            if not ip_clean:
                continue
            tag = f"select_{ip_clean}"
            if tag in seen_tags:
                logger.warning(f"[SingBox] Skipping duplicate outbound tag {tag} to prevent config crash.")
                continue
            seen_tags.add(tag)

            is_whitelisted = (
                ip_clean in whitelist_ips or 
                (device.mac and device.mac.upper().replace("-", ":") in whitelist_macs)
            )
            assigned_proxy = next((p for p in config.proxies if p.id == device.proxy_id), None)
            
            if device.proxy_id and assigned_proxy:
                default_target = device.proxy_id
            elif block_direct:
                if is_whitelisted:
                    default_target = "direct-out"
                else:
                    default_target = "block-out"
            else:
                default_target = "direct-out"

            outbounds.append({
                "type": "selector",
                "tag": tag,
                "outbounds": ["direct-out", "block-out"] + proxy_tags,
                "default": default_target
            })

        # Selector cho IP chưa có thiết bị (xem giải thích ở route_rules phía trên). Danh
        # sách outbounds chỉ gồm direct/block (không kèm proxy_tags) để giảm phình config —
        # nếu sau này có người gán proxy thật cho IP này trước khi nó kịp full-restart, lần
        # gọi update_device_routing()/select_outbound_via_api() đầu tiên sẽ fail (proxy chưa
        # là thành viên selector) và tự fallback sang reload_now() để build lại đầy đủ.
        for ip_str in unclaimed_pool_ips:
            ip_str_clean = ip_str.strip()
            if not ip_str_clean:
                continue
            tag = f"select_{ip_str_clean}"
            if tag in seen_tags:
                logger.warning(f"[SingBox] Skipping duplicate unclaimed outbound tag {tag} to prevent config crash.")
                continue
            seen_tags.add(tag)

            outbounds.append({
                "type": "selector",
                "tag": tag,
                "outbounds": ["direct-out", "block-out"] + proxy_tags,
                "default": pool_default_target
            })

        for proxy in config.proxies:
            tag = proxy.id
            if tag in seen_tags:
                logger.warning(f"[SingBox] Skipping duplicate proxy outbound tag {tag} to prevent config crash.")
                continue
            seen_tags.add(tag)

            outbound = {
                "type": "socks" if proxy.type in ("socks5", "socks") else proxy.type,
                "tag": tag,
                "server": proxy.host,
                "server_port": proxy.port,
                # tcp_fast_open tắt: tránh 1 RTT penalty khi proxy không support TFO
                "tcp_fast_open": False,
                # NOTE: tcp_keep_alive_idle chỉ có trong sing-box 1.10+
                # sing-box 1.9.3 tự manage keepalive bên trong
            }
            if proxy.type in ("socks5", "socks"):
                outbound["udp_over_tcp"] = True
            if proxy.username and proxy.password:
                outbound["username"] = proxy.username
                outbound["password"] = proxy.password
            outbounds.append(outbound)


        # ── Route section ──
        route_section = {
            "rules": route_rules,
            "final": "direct-out",
            "auto_detect_interface": True,
            "default_domain_resolver": "dns_direct"  # v1.13.14: dạng chuỗi đơn hoạt động cho default resolver
        }

        # ── DNS section ──
        # Per-proxy DNS routing: DNS từ mỗi device đi qua đúng proxy của device đó
        # Ngăn DNS leak: nếu DNS đi direct (VN) nhưng TCP đi qua proxy (US)
        # → site so sánh DNS country ≠ IP country → bị phát hiện proxy ngay
        # 
        # DNS direct dùng UDP truyền thống (không chặn) để đảm bảo luôn có mạng trực tiếp,
        # DNS proxy dùng TCP để tối ưu ghép kênh qua proxy SOCKS5/HTTP.
        dns_servers = [
            {
                "tag": "dns_direct",
                "type": "udp",
                "server": "1.1.1.1",   # Cloudflare UDP DNS — không dùng detour (v1.13+ cấm detour direct-out)
                # bind_interface thay cho detour: ép query UDP đi thẳng card WAN thật, tránh
                # tự loop vào GenRouterTUN (route của TUN có metric thấp hơn route WAN thật
                # cho gần hết dải IPv4 — xem giải thích ở outbound "direct-out" bên trên).
                "bind_interface": actual_wan,
            },
            {
                "tag": "dns_direct_fallback",
                "type": "udp",
                "server": "8.8.8.8",   # Google UDP DNS — fallback, không dùng detour
                "bind_interface": actual_wan,
            },
        ]
        dns_rules = []


        # Định tuyến DNS động qua Selector cho từng thiết bị
        # Để tránh việc full-restart sing-box khi thay đổi gán proxy hoặc Reset Direct,
        # chúng ta sẽ gán cho mỗi IP thiết bị một DNS server tĩnh trỏ detour vào selector tương ứng.
        selector_ips = []
        for device in devices:
            ip_clean = device.ip.strip() if device.ip else ""
            if ip_clean and ip_clean not in selector_ips:
                selector_ips.append(ip_clean)
        for ip_str in unclaimed_pool_ips:
            ip_str_clean = ip_str.strip()
            if ip_str_clean and ip_str_clean not in selector_ips:
                selector_ips.append(ip_str_clean)

        pending_dns_buckets = {}
        for ip in selector_ips:
            dns_servers.append({
                "tag": f"dns_dev_{ip}",
                "type": "tcp",
                "server": "1.1.1.1",
                "detour": f"select_{ip}"
            })
            dns_rules.append({
                "source_ip_cidr": [f"{ip}/32"],
                "server": f"dns_dev_{ip}"
            })
            pending_dns_buckets[ip] = f"dns_dev_{ip}"

        self._pending_dns_buckets = pending_dns_buckets

        sb_config = {
            "log": {
                "level": "warn",        # Giảm từ info → warn: ít I/O hơn, tốc độ cao hơn
                "output": SINGBOX_LOG,
                "timestamp": True
            },
            "dns": {
                "servers": dns_servers,
                "rules": dns_rules,
                "final": "dns_direct",
                "strategy": "prefer_ipv4",      # Ưu tiên IPv4, tránh IPv6 fallback chậm
                # NOTE: lazy_cache và disable_expire chỉ có trong sing-box 1.10+
                # với sing-box 1.9.3: chỉ dùng independent_cache (có sẵn)
                "independent_cache": True,   # Cache DNS độc lập cho mỗi DNS server
                "cache_capacity": 8192       # Hỗ trợ trên sing-box v1.11.0+, tối ưu dung lượng cache DNS
            },
            "inbounds": inbounds,
            "outbounds": outbounds,
            "route": route_section,
            "experimental": {
                "clash_api": {
                    "external_controller": "127.0.0.1:26990",
                    "external_ui": ""
                }
            }
        }


        with open(SINGBOX_CONFIG, "w", encoding="utf-8") as f:
            json.dump(sb_config, f, indent=2, ensure_ascii=False)

        logger.info(
            f"[SingBox] Config generated: {len(config.proxies)} proxies, "
            f"{len(devices)} devices, {sum(1 for d in devices if d.proxy_id)} assigned"
        )
        return True

    # ─── Process Lifecycle ───────────────────────────────

    def start(self, _retry=True):
        """Start sing-box process. Cross-platform: Windows uses elevated PS, Linux/macOS runs directly."""
        from app.platform import get_singbox_binary_name, download_binaries
        _IS_WINDOWS = platform.system() == "Windows"

        # Dừng instance cũ nhưng KHÔNG khôi phục NAT: ngay bên dưới ta sẽ remove_nat lại
        # để sing-box thấy source IP thật của phone. Khôi phục rồi remove ngay chỉ tổ tốn
        # 5-16s setup_nat (Hyper-V/WinNat + retry) vô ích mỗi lần restart → kéo dài cửa sổ
        # mất mạng và tăng nguy cơ Wintun kẹt vì restart quá lâu.
        self.stop(restore_nat=False)

        # Auto-download binaries if missing (platform-aware)
        _binary_name = get_singbox_binary_name()  # 'sing-box.exe' on Win, 'sing-box' on Unix
        _singbox_bin = os.path.join(PROJECT_DIR, _binary_name)
        _wintun_dll = os.path.join(PROJECT_DIR, "wintun.dll")

        _needs_download = not os.path.exists(_singbox_bin)
        if _IS_WINDOWS:
            _needs_download = _needs_download or not os.path.exists(_wintun_dll)

        if _needs_download:
            logger.info(f"[SingBox] Missing {_binary_name}. Attempting auto-download...")
            try:
                download_binaries()
            except Exception as e:
                logger.error(f"[SingBox] Auto-download failed: {e}")

        # Check binary again
        if not os.path.exists(_singbox_bin):
            self.last_error = f"Không tìm thấy {_binary_name} và tải tự động thất bại."
            logger.error(f"[SingBox] {self.last_error}")
            from app.error_reporter import report_error
            report_error("SingBox", self.last_error, level="critical")
            return

        if _IS_WINDOWS and not os.path.exists(_wintun_dll):
            self.last_error = "Không tìm thấy wintun.dll và tải tự động thất bại."
            logger.error(f"[SingBox] {self.last_error}")
            from app.error_reporter import report_error
            report_error("SingBox", self.last_error, level="critical")
            return

        if not os.path.exists(SINGBOX_CONFIG):
            self.last_error = "Không tìm thấy file cấu hình sing-box-config.json."
            logger.error(f"[SingBox] {self.last_error}")
            return

        # Truncate log if too large
        if os.path.exists(SINGBOX_LOG):
            try:
                if os.path.getsize(SINGBOX_LOG) > 10 * 1024 * 1024:
                    with open(SINGBOX_LOG, "w") as f:
                        f.truncate(0)
                    logger.info("[SingBox] Truncated oversized log file.")
            except Exception:
                pass

        # CRITICAL FIX: Remove GenRouterNAT before starting sing-box.
        # Windows NAT masquerades phone IPs (192.168.10.x) → TUN IP (172.19.0.1)
        # before entering sing-box, breaking source_ip_cidr routing rules.
        # sing-box direct-out with auto_detect_interface=True handles masquerade itself.
        _lan_subnet = "192.168.10.0/24"
        try:
            import ipaddress as _ip
            _lan_subnet = str(_ip.ip_interface(f"{self._config.lan_gateway_ip}/24").network)
        except Exception:
            pass
        remove_nat_for_singbox(lan_subnet=_lan_subnet)

        # Cleanup orphan TUN interface trước khi start để tránh lỗi:
        # "Cannot create a file when that file already exists"
        # Xảy ra khi server cũ bị kill đột ngột, TUN interface không được giải phóng.
        # Cũng fix: "open interface take too much time to finish" — Wintun driver cần thời gian
        # để fully release sau khi process cũ chết. Nếu start ngay → timeout → crash → watchdog restart.
        try:
            # Lấy tên tun-interface từ config (mặc định là "tun-in")
            _tun_name = "tun-in"
            try:
                import json as _json
                with open(SINGBOX_CONFIG, "r", encoding="utf-8") as _cf:
                    _sb_cfg = _json.load(_cf)
                for _inbound in _sb_cfg.get("inbounds", []):
                    if _inbound.get("type") == "tun":
                        _tun_name = _inbound.get("interface_name", _tun_name)
                        break
            except Exception:
                pass
            _r = subprocess.run(
                ["netsh", "interface", "delete", "interface", f"name={_tun_name}"],
                capture_output=True, text=True, timeout=5
            )
            if _r.returncode == 0:
                logger.info(f"[SingBox] Cleaned up orphan TUN interface '{_tun_name}' before start.")
                # Wintun driver cần vài giây để fully release kernel handle sau khi delete.
                # Không sleep → sing-box mở adapter quá sớm → "open interface take too much time" → crash.
                time.sleep(2)
            else:
                # Adapter không tồn tại → lần đầu chạy. Wintun driver có thể cần load.
                # Sleep ngắn để tránh race condition với driver initialization.
                time.sleep(0.5)
        except Exception as _e:
            logger.debug(f"[SingBox] TUN cleanup (pre-start): {_e}")

        try:
            # Create PowerShell script to start sing-box elevated
            ps_script = os.path.join(PROJECT_DIR, "_start_singbox.ps1")
            # Clear previous log files
            for f_log in [os.path.join(PROJECT_DIR, "sing-box-out.log"), os.path.join(PROJECT_DIR, "sing-box-err.log")]:
                if os.path.exists(f_log):
                    try:
                        os.remove(f_log)
                    except Exception:
                        pass
            with open(ps_script, "w", encoding="utf-8") as f:
                f.write(f'''$proc = Start-Process "{SINGBOX_EXE}" -ArgumentList "run -c `"{SINGBOX_CONFIG}`"" -WorkingDirectory "{PROJECT_DIR}" -NoNewWindow -RedirectStandardOutput "{PROJECT_DIR}\\sing-box-out.log" -RedirectStandardError "{PROJECT_DIR}\\sing-box-err.log" -PassThru
$proc.Id | Out-File -FilePath "{SINGBOX_PID_FILE}" -Encoding ascii -NoNewline
''')

            # Run elevated
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command",
                 f"Start-Process powershell "
                 f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass',"
                 f"'-File','{ps_script}' "
                 f"-Verb RunAs -WindowStyle Hidden"],
                cwd=PROJECT_DIR,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            # Wait for PID file (max 15s)
            for _ in range(30):
                time.sleep(0.5)
                if os.path.exists(SINGBOX_PID_FILE):
                    try:
                        with open(SINGBOX_PID_FILE, "r") as f:
                            content = f.read().strip()
                        if content and content.isdigit():
                            break
                    except Exception:
                        pass

            time.sleep(3)  # Wait for TUN interface creation

            # PID file tồn tại chỉ chứng minh process ĐÃ ĐƯỢC khởi chạy, không chứng minh nó
            # còn sống — sing-box có thể crash ngay sau đó (vd Wintun bị kẹt trạng thái
            # nửa-tạo-nửa-xoá sau force-kill liên tục, lỗi FATAL "create adapter: already
            # exists" / "Element not found"). Xác minh bằng Get-Process (giống property
            # is_running — KHÔNG dùng tasklist: đã xác nhận tasklist không thấy được
            # sing-box.exe chạy qua UAC lồng nhau, dù process vẫn sống) thay vì chỉ tin PID
            # file, để "Started successfully" phản ánh đúng thực tế.
            pid_str = None
            if os.path.exists(SINGBOX_PID_FILE):
                with open(SINGBOX_PID_FILE, "r") as f:
                    pid_str = f.read().strip()

            process_alive = False
            if pid_str and pid_str.isdigit():
                pid = int(pid_str)
                try:
                    import psutil
                    if psutil.pid_exists(pid):
                        proc = psutil.Process(pid)
                        process_alive = "sing-box" in proc.name().lower()
                except Exception:
                    try:
                        os.kill(pid, 0)
                        process_alive = True
                    except OSError:
                        process_alive = False

            if not process_alive:
                err_log_path = os.path.join(PROJECT_DIR, "sing-box-err.log")
                err_content = ""
                if os.path.exists(err_log_path):
                    try:
                        with open(err_log_path, "r", encoding="utf-8", errors="ignore") as f:
                            err_content = f.read().strip()
                    except Exception:
                        pass

                if _retry:
                    logger.warning(
                        f"[SingBox] Process not alive after start (PID file: {pid_str!r}). "
                        f"err log: {err_content[:300] or '(empty)'}. Retrying once after cooldown..."
                    )
                    subprocess.run(["taskkill", "/F", "/IM", "sing-box.exe"], capture_output=True, text=True, timeout=5)
                    try:
                        os.remove(SINGBOX_PID_FILE)
                    except Exception:
                        pass
                    time.sleep(5)  # Cho driver Wintun/OS thêm thời gian giải phóng resource cũ
                    self.start(_retry=False)
                    return
                else:
                    self._is_running = False
                    self.last_error = err_content[:300] or "Process exited immediately (no error log captured)"
                    logger.error(
                        f"[SingBox] Still not alive after retry. err log: {err_content[:300] or '(empty)'}. "
                        f"Manual investigation/reboot may be required."
                    )
                    from app.error_reporter import report_error
                    report_error(
                        "SingBox", self.last_error, level="critical",
                        context={"pid_file_content": pid_str, "err_log_tail": err_content[:2000]},
                    )
                    return

            self._is_running = True
            self.last_error = None
            logger.info(f"[SingBox] Started successfully (PID: {pid_str}, verified alive)")

            # Adjust Wintun interface metric to 8 so that TUN is preferred over WAN (metric 10)
            # while LAN (metric 5) is still preferred for limited broadcasts.
            tun_name = self._config.tun_interface
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"Set-NetIPInterface -InterfaceAlias '{tun_name}' -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 8 -PolicyStore ActiveStore -ErrorAction SilentlyContinue; "
                     f"Set-NetIPInterface -InterfaceAlias '{tun_name}' -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 8 -PolicyStore PersistentStore -ErrorAction SilentlyContinue"],
                    capture_output=True, text=True, timeout=5
                )
                logger.info(f"[SingBox] Adjusted {tun_name} metric to 8")
            except Exception as e_metric:
                logger.warning(f"[SingBox] Failed to adjust {tun_name} metric: {e_metric}")

            # sing-box tự gán DNSServer = 172.19.0.2 (địa chỉ /30 rỗng, không có gì lắng nghe)
            # cho adapter TUN. Vì "sniff": false, sing-box không nhận diện được DNS-qua-TCP
            # tới đúng địa chỉ này là "protocol: dns" nên hijack-dns không bắt được, request
            # rơi xuống direct-out và cố dial tới 172.19.0.2 (không tồn tại) → treo/timeout
            # lặp lại liên tục, kéo chậm cả các kết nối khác. Xoá DNS server khỏi adapter này
            # để Windows không bao giờ tự ý gửi truy vấn tới đó nữa.
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"Set-DnsClientServerAddress -InterfaceAlias '{tun_name}' -ResetServerAddresses -ErrorAction SilentlyContinue"],
                    capture_output=True, text=True, timeout=5
                )
                logger.info(f"[SingBox] Cleared auto-assigned DNS server on {tun_name}")
            except Exception as e_dns:
                logger.warning(f"[SingBox] Failed to clear {tun_name} DNS server: {e_dns}")

            # Cleanup script
            try:
                os.remove(ps_script)
            except Exception:
                pass

        except Exception as e:
            logger.error(f"[SingBox] Failed to start: {e}")

    def stop(self, restore_nat: bool = True):
        """Stop sing-box process.

        restore_nat=True (mặc định): khôi phục GenRouterNAT để thiết bị đi direct vẫn ra
        được internet qua Windows NAT khi sing-box tắt hẳn.
        restore_nat=False: dùng khi stop chỉ là 1 bước trong quá trình restart (start() sẽ
        remove NAT ngay sau đó) — tránh churn NAT tốn kém, xem giải thích ở start().
        """
        if platform.system() != "Windows":
            subprocess.run(
                "pkill -f 'sing-box'", shell=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self._is_running = False
            return

        # Kill by PID
        if os.path.exists(SINGBOX_PID_FILE):
            try:
                with open(SINGBOX_PID_FILE, "r") as f:
                    pid_str = f.read().strip()
                if pid_str.isdigit():
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid_str],
                        capture_output=True, text=True, timeout=5
                    )
                    logger.info(f"[SingBox] Stopped (PID: {pid_str})")
            except Exception as e:
                logger.warning(f"[SingBox] Error killing by PID: {e}")
            try:
                os.remove(SINGBOX_PID_FILE)
            except Exception:
                pass

        # Fallback: kill by name
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "sing-box.exe"],
                capture_output=True, text=True, timeout=5
            )
            logger.info("[SingBox] Stopped any leftover sing-box.exe instances by name.")
        except Exception as e:
            logger.warning(f"[SingBox] Error killing by name: {e}")

        self._is_running = False
        time.sleep(2)  # Wait for TUN interface release
        logger.info("[SingBox] Stopped.")

        if not restore_nat:
            return

        # Restore GenRouterNAT so direct-traffic devices can still access internet
        wan_interface = self._config.wan_interface or detect_wan_interface()
        lan_subnet = "192.168.10.0/24"
        try:
            ip_obj = __import__('ipaddress').ip_interface(f"{self._config.lan_gateway_ip}/24")
            lan_subnet = str(ip_obj.network)
        except Exception:
            pass
        restore_nat_for_singbox(wan_interface=wan_interface, lan_subnet=lan_subnet)

    def hot_reload(self):
        """Schedule a reload, debouncing rapid calls.
        Fire-and-forget: dùng cho các thao tác không cần biết ngay kết quả (vd nhiều
        thay đổi dồn dập). Muốn biết routing có THỰC SỰ được áp dụng hay không (để
        trả đúng success/error cho client thay vì báo giả), dùng reload_now().
        """
        with self._reload_lock:
            if self._reload_timer:
                self._reload_timer.cancel()
            self._reload_timer = threading.Timer(1.0, self._do_hot_reload)
            self._reload_timer.start()

    def select_outbound_via_api(self, device_ip: str, target_outbound: str) -> bool:
        """Gửi request tới Clash API của sing-box để thay đổi selector của thiết bị,
        giúp áp dụng proxy ngay lập tức mà không làm mất mạng.
        """
        import urllib.request
        import urllib.error
        import json
        
        selector_tag = f"select_{device_ip}"
        
        try:
            url = f"http://127.0.0.1:26990/proxies/{selector_tag}"
            data = json.dumps({"name": target_outbound}).encode("utf-8")
            req = urllib.request.Request(
                url, 
                data=data, 
                headers={"Content-Type": "application/json"},
                method="PUT"
            )
            with urllib.request.urlopen(req, timeout=2) as response:
                if response.status in (200, 204):
                    logger.info(f"[SingBox] Switched selector {selector_tag} to {target_outbound} via Clash API.")
                    return True
        except urllib.error.URLError as e:
            logger.warning(f"[SingBox] Clash API request failed for selector {selector_tag}: {e}")
        except Exception as e:
            logger.warning(f"[SingBox] Unexpected error switching selector {selector_tag}: {e}")
            
        return False

    def _dns_bucket_for(self, device_ip: str, proxy_id: Optional[str]) -> str:
        """Bucket DNS mà 1 device sẽ dùng nếu được gán proxy_id này."""
        return f"dns_dev_{device_ip}"

    def update_device_routing(self, device_ip: str, proxy_id: Optional[str]) -> bool:
        """Cập nhật routing cho một thiết bị cụ thể qua Clash API (Zero-Downtime).
        Nếu API thất bại (thiết bị mới chưa có selector), tự động fallback về full restart.
        """
        if not self.is_running:
            return self.reload_now()

        # dns.rules là phần TĨNH của sing-box — chỉ nạp lại được qua full-restart, Clash API
        # không đổi được. Nếu bucket DNS mong muốn khác với bucket ĐANG THỰC SỰ chạy (vd
        # device đã có proxy trước đó, giờ đổi sang proxy khác/Direct), đổi selector qua API
        # thôi là KHÔNG ĐỦ: DNS của device sẽ vẫn âm thầm đi qua proxy CŨ dù outbound đã đổi
        # đúng — đã xác nhận đúng là nguyên nhân "Reset Direct không hoạt động khi thiết bị đã
        # từng được gán proxy". Trường hợp này bắt buộc phải full-restart để áp dụng đúng.
        desired_bucket = self._dns_bucket_for(device_ip, proxy_id)
        current_bucket = self._live_device_dns_bucket.get(device_ip, "dns_direct")
        if desired_bucket != current_bucket:
            logger.info(
                f"[SingBox] DNS bucket cho {device_ip} cần đổi ({current_bucket} → "
                f"{desired_bucket}) — full-restart để áp dụng đúng DNS."
            )
            return self.reload_now()

        target_outbound = "direct-out"
        if proxy_id:
            proxy_exists = any(p.id == proxy_id for p in self._config.proxies)
            if proxy_exists:
                target_outbound = proxy_id
            else:
                target_outbound = "block-out" if getattr(self._config, "block_direct_devices", False) else "direct-out"
        else:
            is_whitelisted = False
            for item in getattr(self._config, "direct_whitelist", []):
                if item.strip() == device_ip:
                    is_whitelisted = True
                    break
            if getattr(self._config, "block_direct_devices", False) and not is_whitelisted:
                target_outbound = "block-out"
            else:
                target_outbound = "direct-out"

        if self.select_outbound_via_api(device_ip, target_outbound):
            # Ghi đè file config.json mới lên đĩa để lưu trạng thái
            self.generate_config()
            return True

        logger.info(f"[SingBox] API update failed. Falling back to full restart for {device_ip}...")
        return self.reload_now()

    def update_multiple_devices_routing(self, assignments: list) -> bool:
        """Cập nhật routing cho nhiều thiết bị cùng lúc qua Clash API (Zero-Downtime).
        assignments: list of tuple (device_ip, proxy_id)
        """
        if not self.is_running:
            return self.reload_now()

        # Nếu BẤT KỲ assignment nào đổi bucket DNS (xem giải thích ở update_device_routing()),
        # cả batch phải full-restart — không có ích gì khi vừa API-switch xong cho những cái
        # không cần đổi DNS, vừa vẫn phải restart cho cái cần đổi; restart 1 lần xử lý đúng
        # cho toàn bộ batch trong 1 lượt.
        needs_restart = any(
            self._dns_bucket_for(ip, proxy_id) != self._live_device_dns_bucket.get(ip, "dns_direct")
            for ip, proxy_id in assignments
        )
        if needs_restart:
            logger.info(
                "[SingBox] Ít nhất 1 thiết bị trong batch cần đổi bucket DNS — full-restart "
                "cho toàn bộ batch để áp dụng đúng."
            )
            return self.reload_now()

        all_ok = True
        for ip, proxy_id in assignments:
            target_outbound = "direct-out"
            if proxy_id:
                proxy_exists = any(p.id == proxy_id for p in self._config.proxies)
                if proxy_exists:
                    target_outbound = proxy_id
                else:
                    target_outbound = "block-out" if getattr(self._config, "block_direct_devices", False) else "direct-out"
            else:
                is_whitelisted = False
                for item in getattr(self._config, "direct_whitelist", []):
                    if item.strip() == ip:
                        is_whitelisted = True
                        break
                if getattr(self._config, "block_direct_devices", False) and not is_whitelisted:
                    target_outbound = "block-out"
                else:
                    target_outbound = "direct-out"
                    
            if not self.select_outbound_via_api(ip, target_outbound):
                all_ok = False
                
        # Ghi đè file config.json mới lên đĩa
        self.generate_config()
        
        if all_ok:
            return True
            
        logger.info("[SingBox] API update failed. Falling back to full restart for bulk assignments...")
        return self.reload_now()

    def reload_now(self) -> bool:
        """Đồng bộ: áp dụng config ngay lập tức bằng cách khởi động lại tiến trình.

        Giữ _reload_lock trong SUỐT quá trình _apply() (không chỉ lúc huỷ timer) — trước
        đây chỉ giữ lock để huỷ debounce timer rồi NHẢ RA trước khi gọi _apply(), nên nếu
        2 reload chồng lấn nhau (vd gán proxy mới gọi reload_now() ngay lúc 1 hot_reload()
        khác — từ DHCP lease renew, ARP auto-register, watchdog...) đang thực sự chạy
        stop()/start(), CẢ HAI cùng đụng vào cùng 1 tiến trình sing-box không đồng bộ: 1
        luồng start() xong verify "alive", rồi luồng kia stop() giết ngay tiến trình đó
        vài giây sau — đúng triệu chứng đã gặp: "Started successfully" rồi watchdog báo
        "not alive" ngay sau đó mỗi khi gán proxy mới. Giữ lock xuyên suốt _apply() đảm
        bảo tại một thời điểm chỉ có đúng 1 luồng được stop()/start() sing-box.

        Coalescing (tránh "restart storm"): nếu 1 luồng khác ĐANG restart khi hàm này được
        gọi (vd người dùng bấm "Lưu"/"Reset Direct" nhiều lần liên tiếp vì không thấy phản
        hồi ngay — đã xác nhận trên máy thật: 1 lần reset bị dồn thành 7 lần full-restart
        liên tiếp, kéo dài ~40s, xem router_manager.log 11:18:24–11:19:02), KHÔNG tự làm
        thêm 1 lần restart độc lập nữa mà chỉ chờ luồng kia xong rồi dùng luôn kết quả đó —
        vì mac_registry.set_proxy() của mọi route luôn chạy TRƯỚC khi gọi reload_now(), nên
        generate_config() của luồng đang restart chắc chắn đã đọc được thay đổi mới nhất
        (miễn _apply_epoch tăng lên SAU khi ta chụp epoch_before, tức lần generate_config()
        đó chạy sau thời điểm ta có mặt).
        """
        epoch_before = self._apply_epoch
        acquired_immediately = self._reload_lock.acquire(blocking=False)
        if not acquired_immediately:
            with self._reload_lock:
                if self._apply_epoch > epoch_before:
                    logger.info(
                        "[SingBox] reload_now() coalesced: 1 lần apply khác vừa chạy xong "
                        "sau khi request này bắt đầu — bỏ qua restart thừa."
                    )
                    return self.is_running
                if self._reload_timer:
                    self._reload_timer.cancel()
                    self._reload_timer = None
                return self._apply()
        try:
            if self._reload_timer:
                self._reload_timer.cancel()
                self._reload_timer = None
            return self._apply()
        finally:
            self._reload_lock.release()

    def _apply(self) -> bool:
        self._apply_epoch += 1
        has_devices = self.generate_config()
        if not has_devices:
            self.stop()  # tắt hẳn → khôi phục NAT cho thiết bị đi direct
            self._live_device_dns_bucket = {}
            logger.info("[SingBox] No devices with proxy → stopped.")
            return True

        # start() tự dừng instance đang chạy (nếu có) bằng stop(restore_nat=False) rồi khởi
        # động lại — không tách stop()+start() ở đây nữa vì như thế stop() bị gọi 2 lần
        # (1 lần ở đây + 1 lần trong start()), khôi phục rồi lại gỡ NAT 2 lượt, restart lâu
        # gấp đôi và tăng nguy cơ Wintun kẹt.
        logger.info("[SingBox] Applying config (start/restart sing-box)...")
        self.start()
        if self.is_running:
            # "Chốt" bucket DNS vừa generate_config() tính — tiến trình sing-box MỚI vừa khởi
            # động chắc chắn đã nạp đúng bucket này (start() luôn dùng file SINGBOX_CONFIG mà
            # generate_config() ở trên vừa ghi).
            self._live_device_dns_bucket = dict(self._pending_dns_buckets)
        return self.is_running

    def _do_hot_reload(self):
        logger.info("[SingBox] Hot-reloading (Debounced)...")
        with self._reload_lock:
            if not self._apply():
                logger.error(f"[SingBox] Hot-reload failed to apply routing: {self.last_error}")

    def apply_routing(self, config: Optional[AppConfig] = None):
        """Full routing apply: generate config → start/restart sing-box.

        Convenience method for use in API endpoints.
        """
        if config:
            self._config = config
        self.hot_reload()
