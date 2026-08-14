# Cách expose server cho app Android (thay cho ngrok)

Trả lời cho des.md §6 — "CẦN XÁC NHẬN: Năng lực connection của hạ tầng".

## Chẩn đoán: đúng là ngrok free tier

Số liệu trong des.md khớp gần như tuyệt đối với giới hạn của ngrok free:

| Đo được (des.md §6) | Giới hạn ngrok free |
|---|---|
| 123 request/phút | ~120 connection/phút |
| `SSLHandshakeException: connection closed` × 323 | ngrok đóng connection khi vượt rate |
| bursty 16 lỗi/giây, success rate 37% | hành vi điển hình khi bị throttle |

Tunnel đóng connection ngay giữa lúc TLS handshake → client thấy `SSLHandshakeException` chứ
không phải HTTP 429, nên rất dễ bị hiểu nhầm là lỗi mạng/TLS.

Ngoài ra mỗi request phải TLS handshake lại qua tunnel (0.33s đơn lẻ, 1.29s khi song song)
là chi phí rất lớn cho một feed cần ~123 request/phút.

## Phương án 1 (khuyến nghị): LAN trực tiếp — không tunnel

Nếu điện thoại và máy chạy server ở **cùng WiFi**, đây là lựa chọn tốt nhất:
không tunnel → không giới hạn rate, không TLS handshake, latency thấp nhất.

```bash
python main.py
# in ra: [*] LAN : http://192.168.x.x:3000/api/feed
```

Dùng đúng URL LAN đó trong app. Server đã bind sẵn `0.0.0.0` nên nhận kết nối từ máy khác.

Hai thứ cần làm 1 lần:

1. **Mở firewall Windows cho port 3000** (chạy PowerShell với quyền admin):
   ```powershell
   New-NetFirewallRule -DisplayName "ZVideo HLS 3000" -Direction Inbound `
       -Protocol TCP -LocalPort 3000 -Action Allow -Profile Private
   ```

2. **Cho phép cleartext HTTP trong app Android** (vì LAN dùng `http://`, không phải `https://`).
   Trong `AndroidManifest.xml`:
   ```xml
   <application android:usesCleartextTraffic="true" ...>
   ```
   Hoặc chặt chẽ hơn: khai báo `network_security_config.xml` chỉ cho phép cleartext với
   subnet LAN đang dùng.

Nhược điểm: chỉ chạy được khi 2 thiết bị cùng mạng.

## Phương án 2: Cloudflare Tunnel — khi cần truy cập từ ngoài mạng

Miễn phí, **không giới hạn số request** như ngrok free, và có HTTPS thật (không cần
sửa cleartext ở app).

```bash
# cài 1 lần
winget install --id Cloudflare.cloudflared

# chạy mỗi lần cần (quick tunnel, không cần tài khoản)
cloudflared tunnel --url http://localhost:3000
# -> https://<random>.trycloudflare.com
```

Lấy URL https đó đặt vào `PUBLIC_BASE_URL` để link asset trong feed trỏ đúng ra ngoài:

```bash
# PowerShell
$env:PUBLIC_BASE_URL = "https://<random>.trycloudflare.com"
python main.py
```

Không đặt `PUBLIC_BASE_URL` thì server tự suy base URL theo host client gọi tới — thường
vẫn đúng, nhưng đặt tường minh sẽ chắc chắn hơn khi đứng sau proxy.

Quick tunnel đổi domain mỗi lần chạy. Muốn domain cố định thì tạo named tunnel
(cần tài khoản Cloudflare miễn phí + 1 domain).

## Phương án 3: Tailscale — mạng riêng, ổn định lâu dài

Cài Tailscale trên cả máy server và điện thoại, cả hai vào chung tailnet, rồi gọi
`http://<tên-máy>:3000`. Không giới hạn rate, hoạt động cả khi khác mạng, nhưng phải
cài app trên thiết bị test.

## Phía server đã xử lý những gì (des.md §6)

| Câu hỏi trong des.md | Trạng thái |
|---|---|
| Có bật HTTP keep-alive? | **Có** — uvicorn giữ keep-alive mặc định, response không có `Connection: close` |
| Có giới hạn connection đồng thời per-IP? | **Không** — uvicorn không giới hạn; giới hạn trước đây đến từ tunnel |
| Số connection cho mỗi video | **Giảm mạnh**: mỗi rendition giờ là **1 file** `.mp4dv` (byte-range) thay vì hàng chục file `.ts` rời |

Nếu vẫn thấy nghẽn khi nhiều client cùng lúc, tăng số worker:

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 3000 --workers 4
```
