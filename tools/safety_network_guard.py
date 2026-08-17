import subprocess
import time
import socket
import sys
import os

LOG_FILE = r"D:\Workspace\Python\c69-router\safety_guard.log"

def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass

TARGET_HOSTS = ["1.1.1.1", "8.8.8.8"]
TARGET_DOMAINS = ["google.com", "cloudflare.com"]

def check_internet() -> bool:
    dns_ok = False
    for domain in TARGET_DOMAINS:
        try:
            socket.gethostbyname(domain)
            dns_ok = True
            break
        except Exception:
            pass

    if not dns_ok:
        return False

    tcp_ok = False
    for host in TARGET_HOSTS:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.5)
            s.connect((host, 53))
            s.close()
            tcp_ok = True
            break
        except Exception:
            pass

    return tcp_ok

def emergency_kill_singbox():
    log("🚨 [SAFETY GUARD] PHÁT HIỆN MẤT MẠNG! Đang kill sing-box.exe...")
    subprocess.run(["taskkill", "/F", "/IM", "sing-box.exe"], capture_output=True)
    subprocess.run(["powershell", "-NoProfile", "-Command", "Get-NetAdapter -Name '*tun*' -ErrorAction SilentlyContinue | Disable-NetAdapter -Confirm:$false"], capture_output=True)
    time.sleep(1)
    if check_internet():
        log("✅ [SAFETY GUARD] Đã khôi phục mạng máy chủ thành công!")
    else:
        log("⚠️ [SAFETY GUARD] Mạng vẫn chưa thông, reset route...")
        subprocess.run(["netsh", "interface", "ipv4", "reset"], capture_output=True)

def main():
    log("🛡️ SAFETY NETWORK GUARD STARTED (Background Monitor Active)")
    fail_count = 0
    while True:
        try:
            ok = check_internet()
            if ok:
                fail_count = 0
            else:
                fail_count += 1
                log(f"⚠ Mất kết nối internet (lần {fail_count})...")
                if fail_count >= 2:
                    emergency_kill_singbox()
                    fail_count = 0
        except Exception as e:
            log(f"Exception in guard: {e}")
        time.sleep(1)

if __name__ == "__main__":
    main()
