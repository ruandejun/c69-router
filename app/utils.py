"""
PhoneFarm GenRouter v2.0 — Utility Functions
"""

import time
import urllib.request
import concurrent.futures
import socket
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def parse_raw_proxy_string(raw: str) -> dict:
    """Parse proxy string in various formats:
    - socks5://user:pass@host:port
    - http://user:pass@host:port
    - host:port:user:pass
    - host:port
    - user:pass@host:port
    
    Returns: {type, host, port, username, password}
    """
    raw = raw.strip()
    proxy_type = "socks5"

    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        if scheme.lower() in ["http", "https"]:
            proxy_type = "http"
        elif scheme.lower() in ["socks", "socks5"]:
            proxy_type = "socks5"
        raw = rest

    username = ""
    password = ""
    host = ""
    port = 1080

    if "@" in raw:
        auth_part, host_part = raw.split("@", 1)
        if ":" in auth_part:
            username, password = auth_part.split(":", 1)
        else:
            username = auth_part

        if ":" in host_part:
            host, port_str = host_part.split(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                pass
        else:
            host = host_part
    else:
        parts = raw.split(":")
        if len(parts) >= 2:
            host = parts[0]
            try:
                port = int(parts[1])
            except ValueError:
                pass

            if len(parts) >= 4:
                username = parts[2]
                password = parts[3]
            elif len(parts) == 3:
                username = parts[2]
        else:
            host = raw

    return {
        "type": proxy_type,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
    }


def test_single_proxy(proxy_info: dict, attempts: int = 2) -> tuple:
    """Test a single proxy connection, retrying before declaring it dead.

    1 lần thử duy nhất (timeout 8s, không retry) dễ bị false negative khi có
    hiện tượng mạng thoáng qua (transient network hiccup) — proxy vẫn sống
    nhưng bị gắn nhầm "Die", khiến router âm thầm bỏ qua route qua proxy đó
    và rơi về direct-out (dùng IP gốc) mà không có cảnh báo rõ ràng.

    Args:
        proxy_info: dict with {id, type, host, port, username, password}
        attempts: số lần thử trước khi kết luận "Die"

    Returns: (proxy_id, status, latency_ms)
    """
    result = None
    for attempt in range(attempts):
        result = _test_single_proxy_once(proxy_info)
        if result[1] == "Live":
            return result
        if attempt < attempts - 1:
            time.sleep(1)
    return result


def _test_single_proxy_once(proxy_info: dict) -> tuple:
    """Single attempt of the proxy connectivity test. See test_single_proxy()."""
    proxy_id = proxy_info.get("id", "unknown")
    start = time.time()

    if proxy_info.get("type") in ("socks5", "socks"):
        try:
            import socks as pysocks
            s = pysocks.socksocket()
            s.set_proxy(
                pysocks.SOCKS5,
                proxy_info["host"],
                proxy_info["port"],
                username=proxy_info.get("username") or None,
                password=proxy_info.get("password") or None,
            )
            s.settimeout(8)
            # gstatic's generate_204 (dùng cho captive-portal check của Android/ChromeOS) thay
            # cho httpbin.org: httpbin.org là dịch vụ test công cộng bên thứ 3, hay bị 503/timeout
            # (đã xác nhận thực tế) khiến proxy còn sống bị gắn nhầm "Die". gstatic ổn định hơn
            # nhiều bậc và trả về ngay lập tức, không có payload.
            s.connect(("www.gstatic.com", 80))
            request_str = (
                "GET /generate_204 HTTP/1.1\r\n"
                "Host: www.gstatic.com\r\n"
                "Connection: close\r\n\r\n"
            )
            s.sendall(request_str.encode())

            response = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                response += chunk
            s.close()

            resp_text = response.decode("utf-8", errors="ignore")
            if "204" in resp_text.split("\r\n")[0]:
                elapsed = int((time.time() - start) * 1000)
                return proxy_id, "Live", elapsed
        except Exception:
            pass
    else:
        try:
            proxy_url = f"{proxy_info.get('type', 'http')}://"
            if proxy_info.get("username") and proxy_info.get("password"):
                proxy_url += f"{proxy_info['username']}:{proxy_info['password']}@"
            proxy_url += f"{proxy_info['host']}:{proxy_info['port']}"
            proxy_handler = urllib.request.ProxyHandler(
                {"http": proxy_url, "https": proxy_url}
            )
            opener = urllib.request.build_opener(proxy_handler)
            with opener.open("http://www.gstatic.com/generate_204", timeout=8) as r:
                if r.status == 204:
                    elapsed = int((time.time() - start) * 1000)
                    return proxy_id, "Live", elapsed
        except Exception:
            pass

    return proxy_id, "Die", -1


def bulk_test_proxies(proxies: list, max_workers: int = 10) -> list:
    """Test multiple proxies in parallel.
    
    Args:
        proxies: list of dicts with {id, type, host, port, username, password}
    
    Returns: list of (proxy_id, status, latency_ms)
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(test_single_proxy, proxies))
    return results


# ─── DNSBL Blacklist Checking ────────────────────────────────────────────────

_DNSBL_LISTS = [
    ("zen.spamhaus.org", "Spamhaus ZEN"),
    ("bl.spamcop.net", "SpamCop"),
    ("dnsbl.sorbs.net", "SORBS"),
    ("xbl.spamhaus.org", "Spamhaus XBL"),
    ("cbl.abuseat.org", "Abuseat CBL"),
]


def check_ip_blacklist(ip: str, timeout: float = 3.0) -> dict:
    """Check if an IP is on common DNSBL blacklists.

    Uses DNS reverse-lookup format: reversed(ip).dnsbl-domain
    A successful resolution means the IP is listed.

    Args:
        ip: IPv4 address to check.
        timeout: Per-lookup timeout in seconds.

    Returns:
        {ip, blacklisted, lists, checked_at}
    """
    parts = ip.strip().split(".")
    if len(parts) != 4:
        return {"ip": ip, "blacklisted": False, "lists": [], "error": "Invalid IPv4", "checked_at": time.time()}

    reversed_ip = ".".join(reversed(parts))
    found_on = []

    def _check_one(entry):
        dnsbl, name = entry
        lookup = f"{reversed_ip}.{dnsbl}"
        try:
            old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(timeout)
            try:
                socket.gethostbyname(lookup)
                return name  # Resolved → IP is listed
            finally:
                socket.setdefaulttimeout(old_timeout)
        except socket.gaierror:
            return None  # NXDOMAIN = not listed

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(_DNSBL_LISTS)) as ex:
        results = list(ex.map(_check_one, _DNSBL_LISTS))

    found_on = [r for r in results if r]
    return {
        "ip": ip,
        "blacklisted": len(found_on) > 0,
        "lists": found_on,
        "checked_at": time.time(),
    }
