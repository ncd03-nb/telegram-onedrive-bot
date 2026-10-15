# Đo upload OneDrive — 15/10/2026

Bot đã trở lại OneDrive. Controller Cloudflare, worker Actions, Index Link, link trực tiếp và lịch xóa 12 giờ được giữ nguyên. Các lượt thử Archive cũ giữ lịch dọn riêng; lệnh mới không gửi file lên Archive.

## Upload từ cùng runner

[Workflow và artifact kết quả](https://github.com/ncd03-nb/telegram-onedrive-bot/actions/runs/38674274562). Một file ngẫu nhiên 512 MiB nằm trên ổ runner, rclone 1.75.1, cùng tài khoản OneDrive, chạy từng trường hợp lần lượt. Tốc độ gồm khởi tạo session và commit file, không gồm bước xác minh sau upload.

| Gói upload | Giao thức | Thời gian | MiB/s |
|---|---|---:|---:|
| 50 MiB | HTTP/2 được bật | 123,35 giây | 4,15 |
| 55 MiB | HTTP/2 được bật | 116,04 giây | 4,41 |
| 55 MiB | Chỉ HTTP/1.1 | 47,48 giây | 10,78 |
| 50 MiB | Chỉ HTTP/1.1 | 46,81 giây | 10,94 |

Cả bốn file đều khớp QuickXorHash của file nguồn và đã chuyển vào thùng rác sau đo. Chọn 50 MiB + HTTP/1.1 cho production. Không tăng fragment tới 60 MiB: [Graph yêu cầu fragment nhỏ hơn 60 MiB, gửi theo thứ tự](https://learn.microsoft.com/en-us/graph/api/driveitem-createuploadsession?view=graph-rest-1.0). Tăng số luồng của một upload session không phải cách hợp lệ để tăng tốc.

[Thử thêm runner miễn phí ARM](https://github.com/ncd03-nb/telegram-onedrive-bot/actions/runs/38674705731) với HTTP/1.1 cho kết quả 9,39 MiB/s (55 MiB) và 9,32 MiB/s (50 MiB), cả hai checksum khớp và file đã vào thùng rác. Vì không nhanh hơn x64 trong lượt đo này, giữ `ubuntu-latest` cho production.

## Nguồn Drive

[Workflow chỉ đọc](https://github.com/ncd03-nb/telegram-onedrive-bot/actions/runs/38674435816): đọc mẫu 512 MiB của một file Drive có quyền truy cập, không đổi file/quyền chia sẻ. Một luồng đạt 44,52 MiB/s; bốn luồng đạt 126,89 MiB/s. SHA256 của từng đoạn đều khớp giữa hai lượt. Nguồn này nhanh hơn upload OneDrive; chưa cần thêm Range song song vào đường truyền production.

## Bộ đệm và kiểm tra

Bộ đệm nguồn 64 MiB cho phép đọc tiếp trong khi rclone đang gửi một fragment, giảm thời gian chờ giữa hai bước. Hàng đợi có giới hạn, không lưu cả file 10 GiB trong RAM/ổ đĩa. Lỗi nguồn, hủy tác vụ và checksum vẫn được kiểm tra. 82 bài kiểm tra Python và 15 bài kiểm tra Cloudflare đều đạt; CI build container thành công.

Một tác vụ `/copy` qua controller thật đã upload file thử, nhận artifact mã hóa, trả Index Link HTTP 302 và tải trực tiếp HTTP 206. File thử và manifest đã được chuyển vào thùng rác sau xác minh. Tốc độ nêu trên không phải cam kết cho mọi nguồn, thời điểm hoặc vị trí runner.
