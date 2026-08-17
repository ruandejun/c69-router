"""
emergency_guard.py — Standalone Emergency Network Guard for c69-router
Chạy script này độc lập (Admin): python emergency_guard.py
Nó sẽ liên tục giám sát mạng của Host PC. Nếu mất mạng quá 4 giây khi đang test:
-> Tự động FORCE KILL sing-box.exe / mihomo.exe ngay lập tức để cứu mạng máy chủ!
"""

import time, subprocess, sys, socket, urllib.request

sys.stdout.reconfigure(encoding='utf-8')

print("=" * 60)
print("🛡️ EMERGENCY NETWORK GUARD — ĐANG GIÁM SÁT KẾT NỐI MÁY CHỦ...")
print("Nếu mất mạng khi bật TUN, script sẽ tự động KILL sing-box/mihomo ngay lập tức!")
print("Nhấn Ctrl+C để dừng giám sát.")
print("=" * 60)

def check_internet():
    # 1. Check socket connect to 1.1.1.1 / 8.8.8.8
    for host, port in [("1.1.1.1", 53), ("8.8.8.8", 53), ("192.168.1.1", 53)]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.5)
            s.connect((host, port))
            s.close()
            return True
        except Exception:
            try:
                s_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s_udp.settimeout(1.5)
                query = b'\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x06google\x03com\x00\x00\x01\x00\x01'
                s_udp.sendto(query, (host, 53))
                resp, _ = s_udp.recvfrom(512)
                if len(resp) > 0:
                    return True
            except Exception:
                pass
    try:
        urllib.request.urlopen("http://1.1.1.1", timeout=1.5)
        return True
    except Exception:
        pass
    return False

def emergency_kill():
    print("\n" + "!" * 60)
    print("🔴 PHÁT HIỆN MẤT MẠNG! ĐANG THỰC HIỆN EMERGENCY KILL...")
    print("!" * 60)
    # Kill sing-box and mihomo
    subprocess.run(["taskkill", "/F", "/IM", "sing-box.exe"], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "mihomo.exe"], capture_output=True)
    
    # Reset TUN DNS
    ps_cmd = (
        "Set-DnsClientServerAddress -InterfaceAlias 'GenRouterTUN' -ResetServerAddresses -ErrorAction SilentlyContinue; "
        "Set-NetIPInterface -InterfaceAlias 'GenRouterTUN' -InterfaceMetric 500 -ErrorAction SilentlyContinue"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True)
    print("✓ Đã kill sing-box & mihomo, đã reset DNS TUN. Mạng máy chủ sẽ phục hồi ngay!")

fail_count = 0
while True:
    try:
        ok = check_internet()
        if ok:
            if fail_count > 0:
                print("🟢 Mạng máy chủ: BÌNH THƯỜNG (OK)")
            fail_count = 0
        else:
            fail_count += 1
            print(f"⚠️ Cảnh báo: Không có kết nối internet (Lần {fail_count}/2)...")
            if fail_count >= 2:
                emergency_kill()
                fail_count = 0
                time.sleep(3)
        time.sleep(2)
    except KeyboardInterrupt:
        print("\nĐã dừng Emergency Guard.")
        break
    except Exception as e:
        time.sleep(2)
