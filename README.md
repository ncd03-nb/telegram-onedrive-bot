# Telegram → OneDrive với Cloudflare và GitHub Actions

Bot [@odrivegay_bot](https://t.me/odrivegay_bot) nhận link Google Drive, Dropbox hoặc HTTP(S), dùng GitHub Actions tải và upload lên OneDrive. Bot payload vẫn dùng Google Drive. **Controller chạy hoàn toàn trên Cloudflare**; Docker trên máy và Render đã dừng.

Bot dùng **một tin tiến độ riêng** và cập nhật nó trong suốt tác vụ; chỉ khi hoàn tất mới gửi **một tin kết quả reply lệnh gốc**. Runner cập nhật khoảng **2 giây/lần khi có thay đổi**, hiển thị %, dung lượng, tốc độ và thời gian. Khi đứng ở một bước, tin nhắn có heartbeat khoảng 10 giây; bot tôn trọng `retry_after` của Telegram. Tin kết quả hiện tên file và hai nút **Index Link / tải trực tiếp**.

File được truyền từ nguồn tới OneDrive qua rclone; không cần chờ Internet Archive nhập file. Runner xác minh dung lượng đích và MD5 nguồn nếu có trước khi báo hoàn thành.

Actions dùng **HTTP/1.1**, gói upload 50 MiB và bộ đệm tải trước **64 MiB**. Bài đo 512 MiB trên cùng runner đạt **10,94 MiB/s**, so với **4,15 MiB/s** bằng HTTP/2; checksum đích khớp. Đây là số đo có kiểm soát, tốc độ file thực tế còn phụ thuộc nguồn và OneDrive. Xem [kết quả đo](docs/onedrive-performance.md). Có thể tắt bộ đệm bằng `SOURCE_PREFETCH_MIB=0` hoặc khôi phục HTTP/2 bằng `ONEDRIVE_DISABLE_HTTP2=false`.

File tự xóa sau **12 giờ từ lúc hoàn tất**. Cloudflare kiểm tra ID/eTag trước khi xóa, tránh xóa file đã bị thay thế. Index Link hết hiệu lực khi hết hạn. Các lượt thử Archive cũ vẫn giữ lịch dọn riêng.

## Lệnh

- `/copy <link> [tên file]`
- `/status [mã tác vụ]`
- `/cancel <mã tác vụ>`
- `/find <từ khóa>`
- `/link <mã tác vụ>` — Index Link Cloudflare và link tải trực tiếp
- `/dl <mã tác vụ>` — link tải trực tiếp mới
- `/check` — nơi lưu hiện tại

Bot này chỉ nhận chat riêng từ người được phép. Link Drive riêng tư cần được chia sẻ cho tài khoản Google đã kết nối; link công khai vẫn chịu quyền tải/quota của Google. Chưa hỗ trợ thư mục Drive, Google Docs/Sheets hoặc MEGA mã hóa.

Link OTA OPlus `downloadCheck` cũng dùng trực tiếp với `/copy`: hỗ trợ các host con của `allawnos.com`, `allawntech.com` (gồm CN), `coloros.com` và `oplusota.com`. Runner gửi header updater tới endpoint kiểm tra, lấy redirect CDN rồi tải file ROM bằng luồng copy hiện có. Tên/kích thước lấy từ file CDN, không dùng tên `downloadCheck`. Link hết hạn hoặc phản hồi JSON báo lỗi (ví dụ mã 2306) sẽ báo thất bại **trước khi bắt đầu upload**, không tạo file JSON giả ROM trên OneDrive. Header updater chỉ gửi tới endpoint được hỗ trợ; OAuth Google, token GitHub và Telegram không gửi tới endpoint/CDN. Các giới hạn file và lịch xóa 12 giờ vẫn áp dụng.

Một số CDN OPlus gắn `Content-Encoding: UTF-8` cho ZIP nhị phân: runner giữ nguyên byte và chấp nhận nhãn này riêng cho luồng OPlus; vẫn từ chối mã hóa HTTP gzip/br/deflate. Kiểm thử ngày 21/10/2026: link CN trước đó bị copy thành JSON đã trả ZIP **8.198.755.980 byte (7,64 GiB)** với magic `PK`, đọc mẫu 32 byte thành công. Chưa copy lại toàn bộ file này trong bài kiểm tra. Bộ kiểm tra Python hiện có 91 bài, gồm luồng redirect tới ROM, JSON/CDN lỗi, host giả mạo, redirect tới IP nội bộ và không khởi tạo uploader khi API trả lỗi.

## Triển khai

Cloudflare Worker và hai Durable Objects nằm trong `cloudflare-failover/`; nó điều khiển cả bot OneDrive và bot payload. Xem [cấu hình và quy trình chuyển dữ liệu](docs/cloudflare-controller.md). Token Telegram, OAuth và token GitHub được lưu trong secrets; file cấu hình local nằm trong `config/` được Git bỏ qua.

Repo là **public**. Inputs và artifacts Actions chứa dữ liệu tác vụ được mã hóa AES-256-GCM, giữ link nguồn, thông tin chat và link tải khỏi log/artifact công khai. Artifact chỉ giữ metadata mã hóa trong hai ngày. Người có link tải có thể tải file trong thời hạn lưu.

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
cd cloudflare-failover
npm ci
npm test
```

[Giao diện health Cloudflare](https://telegram-bot-failover.der-templates.workers.dev/health). Bot payload: [telegram-payload-bot](https://github.com/ncd03-nb/telegram-payload-bot). Tài liệu host cũ được lưu ở [legacy-docker.md](docs/legacy-docker.md).

Cloudflare và GitHub Actions vẫn chịu quota, khả năng cấp runner và chính sách của nhà cung cấp. Tốc độ tải/upload phụ thuộc CDN và đường mạng runner tới cloud. Workflow `onedrive-speed.yml` đo upload file ngẫu nhiên 512 MiB, kiểm tra QuickXorHash và chuyển riêng các file thử vào thùng rác; không dùng dữ liệu người dùng.
