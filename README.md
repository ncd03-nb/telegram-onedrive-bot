# Telegram → Internet Archive với Cloudflare và GitHub Actions

Bot [@odrivegay_bot](https://t.me/odrivegay_bot) nhận link Google Drive, Dropbox hoặc HTTP(S), dùng GitHub Actions tải và upload công khai lên Internet Archive. Chỉ bot @odrivegay_bot chuyển nơi lưu; bot payload vẫn dùng Google Drive. **Controller chạy hoàn toàn trên Cloudflare**; Docker trên máy và Render đã dừng.

Bot dùng **một tin tiến độ riêng** và cập nhật nó trong suốt tác vụ; chỉ khi hoàn tất mới gửi **một tin kết quả reply lệnh gốc**. Runner cập nhật khoảng **2 giây/lần khi có thay đổi**, hiển thị %, dung lượng, tốc độ và thời gian. Khi đứng ở một bước, tin nhắn có heartbeat khoảng 10 giây; bot tôn trọng `retry_after` của Telegram. Tin kết quả hiện tên file và hai nút **Index Link / tải trực tiếp**.

File mới upload bằng multipart 8 luồng × 16 MiB, giới hạn bộ nhớ 128 MiB cho các phần đang gửi. Runner tải nguồn xuống ổ tạm, kiểm tra checksum nguồn rồi upload. Bot chỉ gửi link sau khi metadata Archive xác nhận MD5/kích thước và tải Range thành công. File Archive là **công khai**.

File được đặt lịch xóa sau **12 giờ từ lúc upload hoàn tất**. Cloudflare kiểm tra identifier, tên, MD5 và kích thước trước khi gửi yêu cầu xóa. Archive nhập/xóa file bất đồng bộ nên không hứa xóa tức thời đúng giây hết hạn. Index Link ngừng phục vụ khi hết hạn; link Archive công khai vẫn dùng được cho đến khi Archive xử lý xóa. File OneDrive cũ giữ nguyên link và lịch xóa bảo vệ bằng eTag.

## Lệnh

- `/copy <link> [tên file]`
- `/status [mã tác vụ]`
- `/cancel <mã tác vụ>`
- `/find <từ khóa>`
- `/link <mã tác vụ>` — Index Link Cloudflare và link tải trực tiếp
- `/dl <mã tác vụ>` — link tải trực tiếp mới
- `/check` — nơi lưu hiện tại

Bot này chỉ nhận chat riêng từ người được phép. Link Drive riêng tư cần được chia sẻ cho tài khoản Google đã kết nối; link công khai vẫn chịu quyền tải/quota của Google. Chưa hỗ trợ thư mục Drive, Google Docs/Sheets hoặc MEGA mã hóa.

## Triển khai

Cloudflare Worker và hai Durable Objects nằm trong `cloudflare-failover/`; nó điều khiển cả bot OneDrive và bot payload. Xem [cấu hình và quy trình chuyển dữ liệu](docs/cloudflare-controller.md). Token Telegram, OAuth, khóa IAS3 và token GitHub được lưu trong secrets; file cấu hình local nằm trong `config/` được Git bỏ qua.

Repo là **public**. Inputs và artifacts Actions chứa dữ liệu tác vụ được mã hóa AES-256-GCM, giữ link nguồn, thông tin chat và link tải khỏi log/artifact công khai. File upload mới nằm công khai ở Internet Archive; artifact chỉ giữ metadata mã hóa trong hai ngày.

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
cd cloudflare-failover
npm ci
npm test
```

[Giao diện health Cloudflare](https://telegram-bot-failover.der-templates.workers.dev/health). Bot payload: [telegram-payload-bot](https://github.com/ncd03-nb/telegram-payload-bot). Tài liệu host cũ được lưu ở [legacy-docker.md](docs/legacy-docker.md).

Cloudflare và GitHub Actions vẫn chịu quota, khả năng cấp runner và chính sách của nhà cung cấp. Tốc độ tải/upload phụ thuộc CDN và đường mạng runner tới cloud. Đo 2 GiB từ Actions cho kết quả 12,91 MiB/s tổng upload; đây không phải mức tốc độ bảo đảm. Không upload dữ liệu riêng tư lên Archive.
