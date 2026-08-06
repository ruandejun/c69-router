# Hướng dẫn Cấu hình Hệ thống Định tuyến Native trên Windows (Sing-Box)

Tài liệu này hướng dẫn chi tiết cách thiết lập phần cứng và cấu hình mạng trên Windows Host để chạy bộ định tuyến đổi IP hàng loạt cho Phone Farm sử dụng **Sing-Box** và **Wintun** trực tiếp trên Windows (không cần máy ảo Linux).

---

## 1. Yêu cầu Hệ thống & Phần cứng
* **Hệ điều hành**: Windows 10/11 Pro, Enterprise hoặc LTSC (nên chạy dưới quyền Administrator).
* **Card LAN 1 (WAN)**: Cắm dây mạng từ Modem nhà mạng để nhận Internet.
* **Card LAN 2 (LAN)**: Cắm dây mạng kết nối trực tiếp đến cổng mạng của Aruba 535.
* **Aruba 535 AP**: 
  - Thiết lập Aruba phát Wifi cho các điện thoại.
  - Cấu hình DHCP Server trực tiếp trên Aruba cấp phát dải IP nội bộ, ví dụ: `192.168.100.10` đến `192.168.100.250`.
  - **Quan trọng**: Đặt IP Default Gateway cấp xuống cho điện thoại là `192.168.100.1` (đây là IP tĩnh chúng ta sẽ gán cho Card LAN 2 của Windows).

---

## 2. Cấu hình IP tĩnh cho Card LAN 2 trên Windows
Để máy tính Windows đóng vai trò làm Gateway nhận lưu lượng từ Aruba:
1. Nhấn tổ hợp phím `Windows + R`, gõ `ncpa.cpl` và nhấn Enter để mở Network Connections.
2. Tìm đúng Card mạng LAN 2 (kết nối ra Aruba), click chuột phải chọn **Properties**.
3. Click đúp vào **Internet Protocol Version 4 (TCP/IPv4)**.
4. Cấu hình IP tĩnh như sau:
   - **IP address**: `192.168.100.1`
   - **Subnet mask**: `255.255.255.0`
   - **Default Gateway**: (Để trống)
   - **DNS Servers**: `8.8.8.8` và `1.1.1.1`
5. Nhấn **OK** để lưu lại.

---

## 3. Chạy ứng dụng dưới quyền Administrator
Do phần mềm cần khởi tạo card mạng ảo **Wintun TUN** và thực hiện định tuyến hệ thống, bạn cần khởi chạy uvicorn bằng quyền Administrator:
1. Mở PowerShell hoặc Command Prompt bằng quyền **Run as Administrator**.
2. Di chuyển đến thư mục dự án và khởi chạy:
   ```powershell
   python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
3. Khi khởi động lần đầu, phần mềm sẽ:
   - Tự động tải xuống `sing-box.exe` và `wintun.dll` từ internet về thư mục dự án.
   - Tự động kích hoạt tính năng **IP Enable Router** (chuyển tiếp gói tin) trong Registry của Windows.
   - **Lưu ý**: Bạn cần khởi động lại máy tính Windows **1 lần duy nhất** để Windows áp dụng tính năng chuyển tiếp gói tin (IP Forwarding) này.

---

## 4. Quản lý và Vận hành qua Dashboard
Sau khi chạy server, mở trình duyệt trên máy Windows truy cập vào:
👉 **[http://127.0.0.1:8000/](http://127.0.0.1:8000/)**

* **Quản lý Proxy**: Vào tab "Quản lý Proxy" chọn **Bulk Import** để dán danh sách proxy (định dạng `Host:Port` hoặc `Host:Port:User:Pass`). Sing-Box sẽ tự khởi tạo luồng đi riêng cho từng proxy.
* **Gán IP điện thoại**: Khi các điện thoại kết nối vào Wifi Aruba và nhận IP (ví dụ `192.168.100.20`), chúng sẽ xuất hiện ở tab "Thiết bị". Bạn chỉ cần chọn thiết bị và gán proxy tương ứng, Sing-Box sẽ tự cập nhật cấu hình và đổi IP cho thiết bị đó trong chưa đầy 0.1 giây mà không làm gián đoạn kết nối.
* **Bypass**: Cấu hình các IP/Domain trong Cài đặt để đi thẳng trực tiếp bằng mạng gốc (không qua proxy).
