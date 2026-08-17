import subprocess
import psutil
import os
import shutil
import time

targets = {'c69-router.exe', 'sing-box.exe', 'mihomo.exe', 'c69update.exe'}
for p in psutil.process_iter(['pid', 'name']):
    try:
        n = (p.info['name'] or '').lower()
        if n in targets:
            p.kill()
            print("Killed:", n, p.info['pid'])
    except Exception:
        pass

for t in targets:
    subprocess.run(['taskkill', '/F', '/IM', t], capture_output=True)

time.sleep(1.0)

dist_dir = r'D:\Workspace\Python\c69-router\dist'
if os.path.exists(dist_dir):
    for item in os.listdir(dist_dir):
        item_path = os.path.join(dist_dir, item)
        try:
            if os.path.isdir(item_path):
                shutil.rmtree(item_path, ignore_errors=True)
            else:
                os.remove(item_path)
            print("Deleted:", item)
        except Exception as e:
            print("Could not delete", item, ":", e)

print("=== DIST FOLDER STATUS ===")
print("Remaining files in dist:", os.listdir(dist_dir) if os.path.exists(dist_dir) else "dist deleted")
