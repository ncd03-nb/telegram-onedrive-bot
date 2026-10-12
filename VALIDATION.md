# Kiểm tra tại workspace ngày 13/10/2026

- Docker image AMD64 build thành công với Python 3.12 và rclone 1.75.1.
- Docker Compose được validate bằng `docker compose --env-file .env.example config --no-env-resolution --quiet`.
- 37 unit/integration tests pass trên Windows Python 3.14 và container Linux Python 3.12.
- Tests mới kiểm tra 12h TTL, phục hồi manifest sau mất DB, ownership khi tìm file, link không gia hạn, ID / eTag / If-Match chống xóa file thay thế, Graph HTTP JSON và parsing OAuth an toàn.
- Container smoke dùng **rclone thật**, chuyển 3 MiB vào local backend qua stdin với đúng flags production; SHA256 và kích thước khớp.
- Chạy bot `standby` trong container: `/health` HTTP 200, `/telegram` HTTP 503; không gọi Telegram và không chiếm webhook.
- SIGTERM kết thúc standby sạch và đóng SQLite.

Test download Drive dùng metadata mô phỏng kích thước 10 GiB; **chưa chuyển file 10 GB thật qua Google / Microsoft**. Tests upload cloud dùng rclone subprocess fixture. Chưa có Telegram token / Google OAuth / OneDrive OAuth của người dùng; chưa deploy lên Oracle / Render, chưa benchmark mạng, chưa build ARM64. Sau đăng nhập cần thử một file nhỏ trước rồi một file 10 GB.

Lệnh smoke (tests không được đóng trong production image):

```powershell
docker run --rm -v D:\NothingsVN_Project\telegram-onedrive-bot\tests:/app/tests:ro telegram-onedrive-bot:local python -m tests.smoke_container
```
