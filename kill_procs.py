import subprocess
import psutil

targets = {'c69-router.exe', 'sing-box.exe', 'mihomo.exe', 'c69update.exe'}
killed = []
for p in psutil.process_iter(['pid', 'name']):
    try:
        n = (p.info['name'] or '').lower()
        if n in targets:
            p.kill()
            killed.append(f"{n} (PID {p.info['pid']})")
    except Exception:
        pass

for t in targets:
    subprocess.run(['taskkill', '/F', '/IM', t], capture_output=True)

print("=== DA KILL CAC TIEN TRINH ===")
if killed:
    for k in killed:
        print(" - Killed:", k)
else:
    print(" - Khong co tien trinh nao dang chay.")

remaining = []
for p in psutil.process_iter(['pid', 'name']):
    try:
        n = (p.info['name'] or '').lower()
        if n in targets:
            remaining.append(f"{n} (PID {p.info['pid']})")
    except Exception:
        pass

print("=== TRANG THAI HIEN TAI ===")
if remaining:
    print(" ! Con lai:", remaining)
else:
    print(" [OK] DA KILL SACH TOAN BO 100% (0 process running)")
