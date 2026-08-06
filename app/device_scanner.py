"""
PhoneFarm GenRouter v2.0 — Device Scanner

ARP table scanning, parallel ping check, and device discovery.
"""

import subprocess
import re
import platform
import logging
import concurrent.futures
from typing import Set, Dict

logger = logging.getLogger(__name__)


def get_arp_ips() -> Set[str]:
    """Get all IPs currently in the ARP table."""
    ips = set()
    try:
        if platform.system() == "Windows":
            res = subprocess.run("arp -a", shell=True, capture_output=True, text=True)
            matches = re.findall(
                r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s+"
                r"([0-9a-fA-F\-]{17})\s+(dynamic|static)",
                res.stdout, re.IGNORECASE
            )
            for ip, mac, _ in matches:
                ips.add(ip)
        else:
            res = subprocess.run("ip neigh show", shell=True, capture_output=True, text=True)
            matches = re.findall(
                r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}).*lladdr.*"
                r"(REACHABLE|DELAY|STALE|PROBE)",
                res.stdout
            )
            for ip, state in matches:
                ips.add(ip)
    except Exception as e:
        logger.error(f"[Scanner] Error reading ARP table: {e}")
    return ips


def get_arp_mac_map(subnet_prefix: str = "") -> Dict[str, str]:
    """Get ARP table as {IP: MAC} dict, optionally filtered by subnet prefix."""
    result = {}
    try:
        if platform.system() == "Windows":
            res = subprocess.run("arp -a", shell=True, capture_output=True, text=True)
            matches = re.findall(
                r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s+"
                r"([0-9a-fA-F\-]{17})\s+(dynamic|static)",
                res.stdout, re.IGNORECASE
            )
            for ip, mac_raw, _ in matches:
                if subnet_prefix and not ip.startswith(subnet_prefix):
                    continue
                mac = mac_raw.replace("-", ":").upper()
                if mac != "FF:FF:FF:FF:FF:FF":
                    result[ip] = mac
        else:
            res = subprocess.run("ip neigh show", shell=True, capture_output=True, text=True)
            for line in res.stdout.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 5 and "lladdr" in parts:
                    ip = parts[0]
                    mac_idx = parts.index("lladdr") + 1
                    if mac_idx < len(parts):
                        mac = parts[mac_idx].upper()
                        if subnet_prefix and not ip.startswith(subnet_prefix):
                            continue
                        result[ip] = mac
    except Exception as e:
        logger.error(f"[Scanner] Error reading ARP with MAC: {e}")
    return result


def ping_check(ip: str, timeout_ms: int = 300) -> bool:
    """Ping a single IP. Returns True if reachable."""
    try:
        if platform.system() == "Windows":
            cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
        else:
            timeout_s = max(1, timeout_ms // 1000)
            cmd = ["ping", "-c", "1", "-W", str(timeout_s), ip]

        res = subprocess.run(
            cmd, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=1.0
        )
        return res.returncode == 0
    except Exception:
        return False


def batch_ping_check(ips: list, max_workers: int = 30) -> Dict[str, bool]:
    """Parallel ping check for multiple IPs."""
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(ping_check, ip): ip for ip in ips}
        for future in concurrent.futures.as_completed(futures):
            ip = futures[future]
            try:
                results[ip] = future.result()
            except Exception:
                results[ip] = False
    return results
