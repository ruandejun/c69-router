# Hướng Dẫn Từng Bước Cấu Hình Aruba 535 Cho Phone Farm

Tài liệu này hướng dẫn chi tiết cách cấu hình cục phát Aruba 535 từ trạng thái mặc định (Factory Reset) hoạt động đồng bộ với hệ thống định tuyến Sing-box trên máy tính Windows.

---

## Bước 1: Kết nối thiết bị
1. Cắm dây mạng từ cổng **LAN 2 (Ethernet 3)** trên máy tính Windows vào cổng **E0 (hoặc POE)** của cục Aruba.
2. Đợi khoảng **3 - 5 phút** để Aruba khởi động xong. Đèn LED trên Aruba sẽ chuyển sang trạng thái nhấp nháy hoặc sáng ổn định.

---

## Bước 2: Truy cập trang quản trị Aruba
1. Trên máy tính Windows (hoặc điện thoại), mở danh sách Wi-Fi và kết nối vào Wi-Fi mặc định của Aruba:
   * Tên Wi-Fi mặc định thường có dạng: **`SetMeUp-XX:XX:XX`** hoặc **`Instant-XX:XX:XX`** (không có mật khẩu).
2. Mở trình duyệt Web (Chrome, Edge) và truy cập địa chỉ:
   👉 **`https://172.16.0.254:4343`**
3. **Bỏ qua cảnh báo bảo mật**:
   * Trình duyệt sẽ hiển thị cảnh báo đỏ *"Kết nối của bạn không phải là riêng tư"*.
   * Click vào nút **Advanced (Nâng cao)** ở phía dưới.
   * Click vào dòng **Proceed to 172.16.0.254 (unsafe)** / **Tiếp tục truy cập 172.16.0.254 (không an toàn)**.

---

## Bước 3: Đăng nhập & Đổi mật khẩu quản trị
1. Nhập thông tin tài khoản đăng nhập mặc định:
   * **Username**: `admin`
   * **Password**: `admin` (hoặc `123456`, hoặc số Serial Number in phía sau thiết bị đối với phiên bản ArubaOS mới).
2. Hệ thống sẽ yêu cầu bạn đổi mật khẩu mới ngay lập tức. Hãy đặt mật khẩu mới của bạn và lưu lại.

---

## Bước 4: Tạo mạng Wi-Fi mới (Bridge Mode)
Sau khi đăng nhập vào Dashboard quản trị Aruba, hãy tiến hành tạo mạng Wi-Fi phát cho Phone Farm:

1. Tại tab **Networks**, click vào nút **New** (hoặc dấu **+**) để tạo mạng Wi-Fi mới.
2. **Tab 1: General (Cấu hình chung)**
   * **Name (SSID)**: Điền tên Wi-Fi bạn muốn đặt (ví dụ: `Aruba6789`).
   * **Primary usage**: Chọn **Employee** (hoặc **Wireless**).
   * Click **Next**.

3. **Tab 2: VLAN (Cực kỳ quan trọng)**
   > [!IMPORTANT]
   > Đây là bước cốt lõi quyết định việc cấp IP và định tuyến thông qua Sing-Box trên máy tính.
   * **Client IP assignment**: Chọn **Network Assigned** (Có nghĩa là IP sẽ do máy chủ DHCP trên máy tính của bạn cấp phát, không phải do Aruba cấp).
   * **Client VLAN assignment**: Chọn **Default** (để chuyển tiếp toàn bộ dữ liệu trực tiếp sang cổng LAN cắm vào máy tính).
   * Click **Next**.

4. **Tab 3: Security (Bảo mật)**
   * **Security level**: Chọn **WPA2 Personal** (hoặc WPA2/WPA3 Personal).
   * **Passphrase**: Điền mật khẩu Wi-Fi bạn muốn đặt (ví dụ: `hanoi123`).
   * Click **Next**.

5. **Tab 4: Access (Quyền truy cập)**
   * Giữ nguyên cấu hình mặc định là **Unrestricted** (Không giới hạn).
   * Click **Finish**.

---

## Bước 5: Kiểm tra hoạt động
1. Dùng điện thoại kết nối vào Wi-Fi mới bạn vừa tạo (ví dụ: `Aruba6789`).
2. Mở Dashboard quản lý Router của bạn trên trình duyệt máy tính:
   👉 **[http://127.0.0.1:8000/](http://127.0.0.1:8000/)**
3. Điện thoại của bạn sẽ nhận được địa chỉ IP trong dải `192.168.88.x` (do máy tính cấp phát).
4. Thiết bị điện thoại sẽ lập tức xuất hiện trong danh sách **"Thiết bị"** trên giao diện Web Dashboard để bạn gán Proxy/Socks5 theo nhu cầu.
