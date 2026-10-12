# Kiểm tra tại workspace ngày 13/10/2026

- Docker image AMD64 build thành công với Python 3.12 và rclone 1.75.1.
- Docker Compose được validate bằng `docker compose --env-file .env.example config --no-env-resolution --quiet`.
- 38 unit/integration tests pass trên Windows Python 3.14 và container Linux Python 3.12.
- Tests mới kiểm tra 12h TTL, phục hồi manifest sau mất DB, ownership khi tìm file, link không gia hạn, ID / eTag / If-Match chống xóa file thay thế, Graph HTTP JSON và parsing OAuth an toàn.
- Container smoke dùng **rclone thật**, chuyển 3 MiB vào local backend qua stdin với đúng flags production; SHA256 và kích thước khớp.
- Chạy bot `standby` trong container: `/health` HTTP 200, `/telegram` HTTP 503; không gọi Telegram và không chiếm webhook.
- SIGTERM kết thúc standby sạch và đóng SQLite.
- OneDrive Business thật: upload file test 56 KiB, lấy hai link, tải lại và so nội dung, ghi manifest, tìm file theo owner, không xóa trước hạn, giả lập hết hạn riêng file test rồi recycle bằng Graph. File và manifest test đã xóa, thư mục test trống đã dọn. Không chờ 12 giờ thực tế.
- Bot chính đã chạy Docker polling trên máy người dùng, health tại `http://127.0.0.1:8081/health`; Telegram token và owner allowlist đã cấu hình tại máy, không có trong Git.
- Google OAuth chỉ đọc đã kết nối. Refresh token và `Drive v3 about` được kiểm tra từ container Docker chính, trả HTTP 200 / `drive#about`; không in credentials hoặc tên file cá nhân.
- Render Free Singapore đã triển khai service `telegram-onedrive-standby`; endpoint HTTPS `/health` trả HTTP 200 và mode `standby`. Bot chính vẫn polling; dự phòng không đăng ký webhook. URL webhook thực tế đã lưu cho bước chuyển host thủ công.
- GitHub Actions của commit `1c3f4aa` đã chạy tests và Docker build thành công.

Test download Drive dùng metadata mô phỏng kích thước 10 GiB; **chưa chuyển file 10 GB thật qua Google / Microsoft**. Unit tests upload cloud dùng rclone subprocess fixture, smoke bổ sung dùng OneDrive thật như trên. Chưa deploy lên Oracle, chưa benchmark mạng, chưa build ARM64 hoặc thực hiện chuyển Telegram webhook sang Render thật. Cần thử một file Drive nhỏ trước rồi một file 10 GB. Google app Testing cấp refresh token 7 ngày; đây không phải OAuth vĩnh viễn.

Lệnh smoke (tests không được đóng trong production image):

```powershell
docker run --rm -v D:\NothingsVN_Project\telegram-onedrive-bot\tests:/app/tests:ro telegram-onedrive-bot:local python -m tests.smoke_container
```
