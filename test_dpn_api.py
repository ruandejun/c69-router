"""
Verification script for DPN function in c69-router by directly calling the route function.
This bypasses Starlette/FastAPI TestClient dependencies.
"""

import sys
import os
import json

# Ensure UTF-8 output on Windows console
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Mock FastAPI Request
class MockRequest:
    class Client:
        def __init__(self, host):
            self.host = host
    def __init__(self, host):
        self.client = self.Client(host)

# Import router dependencies
from app.config import load_config
from app.mac_registry import MACRegistry
from app.singbox_manager import SingBoxManager
from app.routes.proxies import fetch_and_assign_dpn

def test_dpn_direct_call():
    print("=== Khởi tạo cấu hình và registry ===")
    config = load_config()
    mac_registry = MACRegistry()
    
    # Khởi tạo SingBoxManager mock (chỉ để bypass dependency mà không chạy thật process)
    singbox_manager = SingBoxManager(config, mac_registry)
    
    # Thiết bị test ảo
    dummy_mac = "AA:BB:CC:DD:EE:FF"
    dummy_ip = "192.168.10.150"
    
    print(f"Đăng ký thiết bị ảo: MAC={dummy_mac} | IP={dummy_ip}")
    mac_registry.update_lease(dummy_mac, dummy_ip, "DPN_Test_Device")
    
    # Giả lập Request từ client
    req = MockRequest(host=dummy_ip)
    
    print(f"\n=== Gọi hàm fetch_and_assign_dpn (Quốc gia: US) ===")
    try:
        result = fetch_and_assign_dpn(
            request=req,
            country="US",
            mac=dummy_mac,
            check_clean=True,
            config=config,
            mac_registry=mac_registry,
            singbox_manager=singbox_manager
        )
        print("\n=== KẾT QUẢ THIẾT LẬP KẾT NỐI DPN US ===")
        print(json.dumps(result, indent=4, ensure_ascii=False))
        
    except Exception as e:
        print(f"\nLỗi khi thực thi hàm: {e}")

if __name__ == "__main__":
    test_dpn_flow = test_dpn_direct_call
    test_dpn_flow()
