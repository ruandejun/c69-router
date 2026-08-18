"""
PhoneFarm GenRouter v2.0 — DHCP Server

DHCP Server tích hợp Python, lắng nghe trên UDP port 67.
Cấp IP cho thiết bị qua card LAN, hỗ trợ:
- MAC sticky IP (cấp lại cùng IP khi reconnect)
- Auto-detect thiết bị mới → tạo device entry trong MAC Registry
- Callback khi lease mới → trigger routing update

Yêu cầu quyền Administrator (bind port 67).
"""

import socket
import struct
import threading
import queue
import logging
import time
import ipaddress
from typing import Optional, Callable

logger = logging.getLogger(__name__)

# DHCP Message Types
DHCP_DISCOVER = 1
DHCP_OFFER = 2
DHCP_REQUEST = 3
DHCP_DECLINE = 4
DHCP_ACK = 5
DHCP_NAK = 6
DHCP_RELEASE = 7
DHCP_INFORM = 8

# DHCP Options
OPT_SUBNET_MASK = 1
OPT_ROUTER = 3
OPT_DNS = 6
OPT_HOSTNAME = 12
OPT_REQUESTED_IP = 50
OPT_LEASE_TIME = 51
OPT_MSG_TYPE = 53
OPT_SERVER_ID = 54
OPT_PARAM_REQ = 55
OPT_END = 255

# Magic cookie
DHCP_MAGIC = b'\x63\x82\x53\x63'


def _ip_to_bytes(ip_str: str) -> bytes:
    """Convert IP string to 4 bytes."""
    return socket.inet_aton(ip_str)


def _bytes_to_ip(b: bytes) -> str:
    """Convert 4 bytes to IP string."""
    return socket.inet_ntoa(b)


def _mac_bytes_to_str(b: bytes) -> str:
    """Convert MAC bytes (6 bytes) to uppercase colon-separated string."""
    return ":".join(f"{byte:02X}" for byte in b[:6])


def _parse_options(data: bytes) -> dict:
    """Parse DHCP options from raw data (after magic cookie)."""
    options = {}
    i = 0
    while i < len(data):
        opt_code = data[i]
        if opt_code == OPT_END:
            break
        if opt_code == 0:  # Padding
            i += 1
            continue
        if i + 1 >= len(data):
            break
        opt_len = data[i + 1]
        if i + 2 + opt_len > len(data):
            break
        opt_data = data[i + 2: i + 2 + opt_len]
        options[opt_code] = opt_data
        i += 2 + opt_len
    return options


class DHCPServer:
    """DHCP Server tích hợp cho GenRouter.
    
    Args:
        server_ip: IP của card LAN (gateway cho devices)
        subnet_mask: Subnet mask (default 255.255.255.0)
        dns_server: DNS server cấp cho devices
        pool_start: Đầu dải DHCP pool
        pool_end: Cuối dải DHCP pool
        lease_time: Thời gian lease (seconds)
        mac_registry: MACRegistry instance
        on_lease_callback: Callback(mac, ip, name) khi lease mới/renew
        interface_ip: IP để bind socket (thường là server_ip hoặc 0.0.0.0)
    """

    def __init__(
        self,
        server_ip: str = "192.168.10.1",
        subnet_mask: str = "255.255.255.0",
        dns_server: str = "9.9.9.9",
        pool_start: str = "192.168.10.100",
        pool_end: str = "192.168.10.200",
        lease_time: int = 3600,
        mac_registry=None,
        on_lease_callback: Optional[Callable] = None,
        interface_name: str = "Ethernet 3",
    ):
        self.server_ip = server_ip
        self.subnet_mask = subnet_mask
        self.dns_server = dns_server
        self.pool_start = pool_start
        self.pool_end = pool_end
        self.lease_time = lease_time
        self.mac_registry = mac_registry
        self.on_lease = on_lease_callback
        self.interface_name = interface_name
        self._sock = None
        self._send_sock = None
        self._running = False
        self._thread = None
        # Tracking DHCP readiness — not just "running" but "able to serve clients"
        self._sender_ready = False       # True khi sender socket bind dung LAN IP
        self._sender_degraded = False    # True khi sender = recv socket (fallback, co the bi TUN bat)
        self._startup_error = ""         # Loi cuoi cung khi start
        self._last_offer_time = 0.0      # Timestamp lan cuoi gui OFFER
        self._last_ack_time = 0.0        # Timestamp lan cuoi gui ACK
        self._total_offers = 0
        self._total_acks = 0
        self._total_naks = 0
        # Hang doi xu ly on_lease callback trong 1 thread nen RIENG, tach khoi
        # _listen_loop() — xem giai thich chi tiet o _lease_worker_loop().
        self._lease_queue: "queue.Queue" = queue.Queue()
        self._lease_worker_thread = None
        self.host_macs = self._get_host_macs()

    def _get_host_macs(self) -> set:
        """Get all MAC addresses of the host machine to prevent DHCP loops."""
        macs = set()
        
        # Method 1: Using PowerShell (native on Windows, very reliable for getting all adapter MACs)
        import platform
        if platform.system() == "Windows":
            try:
                import subprocess
                cmd = ["powershell", "-NoProfile", "-Command", "Get-NetAdapter -ErrorAction SilentlyContinue | Select-Object -ExpandProperty MacAddress"]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if res.returncode == 0:
                    for line in res.stdout.splitlines():
                        line = line.strip()
                        if line:
                            clean = line.replace("-", ":").upper()
                            if len(clean) == 17 and all(c in '0123456789ABCDEF:' for c in clean):
                                macs.add(clean)
            except Exception as e:
                logger.warning(f"[DHCP] Failed to get host MACs via PowerShell: {e}")
                
        # Method 2: Using psutil (if installed)
        try:
            import psutil
            addrs = psutil.net_if_addrs()
            for interface, interface_addrs in addrs.items():
                for addr in interface_addrs:
                    if hasattr(psutil, 'AF_LINK') and addr.family == psutil.AF_LINK:
                        mac = addr.address.replace("-", ":").upper()
                        if mac:
                            macs.add(mac)
                    elif addr.family == -1 or ":" in addr.address or "-" in addr.address:
                        clean = addr.address.replace("-", ":").upper()
                        if len(clean) == 17 and all(c in '0123456789ABCDEF:' for c in clean):
                            macs.add(clean)
        except Exception as e:
            if not macs:
                logger.debug(f"[DHCP] Failed to get host MACs via psutil: {e}")
            
        # Method 3: Fallback using uuid
        try:
            import uuid
            node_mac = ':'.join(['{:02X}'.format((uuid.getnode() >> ele) & 0xff) for ele in range(0,8*6,8)][::-1])
            macs.add(node_mac)
        except Exception:
            pass
            
        logger.info(f"[DHCP] Detected host MAC addresses to exclude from leasing: {macs}")
        return macs

    def _get_interface_index(self) -> Optional[int]:
        """Get interface index from friendly interface name using powershell."""
        import platform
        if platform.system() != "Windows":
            return None
        import subprocess
        try:
            cmd = [
                "powershell", "-NoProfile", "-Command",
                f"(Get-NetIPInterface -InterfaceAlias '{self.interface_name}' -AddressFamily IPv4 -ErrorAction SilentlyContinue).InterfaceIndex"
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            out = res.stdout.strip()
            if out.isdigit():
                return int(out)
        except Exception as e:
            logger.error(f"[DHCP] Failed to get interface index for {self.interface_name}: {e}")
        return None

    def start(self) -> bool:
        """Start DHCP server on UDP port 67 with dual-socket setup.

        Returns True if started successfully (at least receiver socket OK).
        Sets self._sender_ready and self._sender_degraded for detailed status.
        """
        if self._running:
            logger.warning("[DHCP] Server already running.")
            return True

        self._startup_error = ""
        self._sender_ready = False
        self._sender_degraded = False

        try:
            # 1. Receiver socket: binds to 0.0.0.0:67 to capture broadcasts from all interfaces
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._sock.settimeout(2.0)  # Allow periodic stop checks
            self._sock.bind(("0.0.0.0", 67))

            # 2. Sender socket: binds to self.server_ip:67 to force routing out LAN interface (Ethernet 3)
            # This is critical when Wintun is the default gateway (sing-box TUN mode)
            try:
                self._send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                
                # Force outgoing broadcasts/multicasts out of LAN interface using IP_MULTICAST_IF and IP_UNICAST_IF
                _if_index_ok = False
                try:
                    self._send_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.server_ip))
                    ifIndex = self._get_interface_index()
                    if ifIndex:
                        # IP_UNICAST_IF = 31 on Windows. Forces all traffic out of specified interface index.
                        self._send_sock.setsockopt(socket.IPPROTO_IP, 31, struct.pack("!I", ifIndex))
                        _if_index_ok = True
                        logger.info(f"[DHCP] Force interface binding to index {ifIndex} ({self.interface_name}) using IP_UNICAST_IF")
                    else:
                        logger.warning(f"[DHCP] Could not get interface index for '{self.interface_name}' — sender may route incorrectly")
                except Exception as e_opt:
                    logger.warning(f"[DHCP] Failed to set interface-forcing socket options: {e_opt}")

                self._send_sock.bind((self.server_ip, 67))
                self._sender_ready = True
                logger.info(f"[DHCP] ✅ Sender socket bound to {self.server_ip}:67 (interface_index={'OK' if _if_index_ok else 'MISSING'})")
            except Exception as e:
                logger.warning(
                    f"[DHCP] Could not bind send socket to {self.server_ip}:67 ({e}). "
                    f"Trying dynamic port on {self.server_ip}..."
                )
                try:
                    self._send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    self._send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    self._send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    try:
                        self._send_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.server_ip))
                        ifIndex = self._get_interface_index()
                        if ifIndex:
                            self._send_sock.setsockopt(socket.IPPROTO_IP, 31, struct.pack("!I", ifIndex))
                    except Exception:
                        pass
                    self._send_sock.bind((self.server_ip, 0))
                    self._sender_ready = True
                    logger.info(f"[DHCP] ✅ Sender socket bound to {self.server_ip}:0 (dynamic port, interface-forced)")
                except Exception as e2:
                    logger.error(f"[DHCP] ⚠ Failed to bind send socket to {self.server_ip}:0 ({e2}). Using recv socket for replies — DEGRADED MODE.")
                    self._send_sock = self._sock
                    self._sender_degraded = True
                    # Degraded: reply di qua recv socket (bind 0.0.0.0) → co the bi TUN bat nham

            self._running = True
            self._thread = threading.Thread(target=self._listen_loop, daemon=True)
            self._thread.start()
            self._lease_worker_thread = threading.Thread(
                target=self._lease_worker_loop, daemon=True, name="dhcp-lease-worker"
            )
            self._lease_worker_thread.start()

            # Summary log
            _mode = "DEGRADED" if self._sender_degraded else ("READY" if self._sender_ready else "PARTIAL")
            logger.info(
                f"[DHCP] Server started [{_mode}] | "
                f"Recv: 0.0.0.0:67 | Send: {self.server_ip}:67 | "
                f"Pool: {self.pool_start}-{self.pool_end} | "
                f"Gateway: {self.server_ip} | DNS: {self.dns_server}"
            )
            if self._sender_degraded:
                logger.warning(
                    "[DHCP] ⚠ DEGRADED MODE: Sender socket = Receiver socket (bind 0.0.0.0). "
                    "DHCP replies may be intercepted by TUN adapter when sing-box is running. "
                    "Cause: LAN interface IP not ready or port conflict."
                )
            return True
        except PermissionError:
            self._startup_error = "Permission denied binding port 67. Run as Administrator!"
            logger.error(f"[DHCP] {self._startup_error}")
            return False
        except OSError as e:
            if "address already in use" in str(e).lower() or e.errno == 10048:
                self._startup_error = f"Port 67 already in use — another DHCP server may be running."
                logger.warning(f"[DHCP] {self._startup_error}")
            else:
                self._startup_error = f"Socket error: {e}"
                logger.error(f"[DHCP] {self._startup_error}")
            return False
        except Exception as e:
            self._startup_error = f"Failed to start: {e}"
            logger.error(f"[DHCP] {self._startup_error}")
            return False

    def stop(self):
        """Stop DHCP server."""
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._send_sock and self._send_sock is not self._sock:
            try:
                self._send_sock.close()
            except Exception:
                pass
            self._send_sock = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        if self._lease_worker_thread:
            self._lease_worker_thread.join(timeout=5)
            self._lease_worker_thread = None
        logger.info("[DHCP] Server stopped.")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_fully_ready(self) -> bool:
        """True khi DHCP server dang chay VA sender socket bind dung LAN IP.
        False khi chua start, start fail, hoac dang o degraded mode."""
        return self._running and self._sender_ready and not self._sender_degraded

    def readiness_check(self) -> dict:
        """Kiem tra trang thai san sang chi tiet cua DHCP server.

        Returns dict voi:
          - ready: bool — True neu DHCP co the phuc vu client binh thuong
          - running: bool — Server thread dang chay
          - sender_ready: bool — Sender socket bind dung LAN IP
          - sender_degraded: bool — Sender = Receiver (co the bi TUN bat)
          - startup_error: str — Loi khi start (neu co)
          - stats: dict — So lieu OFFER/ACK/NAK
          - detail: str — Mo ta trang thai hien tai
        """
        import time as _time
        stats = {
            "total_offers": self._total_offers,
            "total_acks": self._total_acks,
            "total_naks": self._total_naks,
            "last_offer_ago": round(_time.time() - self._last_offer_time, 1) if self._last_offer_time else None,
            "last_ack_ago": round(_time.time() - self._last_ack_time, 1) if self._last_ack_time else None,
        }

        if not self._running:
            return {
                "ready": False, "running": False,
                "sender_ready": False, "sender_degraded": False,
                "startup_error": self._startup_error or "Not started",
                "stats": stats,
                "detail": f"DHCP server not running. Error: {self._startup_error}" if self._startup_error else "DHCP server not started.",
            }

        if self._sender_degraded:
            detail = (
                f"DEGRADED: Sender socket = Receiver (bind 0.0.0.0). "
                f"DHCP replies may be intercepted by TUN. "
                f"LAN IP '{self.server_ip}' on '{self.interface_name}' may not be assigned yet."
            )
        elif self._sender_ready:
            detail = f"READY: Sender bound to {self.server_ip}:67, interface '{self.interface_name}'."
        else:
            detail = "PARTIAL: Server running but sender socket status unknown."

        return {
            "ready": self._sender_ready and not self._sender_degraded,
            "running": True,
            "sender_ready": self._sender_ready,
            "sender_degraded": self._sender_degraded,
            "startup_error": self._startup_error,
            "stats": stats,
            "detail": detail,
        }

    def _listen_loop(self):
        """Main listener loop with periodic LAN health checks."""
        last_health_check = 0
        HEALTH_CHECK_INTERVAL = 30  # seconds

        while self._running:
            try:
                data, addr = self._sock.recvfrom(2048)
                if len(data) < 240:
                    continue
                self._handle_packet(data, addr)
            except socket.timeout:
                # Periodic health check: if sender socket is dead, try to rebind
                import time as _time
                now = _time.time()
                if now - last_health_check > HEALTH_CHECK_INTERVAL:
                    last_health_check = now
                    self._check_and_rebind_sender()
                continue
            except OSError:
                if self._running:
                    logger.error("[DHCP] Socket closed unexpectedly.")
                break
            except Exception as e:
                if self._running:
                    logger.error(f"[DHCP] Error in listen loop: {e}")

    def _lease_worker_loop(self):
        """Xử lý on_lease callback (đồng bộ routing/proxy, có thể trigger full-restart
        sing-box mất vài giây) trong 1 thread nền RIÊNG BIỆT với _listen_loop().

        Lý do: on_lease được gọi SAU KHI đã gửi ACK (thiết bị đã có IP), nhưng nếu gọi
        trực tiếp/đồng bộ ngay trong _listen_loop() (thread duy nhất nhận gói UDP), một
        lần callback chậm (vd sing-box đang full-restart, hoặc Clash API vừa restart nên
        chưa kịp sẵn sàng → request timeout 2s → lại trigger thêm 1 lần full-restart nữa)
        sẽ CHẶN LUÔN việc nhận gói DHCP của MỌI thiết bị khác cho tới khi callback đó xong
        — đã xác nhận qua log thực tế: hàng loạt "API update failed. Falling back to full
        restart" liên tiếp mỗi ~9s, mỗi lần xử lý đúng 1 thiết bị, trong lúc đó KHÔNG thiết
        bị mới nào xin được IP vì _listen_loop() bị kẹt trong _handle_request() cũ. Đưa
        callback ra 1 thread riêng, xử lý tuần tự qua Queue (an toàn với generate_config()/
        state của SingBoxManager — không xử lý song song), để _listen_loop() luôn rảnh
        quay lại recvfrom() ngay sau khi ACK được gửi.
        """
        while self._running:
            try:
                item = self._lease_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                continue
            mac_str, req_ip, hostname, ip_changed = item
            try:
                self.on_lease(mac_str, req_ip, hostname, ip_changed)
            except Exception as e:
                logger.error(f"[DHCP] Lease callback error: {e}")

    def _check_and_rebind_sender(self):
        """Check if LAN interface is up and rebind sender socket if needed."""
        import platform
        if platform.system() != "Windows":
            return

        try:
            from app.network_setup import check_lan_interface_health
            health = check_lan_interface_health(self.interface_name)

            if health["status"] == "healthy" and health.get("ip_address"):
                current_ip = health["ip_address"]
                # If sender socket is the same as recv socket (fallback mode),
                # try to create a proper sender socket bound to LAN IP
                if self._send_sock is self._sock:
                    logger.info(
                        f"[DHCP] LAN interface '{self.interface_name}' is back up "
                        f"(IP: {current_ip}). Attempting to rebind sender socket..."
                    )
                    try:
                        new_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        new_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        new_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                        try:
                            new_sock.setsockopt(
                                socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                socket.inet_aton(current_ip)
                            )
                            ifIndex = self._get_interface_index()
                            if ifIndex:
                                import struct
                                new_sock.setsockopt(
                                    socket.IPPROTO_IP, 31,
                                    struct.pack("!I", ifIndex)
                                )
                        except Exception:
                            pass
                        new_sock.bind((current_ip, 0))
                        self._send_sock = new_sock
                        self.server_ip = current_ip
                        logger.info(
                            f"[DHCP] ✓ Successfully rebound sender socket to "
                            f"{current_ip} on '{self.interface_name}'"
                        )
                    except Exception as e:
                        logger.debug(f"[DHCP] Rebind attempt failed: {e}")
        except Exception as e:
            logger.debug(f"[DHCP] Health check error (non-critical): {e}")

    def _handle_packet(self, data: bytes, addr: tuple):
        """Parse and handle a DHCP packet."""
        try:
            # ── Parse DHCP header ──
            op = data[0]         # 1 = BOOTREQUEST (from client)
            if op != 1:
                return

            htype = data[1]      # Hardware type (1 = Ethernet)
            hlen = data[2]       # Hardware address length (6 for MAC)
            xid = data[4:8]      # Transaction ID
            flags = struct.unpack("!H", data[10:12])[0]
            ciaddr = data[12:16]  # Client IP (if renewing)
            giaddr = data[24:28]  # Gateway IP (relay agent)
            chaddr = data[28:44]  # Client hardware address (MAC)

            mac_str = _mac_bytes_to_str(chaddr[:hlen])

            # ── Parse options ──
            # Find magic cookie
            magic_idx = data.find(DHCP_MAGIC)
            if magic_idx < 0:
                return
            options = _parse_options(data[magic_idx + 4:])

            # Get message type
            msg_type_data = options.get(OPT_MSG_TYPE)
            if not msg_type_data:
                return
            msg_type = msg_type_data[0]

            # Get hostname if present
            hostname = ""
            if OPT_HOSTNAME in options:
                try:
                    hostname = options[OPT_HOSTNAME].decode("utf-8", errors="ignore")
                except Exception:
                    pass

            # Ignore requests from host's own MAC or Windows RAS virtual adapters
            # NOTE: hostname check was REMOVED — it caused false positives when
            # different PCs share the same Windows hostname (e.g. WINDOWS-XXXXXXX).
            # MAC-based check is globally unique and sufficient.
            
            # Check if it is a RAS adapter belonging to this host
            is_my_ras = False
            if mac_str.startswith("52:41:53:20"):
                clean_mac = mac_str.replace(":", "")
                for host_mac in self.host_macs:
                    clean_host = host_mac.replace(":", "")
                    if clean_host in clean_mac:
                        is_my_ras = True
                        break
            
            if mac_str in self.host_macs or is_my_ras:
                reasons = []
                if mac_str in self.host_macs: reasons.append("host's own MAC")
                if is_my_ras: reasons.append("host's own RAS virtual interface")
                reason = " & ".join(reasons)
                logger.info(f"[DHCP] Ignoring DHCP request from host (detected via {reason}: {mac_str} / name: {hostname})")
                return

            # Get requested IP
            requested_ip = None
            if OPT_REQUESTED_IP in options:
                requested_ip = _bytes_to_ip(options[OPT_REQUESTED_IP])

            # Get server identifier (Option 54)
            server_id = None
            if OPT_SERVER_ID in options:
                try:
                    server_id = _bytes_to_ip(options[OPT_SERVER_ID])
                except Exception:
                    pass

            # Log Parameter Request List (Option 55) for debugging — helps identify
            # which options a client expects and may reject the OFFER without
            param_req = options.get(OPT_PARAM_REQ)
            _param_list_str = ""
            if param_req:
                _param_list_str = ",".join(str(b) for b in param_req)

            if msg_type == DHCP_DISCOVER:
                logger.info(
                    f"[DHCP] DISCOVER from {mac_str} (hostname='{hostname}', "
                    f"hlen={hlen}, flags=0x{flags:04X}, "
                    f"param_req=[{_param_list_str}])"
                )
                self._handle_discover(xid, mac_str, chaddr[:hlen], flags, hostname)
            elif msg_type == DHCP_REQUEST:
                self._handle_request(
                    xid, mac_str, chaddr[:hlen], flags,
                    requested_ip, ciaddr, hostname, server_id
                )
            elif msg_type == DHCP_RELEASE:
                logger.info(f"[DHCP] RELEASE from {mac_str}")
            elif msg_type == DHCP_INFORM:
                logger.debug(f"[DHCP] INFORM from {mac_str}")

        except Exception as e:
            logger.error(f"[DHCP] Error handling packet: {e}")

    def _allocate_ip(self, mac: str) -> Optional[str]:
        """Allocate IP for MAC using MAC Registry (sticky IP support)."""
        if not self.mac_registry:
            return None

        # 1. Check if MAC already has an IP
        existing_ip = self.mac_registry.get_ip_for_mac(mac)
        if existing_ip:
            # Verify IP is still in our pool range
            try:
                ip_obj = ipaddress.IPv4Address(existing_ip)
                start_obj = ipaddress.IPv4Address(self.pool_start)
                end_obj = ipaddress.IPv4Address(self.pool_end)
                if start_obj <= ip_obj <= end_obj:
                    return existing_ip
            except Exception:
                pass

        # 2. Allocate new IP from pool (reclaims from long-offline devices if pool is full)
        return self.mac_registry.allocate_new_ip(mac, self.pool_start, self.pool_end, self.lease_time)

    def _handle_discover(self, xid, mac_str, mac_bytes, flags, hostname):
        """Handle DHCP Discover → Send Offer."""
        offered_ip = self._allocate_ip(mac_str)
        if not offered_ip:
            logger.warning(f"[DHCP] Cannot allocate IP for {mac_str} (pool full?)")
            return

        is_known = self.mac_registry and self.mac_registry.get_ip_for_mac(mac_str) is not None
        logger.info(
            f"[DHCP] → OFFER {offered_ip} to {mac_str} "
            f"({'known' if is_known else 'NEW device'})"
        )

        self._send_response(
            msg_type=DHCP_OFFER,
            xid=xid,
            yiaddr=offered_ip,
            mac_bytes=mac_bytes,
            flags=flags,
        )

    def _handle_request(self, xid, mac_str, mac_bytes, flags,
                        requested_ip, ciaddr, hostname, server_id=None):
        """Handle DHCP Request → Send ACK or NAK."""
        # If server_id is provided and does not match our IP, ignore the request
        if server_id and server_id != self.server_ip:
            logger.info(
                f"[DHCP] REQUEST from {mac_str} is for server {server_id} "
                f"(not us {self.server_ip}) → ignoring"
            )
            return

        # Determine which IP client is requesting
        req_ip = requested_ip
        if not req_ip:
            ciaddr_str = _bytes_to_ip(ciaddr)
            if ciaddr_str != "0.0.0.0":
                req_ip = ciaddr_str

        # Validate requested IP is within our pool range
        if req_ip:
            try:
                ip_obj = ipaddress.IPv4Address(req_ip)
                start_obj = ipaddress.IPv4Address(self.pool_start)
                end_obj = ipaddress.IPv4Address(self.pool_end)
                if not (start_obj <= ip_obj <= end_obj):
                    logger.info(
                        f"[DHCP] REQUEST from {mac_str}: requested {req_ip} "
                        f"is outside pool ({self.pool_start}-{self.pool_end}) → NAK"
                    )
                    self._send_response(
                        msg_type=DHCP_NAK, xid=xid,
                        yiaddr="0.0.0.0", mac_bytes=mac_bytes, flags=flags,
                    )
                    return
            except Exception:
                logger.warning(f"[DHCP] REQUEST from {mac_str}: invalid requested IP '{req_ip}' → NAK")
                self._send_response(
                    msg_type=DHCP_NAK, xid=xid,
                    yiaddr="0.0.0.0", mac_bytes=mac_bytes, flags=flags,
                )
                return

        if not req_ip:
            # Allocate from pool (sticky or new)
            req_ip = self._allocate_ip(mac_str)

        if not req_ip:
            logger.warning(f"[DHCP] REQUEST from {mac_str}: no IP to assign → NAK")
            self._send_response(
                msg_type=DHCP_NAK, xid=xid,
                yiaddr="0.0.0.0", mac_bytes=mac_bytes, flags=flags,
            )
            return

        # Verify IP is available for this MAC
        if self.mac_registry:
            owner_mac = self.mac_registry._ip_to_mac.get(req_ip)
            if owner_mac and owner_mac != mac_str:
                logger.warning(
                    f"[DHCP] REQUEST from {mac_str}: requested {req_ip} "
                    f"is owned by {owner_mac} → NAK"
                )
                self._send_response(
                    msg_type=DHCP_NAK, xid=xid,
                    yiaddr="0.0.0.0", mac_bytes=mac_bytes, flags=flags,
                )
                return

        logger.info(f"[DHCP] REQUEST from {mac_str} for {req_ip} → ACK")

        # Ghi nhận IP này có phải MỚI đối với mac hay không (trước khi update_lease() ghi
        # đè) — dùng để callback quyết định có cần đồng bộ lại routing hay không (renew
        # bình thường, IP không đổi, thì routing chắc chắn vẫn đúng từ trước, không cần
        # đụng vào sing-box mỗi lần renew).
        ip_changed = True
        if self.mac_registry:
            ip_changed = (self.mac_registry.get_ip_for_mac(mac_str) != req_ip)
            self.mac_registry.update_lease(mac_str, req_ip, hostname)

        # Send ACK
        self._send_response(
            msg_type=DHCP_ACK,
            xid=xid,
            yiaddr=req_ip,
            mac_bytes=mac_bytes,
            flags=flags,
        )

        # Trigger callback (for routing update) qua hàng đợi nền — xem _lease_worker_loop()
        # để biết lý do KHÔNG gọi trực tiếp/đồng bộ ở đây.
        if self.on_lease:
            self._lease_queue.put((mac_str, req_ip, hostname, ip_changed))

    def _add_static_arp(self, ip: str, mac_bytes: bytes) -> bool:
        import platform
        if platform.system() != "Windows":
            return True
        import subprocess
        mac_str = "-".join(f"{b:02x}" for b in mac_bytes)
        
        # Method 1: Try modern PowerShell New-NetNeighbor
        try:
            cmd = [
                "powershell", "-NoProfile", "-Command",
                f"New-NetNeighbor -InterfaceAlias '{self.interface_name}' -IPAddress '{ip}' -LinkLayerAddress '{mac_str}' -State Permanent -Confirm:$false -ErrorAction Stop"
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if res.returncode == 0:
                logger.info(f"[DHCP] Added static ARP via New-NetNeighbor: {ip} -> {mac_str} on interface {self.interface_name}")
                return True
        except Exception as e_netneighbor:
            logger.debug(f"[DHCP] New-NetNeighbor failed: {e_netneighbor}")

        # Method 2: Fallback to legacy arp -s
        try:
            res = subprocess.run(
                ["arp", "-s", ip, mac_str, self.server_ip],
                capture_output=True, text=True, timeout=2
            )
            output = (res.stdout + res.stderr).lower()
            if res.returncode == 0 and "failed" not in output and "denied" not in output and "requires elevation" not in output:
                logger.info(f"[DHCP] Added static ARP via legacy arp -s: {ip} -> {mac_str} on interface {self.server_ip}")
                return True
            else:
                err_msg = res.stderr.strip() or res.stdout.strip()
                logger.warning(f"[DHCP] Failed to add static ARP via legacy arp: {err_msg}")
                return False
        except Exception as e:
            logger.error(f"[DHCP] Failed to add static ARP for {ip} on interface {self.server_ip}: {e}")
            return False

    def _remove_static_arp(self, ip: str):
        import platform
        if platform.system() != "Windows":
            return
        import subprocess
        
        # Method 1: Remove via Remove-NetNeighbor
        try:
            cmd = [
                "powershell", "-NoProfile", "-Command",
                f"Remove-NetNeighbor -InterfaceAlias '{self.interface_name}' -IPAddress '{ip}' -Confirm:$false -ErrorAction Stop"
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if res.returncode == 0:
                logger.debug(f"[DHCP] Removed static ARP via Remove-NetNeighbor for {ip} on interface {self.interface_name}")
                return
        except Exception as e_netneighbor:
            logger.debug(f"[DHCP] Remove-NetNeighbor failed: {e_netneighbor}")

        # Method 2: Fallback to legacy arp -d
        try:
            res = subprocess.run(
                ["arp", "-d", ip, self.server_ip],
                capture_output=True, text=True, timeout=2
            )
            if res.returncode == 0:
                logger.debug(f"[DHCP] Removed static ARP via legacy arp -d for {ip} on interface {self.server_ip}")
            else:
                logger.warning(f"[DHCP] Failed to remove static ARP via legacy arp: {res.stderr.strip()}")
        except Exception as e:
            logger.error(f"[DHCP] Failed to remove static ARP for {ip} on interface {self.server_ip}: {e}")

    def _send_response(self, msg_type: int, xid: bytes, yiaddr: str,
                       mac_bytes: bytes, flags: int):
        """Build and send a DHCP response packet."""
        try:
            # ── DHCP Header ──
            packet = bytearray(236)
            packet[0] = 2                                    # op: BOOTREPLY
            packet[1] = 1                                    # htype: Ethernet
            packet[2] = 6                                    # hlen: MAC length
            packet[3] = 0                                    # hops
            packet[4:8] = xid                                # Transaction ID
            struct.pack_into("!H", packet, 8, 0)            # secs
            
            # Pack broadcast flag only if client requested it or if we are broadcasting
            if (flags & 0x8000) or yiaddr == "0.0.0.0":
                forced_flags = flags | 0x8000
            else:
                forced_flags = flags
            struct.pack_into("!H", packet, 10, forced_flags) # flags (broadcast flag)
            
            packet[16:20] = _ip_to_bytes(yiaddr)            # yiaddr (your IP)
            packet[20:24] = _ip_to_bytes(self.server_ip)    # siaddr (server IP)
            packet[28:28 + len(mac_bytes)] = mac_bytes       # chaddr

            # ── Magic Cookie + Options ──
            options = bytearray()
            options += DHCP_MAGIC

            # Option 53: Message Type
            options += bytes([OPT_MSG_TYPE, 1, msg_type])

            # Option 54: Server Identifier
            options += bytes([OPT_SERVER_ID, 4])
            options += _ip_to_bytes(self.server_ip)

            if msg_type in (DHCP_OFFER, DHCP_ACK):
                # Option 51: Lease Time
                options += bytes([OPT_LEASE_TIME, 4])
                options += struct.pack("!I", self.lease_time)

                # Option 58: T1 Renewal Time (REQUIRED by many Windows DHCP clients)
                # If missing, some Windows builds silently ignore the OFFER
                t1_renewal = self.lease_time // 2
                options += bytes([58, 4])
                options += struct.pack("!I", t1_renewal)

                # Option 59: T2 Rebinding Time (REQUIRED by many Windows DHCP clients)
                t2_rebinding = (self.lease_time * 7) // 8
                options += bytes([59, 4])
                options += struct.pack("!I", t2_rebinding)

                # Option 1: Subnet Mask
                options += bytes([OPT_SUBNET_MASK, 4])
                options += _ip_to_bytes(self.subnet_mask)

                # Option 3: Router (Gateway)
                options += bytes([OPT_ROUTER, 4])
                options += _ip_to_bytes(self.server_ip)

                # Option 6: DNS Server
                options += bytes([OPT_DNS, 4])
                options += _ip_to_bytes(self.dns_server)

                # Option 15: Domain Name (Windows clients commonly request this)
                domain_name = b"genrouter.local"
                options += bytes([15, len(domain_name)])
                options += domain_name

                # Option 28: Broadcast Address
                try:
                    ip_obj = ipaddress.ip_interface(f"{self.server_ip}/{self.subnet_mask}")
                    subnet_broadcast = str(ip_obj.network.broadcast_address)
                    options += bytes([28, 4])
                    options += _ip_to_bytes(subnet_broadcast)
                except Exception:
                    pass

            # End option
            options += bytes([OPT_END])

            # Pad to minimum 300 bytes (required by some clients)
            full_packet = bytes(packet) + bytes(options)
            if len(full_packet) < 300:
                full_packet += b'\x00' * (300 - len(full_packet))

            # ── Send ──
            # Calculate subnet broadcast address (e.g. 192.168.10.255)
            subnet_broadcast = "255.255.255.255"
            try:
                ip_obj = ipaddress.ip_interface(f"{self.server_ip}/{self.subnet_mask}")
                subnet_broadcast = str(ip_obj.network.broadcast_address)
            except Exception:
                pass

            msg_names = {DHCP_OFFER: "OFFER", DHCP_ACK: "ACK", DHCP_NAK: "NAK"}
            msg_name = msg_names.get(msg_type, str(msg_type))

            sock = self._send_sock if self._send_sock else self._sock
            sent = False

            # Force broadcast path on Windows to avoid slow static ARP operations and CPU spikes.
            # We bypass unicast response entirely to prevent blocking the socket listener loop.
            try:
                sock.sendto(full_packet, ("255.255.255.255", 68))
                logger.info(f"[DHCP] Sent broadcast {msg_name} response to 255.255.255.255:68")
                sent = True
            except OSError as e:
                logger.error(f"[DHCP] Failed sending to global broadcast: {e}")

            try:
                sock.sendto(full_packet, (subnet_broadcast, 68))
                logger.info(f"[DHCP] Sent broadcast {msg_name} response to subnet broadcast {subnet_broadcast}:68")
                sent = True
            except OSError as e:
                logger.error(f"[DHCP] Failed sending to subnet broadcast {subnet_broadcast}:68: {e}")

            if not sent:
                logger.error("[DHCP] Failed to send DHCP response via any method.")

            # ── Stats tracking ──
            import time as _time
            if sent:
                if msg_type == DHCP_OFFER:
                    self._total_offers += 1
                    self._last_offer_time = _time.time()
                elif msg_type == DHCP_ACK:
                    self._total_acks += 1
                    self._last_ack_time = _time.time()
                elif msg_type == DHCP_NAK:
                    self._total_naks += 1

        except Exception as e:
            logger.error(f"[DHCP] Error sending response: {e}")

    def update_config(self, server_ip: str, subnet_mask: str, dns_server: str,
                      pool_start: str, pool_end: str, lease_time: int, interface_name: str = ""):
        """Update DHCP config without restart."""
        old_ip = self.server_ip
        old_interface = self.interface_name
        self.server_ip = server_ip
        self.subnet_mask = subnet_mask
        self.dns_server = dns_server
        self.pool_start = pool_start
        self.pool_end = pool_end
        self.lease_time = lease_time
        if interface_name:
            self.interface_name = interface_name
        logger.info(f"[DHCP] Config updated: pool={pool_start}-{pool_end}")

        if (old_ip != server_ip or old_interface != self.interface_name) and self._running:
            logger.info(f"[DHCP] LAN interface/IP changed. Re-binding sender socket.")
            if self._send_sock and self._send_sock is not self._sock:
                try:
                    self._send_sock.close()
                except Exception:
                    pass
            try:
                self._send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                
                try:
                    self._send_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.server_ip))
                    ifIndex = self._get_interface_index()
                    if ifIndex:
                        self._send_sock.setsockopt(socket.IPPROTO_IP, 31, struct.pack("!I", ifIndex))
                except Exception:
                    pass
                
                self._send_sock.bind((self.server_ip, 67))
            except Exception as e:
                logger.warning(
                    f"[DHCP] Failed to re-bind sender socket to {server_ip}:67 ({e}). "
                    f"Trying dynamic port..."
                )
                try:
                    self._send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    self._send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    self._send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    try:
                        self._send_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.server_ip))
                        ifIndex = self._get_interface_index()
                        if ifIndex:
                            self._send_sock.setsockopt(socket.IPPROTO_IP, 31, struct.pack("!I", ifIndex))
                    except Exception:
                        pass
                    self._send_sock.bind((self.server_ip, 0))
                except Exception as e2:
                    logger.error(f"[DHCP] Failed to bind dynamic sender socket on {server_ip} ({e2}). Using recv socket.")
                    self._send_sock = self._sock
