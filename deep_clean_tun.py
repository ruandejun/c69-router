import subprocess
import psutil
import time
import os

print("=== 1. KILL TOAN BO PROCESSES ===")
targets = {'c69-router.exe', 'sing-box.exe', 'mihomo.exe', 'c69update.exe'}
for p in psutil.process_iter(['pid', 'name']):
    try:
        n = (p.info['name'] or '').lower()
        if n in targets:
            p.kill()
            print(f"Killed: {n} ({p.info['pid']})")
    except Exception:
        pass

for t in targets:
    subprocess.run(['taskkill', '/F', '/IM', t], capture_output=True)

time.sleep(1.0)

print("\n=== 2. GO BO TOAN BO ADAPTER GENROUTERTUN / WINTUN ===")
ps_remove_adapters = """
# Tim tat ca adapter Wintun hoac GenRouterTUN
Get-PnpDevice -FriendlyName "*GenRouter*" -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "Dang go device:" $_.FriendlyName $_.InstanceId
    & pnputil /remove-device $_.InstanceId /force 2>&1 | Out-Null
}

Get-PnpDevice -FriendlyName "*Wintun*" -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "Dang go Wintun device:" $_.FriendlyName $_.InstanceId
    & pnputil /remove-device $_.InstanceId /force 2>&1 | Out-Null
}

Get-NetAdapter | Where-Object { $_.Name -like "*TUN*" -or $_.InterfaceDescription -like "*Wintun*" } | ForEach-Object {
    Write-Host "Dang disable/remove NetAdapter:" $_.Name
    Disable-NetAdapter -Name $_.Name -Confirm:$false -ErrorAction SilentlyContinue
}
"""
res = subprocess.run(["powershell", "-NoProfile", "-Command", ps_remove_adapters], capture_output=True, text=True)
print(res.stdout if res.stdout else "Khong con adapter Wintun nao hoat dong.")

print("\n=== 3. XOA TOAN BO ROUTE RAC CUA TUN TRONG WINDOWS ===")
# Xoa cac route thuoc 0.0.0.0/1, 128.0.0.0/1, 172.19.0.0, 198.18.0.0 neu con sot
stale_routes = ["0.0.0.0 mask 128.0.0.0", "128.0.0.0 mask 128.0.0.0", "172.19.0.0", "198.18.0.0"]
for r in stale_routes:
    subprocess.run(["route", "delete"] + r.split(), capture_output=True)

print("Da xoa sach cac route cu cua TUN.")

print("\n=== 4. FLUSH DNS & RESET DNS TREN CAC CARD LAN/WAN ===")
subprocess.run(["ipconfig", "/flushdns"], capture_output=True)

ps_reset_dns = """
# Reset DNS tren Ethernet 3, Ethernet 4, Ethernet ve tu dong / sach
@('Ethernet', 'Ethernet 3', 'Ethernet 4', 'Wi-Fi') | ForEach-Object {
    $name = $_
    if (Get-NetAdapter -Name $name -ErrorAction SilentlyContinue) {
        Set-DnsClientServerAddress -InterfaceAlias $name -ResetServerAddresses -ErrorAction SilentlyContinue
        Write-Host "Reset DNS on $name: OK"
    }
}
"""
res_dns = subprocess.run(["powershell", "-NoProfile", "-Command", ps_reset_dns], capture_output=True, text=True)
print(res_dns.stdout)

print("\n=== 5. KIEM TRA LAI TOAN BO TRANG THAI MANG ===")
ps_check = """
Get-NetAdapter | Format-Table Name, InterfaceDescription, Status, LinkSpeed -AutoSize
"""
res_check = subprocess.run(["powershell", "-NoProfile", "-Command", ps_check], capture_output=True, text=True)
print(res_check.stdout)

print("=== [HOAN TAT] DA XOA TRANG GENROUTERTUN VA TOAN BO CACHE/SETTING CU ===")
