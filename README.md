# Telegram → OneDrive với Cloudflare và GitHub Actions

Bot [@odrivegay_bot](https://t.me/odrivegay_bot) nhận link Google Drive, Dropbox hoặc HTTP(S), dùng GitHub Actions tải và upload vào OneDrive Microsoft 365 Education. **Controller chạy hoàn toàn trên Cloudflare**; Docker trên máy và Render đã dừng.

Runner sửa cùng một tin nhắn tiến độ khoảng **2 giây/lần khi có thay đổi**, hiển thị %, dung lượng, tốc độ và thời gian. Khi đứng ở một bước, tin nhắn có heartbeat khoảng 10 giây; bot tôn trọng `retry_after` của Telegram. Sau khi hoàn tất, bot gửi tên file và hai nút **Index Link / tải trực tiếp**.

File do bot upload được đặt lịch xóa sau **12 giờ từ lúc hoàn tất**. Cloudflare lưu thời hạn và kiểm tra file ID/eTag trước khi chuyển file vào thùng rác; file đã được thay thế hoặc sửa sẽ được bảo vệ. Index Link chuyển hướng tới URL tải OneDrive mới, dữ liệu file đi trực tiếp từ OneDrive tới người tải.

## Lệnh

- `/copy <link> [tên file]`
- `/status [mã tác vụ]`
- `/cancel <mã tác vụ>`
- `/find <từ khóa>`
- `/link <mã tác vụ>` — Index Link Cloudflare và link tải trực tiếp
- `/dl <mã tác vụ>` — link tải trực tiếp mới
- `/check` — quota OneDrive

Bot này chỉ nhận chat riêng từ người được phép. Link Drive riêng tư cần được chia sẻ cho tài khoản Google đã kết nối; link công khai vẫn chịu quyền tải/quota của Google. Chưa hỗ trợ thư mục Drive, Google Docs/Sheets hoặc MEGA mã hóa.

## Triển khai

Cloudflare Worker và hai Durable Objects nằm trong `cloudflare-failover/`; nó điều khiển cả bot OneDrive và bot payload. Xem [cấu hình và quy trình chuyển dữ liệu](docs/cloudflare-controller.md). Token Telegram, OAuth và token GitHub được lưu trong secrets; file cấu hình local nằm trong `config/` được Git bỏ qua.

Repo là **public**. Inputs và artifacts Actions chứa dữ liệu tác vụ được mã hóa AES-256-GCM, giữ link nguồn, thông tin chat và link tải khỏi log/artifact công khai. File upload nằm ở OneDrive; artifact chỉ giữ metadata mã hóa trong hai ngày.

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
cd cloudflare-failover
npm ci
npm test
```

[Giao diện health Cloudflare](https://telegram-bot-failover.der-templates.workers.dev/health). Bot payload: [telegram-payload-bot](https://github.com/ncd03-nb/telegram-payload-bot). Tài liệu host cũ được lưu ở [legacy-docker.md](docs/legacy-docker.md).

Cloudflare và GitHub Actions vẫn chịu quota, khả năng cấp runner và chính sách của nhà cung cấp. Tốc độ tải/upload phụ thuộc CDN và đường mạng runner tới cloud.
