# Telegram → OneDrive cho file Google Drive khoảng 10 GB

Bot nhận link trong chat riêng, tải từ Google Drive bằng OAuth quyền chỉ đọc và upload theo chunk vào OneDrive Microsoft 365 Education qua rclone. Sau khi upload, bot trả link file và link tải trực tiếp, lưu file 12 giờ rồi chuyển vào thùng rác. Có Docker Compose cho host chính và Render Free cho host dự phòng kích hoạt thủ công.

Bạn cần token bot, Telegram user ID, đăng nhập OneDrive bằng mail sinh viên và đăng nhập Google. Phiên đăng nhập trên web không tự cấp OAuth cho bot. Không gửi mật khẩu, token hoặc file OAuth vào chat. Repo này không chứa credentials.

## Link nào dùng được?

| Nguồn | Điều kiện |
| --- | --- |
| Drive của bạn | Tài khoản Google đã đăng nhập phải có quyền đọc / tải file |
| Drive của người khác, link công khai | Bot tải được nếu Google cho phép tải; vẫn chịu quota |
| Drive của người khác, link riêng tư | Chủ file chia sẻ cho tài khoản Google kết nối với bot |
| Dropbox | Link chia sẻ công khai cho phép tải |
| HTTP / HTTPS | Link tải trực tiếp; không phải trang HTML |

Không hỗ trợ tải cả thư mục, shortcut, Google Docs/Sheets/Slides hoặc link MEGA mã hóa. Bot không bỏ qua quyền tải, quota Google hay chính sách của trường. Link Google Drive được nhận ở dạng `https://drive.google.com/file/d/FILE_ID/view` hoặc `https://drive.google.com/open?id=FILE_ID`; giữ nguyên `resourcekey` nếu link có trường này.

## Chọn host

| Phương án | Vai trò | Đánh đổi |
| --- | --- | --- |
| Máy cá nhân / NAS chạy Docker liên tục | Chính; không phát sinh tiền thuê host | Cần máy luôn bật và đường truyền đủ tốt; vẫn có chi phí điện / Internet |
| Oracle Cloud Always Free A1, home region Singapore nếu có capacity | Chính nếu muốn cloud không có phí sử dụng trong quota miễn phí | Đăng ký / xác minh tài khoản; VM nhàn rỗi có thể bị thu hồi; phải chọn đúng tài nguyên Always Free |
| Render Free Singapore | Dự phòng thủ công; cùng Dockerfile | Ngủ sau 15 phút, cold start khoảng một phút, ổ đĩa tạm, giới hạn lưu lượng; không đảm bảo job 10 GB |
| Koyeb Free | Có thể dùng cùng image và chế độ webhook | Free chỉ Frankfurt / Washington DC, ngủ sau một giờ, không volume; khoảng cách xa VN / Thái Lan |

Tài liệu Oracle hiện ghi A1 miễn phí 1.500 OCPU-giờ và 9.000 GB RAM-giờ / tháng, tương đương **tổng 2 OCPU + 12 GB RAM**, không dùng thông tin cũ 4 OCPU + 24 GB. Xem quota thực tế của tài khoản khi tạo VM. Có thể chọn A1 1 OCPU + 2 GB RAM cho bot này. Chỉ triển khai trong home region và tài nguyên có nhãn Always Free. Không bật dịch vụ hoặc vượt quota trả phí.

Không có cam kết host miễn phí vĩnh viễn + không ngủ + failover tức thời + truyền file 10 GB không giới hạn. Render có thể đình chỉ dịch vụ khi traffic do service khởi tạo cao; không thêm phương thức thanh toán giúp tránh tự động tính phí vượt phần miễn phí, nhưng khi hết hạn mức bot có thể bị ngưng. Render được cung cấp ở đây như lựa chọn dự phòng hobby, không phải bảo đảm chạy bền cho file lớn.

## 1. Chuẩn bị bot Telegram

Tạo bot riêng bằng [@BotFather](https://t.me/BotFather), lệnh `/newbot`. Một bot chỉ nên có một host chủ động. Bot được chuẩn bị cho người dùng hiện tại: [@odrivegay_bot](https://t.me/odrivegay_bot).

Trong PowerShell tại thư mục dự án:

```powershell
python scripts/telegram_identity.py
Copy-Item .env.example .env
```

Script hỏi token bằng ô nhập ẩn; gửi `/start` tới bot mới để lấy user ID. Sửa `.env`: điền `TELEGRAM_BOT_TOKEN` và `ALLOWED_USER_IDS`. Nhiều người dùng được phép: `123456789,987654321`. Người không có trong danh sách bị bỏ qua; bot không nhận lệnh trong nhóm.

## 2. Đăng nhập OneDrive bằng mail sinh viên

Tải rclone từ [trang chính thức](https://rclone.org/downloads/), giải nén và thêm vào PATH. Cấu hình trên máy của bạn có trình duyệt:

```powershell
rclone config --config .\config\rclone.conf
```

Chọn `n` → tên remote **onedrive** → loại **Microsoft OneDrive**. Chọn tự đăng nhập bằng trình duyệt, dùng tài khoản mail sinh viên; chọn **OneDrive Personal or Business**, rồi chọn đúng drive hiển thị loại **business** của tài khoản trường. Không chọn một site SharePoint khác nếu không cần.

Hoặc dùng helper có sẵn, với rclone trên PATH:

```powershell
python scripts/onedrive_login.py
```

Helper ghi URL OAuth vào `config/onedrive-auth-url.txt`; mở URL này trong trình duyệt đã đăng nhập OneDrive và duyệt quyền. Helper xin `Files.ReadWrite offline_access User.Read`, lấy drive cá nhân từ Graph và lưu `config/rclone.conf`; không in token. Không bật `disable_site_permission` trong helper vì tùy chọn này của rclone thay thế danh sách quyền tùy chỉnh.

Trong advanced options có thể bật `disable_site_permission=true` để không yêu cầu quyền tìm SharePoint sites nếu bạn chỉ dùng OneDrive của mình. Nếu báo “Need admin approval” hoặc “AADSTS…”, trường đang giới hạn OAuth; cần quyền được trường cho phép. Bot không thể đảm bảo mọi tenant trường đều kết nối được. Rclone có hướng dẫn tenant-specific và SharePoint v2 cho một số cấu hình; xem [OneDrive backend](https://rclone.org/onedrive/) nếu cách chuẩn không dùng được.

Giữ `rclone.conf` chưa mã hóa trong thư mục cấu hình riêng đã giới hạn quyền đọc; bot hiện không hỗ trợ config được mã hóa bằng mật khẩu. File chứa refresh token và phải được giữ bí mật.

Kiểm tra trước khi chạy:

```powershell
rclone about onedrive: --config .\config\rclone.conf
rclone lsd onedrive: --config .\config\rclone.conf
```

OneDrive của trường phải được kích hoạt và còn quota cho file 10 GB; bot không tăng quota tài khoản.

## 3. Đăng nhập Google Drive — khuyến nghị cho file 10 GB

1. Vào [Google Cloud Console](https://console.cloud.google.com/), tạo project riêng và bật **Google Drive API**. Bot chỉ gọi Drive API; không cần bật máy ảo hoặc dịch vụ trả phí.
2. Trong **Google Auth Platform**, cấu hình Branding / Audience. Với tài khoản cá nhân chọn External. Khi đang Testing, thêm tài khoản Google của bạn vào Test users.
3. Tạo OAuth client loại **Desktop app**, tải JSON client về máy, đặt trong `config/google-client.json`.
4. Chạy lệnh bên dưới và đăng nhập tài khoản Google sẽ được dùng để tải. Script chỉ xin `drive.readonly` và lưu refresh token trong `config/google-token.json`.

```powershell
python scripts/google_login.py --client .\config\google-client.json
```

Nếu app External còn ở trạng thái **Testing**, refresh token với quyền Drive thường hết hạn sau **7 ngày**. Để sử dụng lâu dài, chuyển app sang In production theo yêu cầu Google; việc xác minh / cảnh báo quyền nhạy cảm còn phụ thuộc đối tượng dùng và cấu hình app. Sau khi đổi trạng thái, đăng nhập lại. Không có token nào được bảo đảm tồn tại vĩnh viễn: người dùng hoặc tổ chức có thể thu hồi quyền.

Bạn có thể dùng tài khoản Gmail cá nhân cho Google Drive và mail sinh viên cho OneDrive; hai tài khoản không cần trùng nhau. File riêng tư của người khác phải được chia sẻ cho **tài khoản Google này**. Các file “Anyone with the link” cũng có thể tải qua API nếu Google cho phép. Khi có Google OAuth, bot dùng API và không âm thầm chuyển sang tải ẩn danh nếu OAuth / quyền bị lỗi.

Không có `google-token.json`: bot vẫn thử tải Drive công khai bằng link tải / form xác nhận, nhưng luồng này phụ thuộc trang tải Google và không bảo đảm cho file 10 GB.

## 4. Chạy Docker

Trên máy Windows, mở Docker Desktop với Linux containers. Tại thư mục dự án:

```powershell
docker compose up -d --build
docker compose logs -f --tail 50 bot
```

`/health` được bind ở `http://127.0.0.1:8081/health` (đổi bằng `HOST_PORT`), không cần mở cổng Internet cho chế độ polling. Compose giữ OAuth và hàng đợi trong `./config` và `./data`, giới hạn RAM container 512 MB và tự khởi động lại khi Docker daemon hoạt động.

Nếu đưa lên Ubuntu / Oracle A1, cài Docker Engine + Compose theo [tài liệu Docker](https://docs.docker.com/engine/install/ubuntu/), chuyển thư mục này và hai file cấu hình bí mật qua SSH. Chuẩn bị quyền volume cho user container UID 10001:

```bash
mkdir -p config data
sudo chown -R 10001:10001 config data
sudo chmod 700 config data
sudo chmod 600 config/rclone.conf config/google-token.json
cp .env.example .env
# Điền token và user ID vào .env bằng editor, rồi:
docker compose up -d --build
```

Không đưa `.env`, `config/`, `data/` lên Git. Để host chính chạy ngay sau reboot, Docker service cần được bật lúc boot. Build đa kiến trúc dùng image Python / rclone có bản ARM64; quá trình build ARM64 chưa được kiểm thử tại workspace Windows AMD64 này.

## 5. Dùng bot

```text
/copy https://drive.google.com/file/d/FILE_ID/view
/copy https://drive.google.com/file/d/FILE_ID/view ROM.zip
/status
/status MÃ_TÁC_VỤ
/cancel MÃ_TÁC_VỤ
/find ROM
/find
/link MÃ_TÁC_VỤ
/dl MÃ_TÁC_VỤ
/check
```

Gửi link trần cũng được. File được lưu ở `TelegramUploads/<mã tác vụ>/<tên file>` để tránh ghi đè file khác. Tên file tùy chọn có thể có khoảng trắng. Upload thành công trả hai link: URL mở file trên OneDrive và URL tải trực tiếp do Microsoft cấp. URL mở file tuân theo quyền OneDrive hiện có; URL tải trực tiếp cho phép người có URL tải trong thời gian nó còn hiệu lực.

`/find <từ khóa>` (alias `/search`) tìm tối đa 10 file còn hạn mà chính bạn đã upload qua bot; `/find` liệt kê file gần đây. Lệnh này không quét toàn bộ OneDrive. `/link <mã>` trả cả hai link, `/dl <mã>` (alias `/download`) lấy link tải trực tiếp mới. Link tải Microsoft có thời hạn ngắn, thường khoảng 1 giờ, nên bot gọi Graph để lấy mới mỗi lần; không lưu URL tải ký vào DB / manifest. Lấy link không gia hạn 12 giờ.

Lịch xóa bắt đầu ngay khi upload được xác nhận hoàn tất. Mặc định `RETENTION_HOURS=12`, kiểm tra mỗi `CLEANUP_INTERVAL_SECONDS=60` giây. Bot chỉ xóa file của các job hoàn tất từ phiên bản có tính năng này; không xóa file vốn có trên OneDrive hay job thất bại / hủy. Khi xóa, bot kiểm tra ID và eTag gốc, rồi dùng DELETE với If-Match: file đã chỉnh sửa, thay thế hoặc di chuyển sẽ được bảo vệ. Xóa đưa file vào Recycle Bin của OneDrive, chưa xóa vĩnh viễn hay dọn thùng rác.

Lịch được lưu trong SQLite host và file `.bot-retention.json` cùng thư mục job trên OneDrive. Khi chuyển host, bot phục hồi lịch và quyền sở hữu từ manifest mỗi 5 phút, giữ hạn ban đầu. Manifest không chứa OAuth, URL nguồn hoặc URL tải ký. Bot offline / host miễn phí đang ngủ / OAuth lỗi có thể xóa trễ; sẽ thử lại khi chạy được. Nếu không ghi được manifest, bot báo rõ và lịch chỉ còn trong DB host. Khi mất cả DB lẫn manifest, bot không đoán rồi xóa file.

Một file được chạy tại một thời điểm; mỗi người tối đa 3 job chờ / chạy, toàn bot tối đa 20. Mặc định tối đa **12 GiB** / file, timeout **6 giờ**. Có thể sửa `MAX_FILE_GIB` và `TRANSFER_TIMEOUT_SECONDS` trong `.env`. Chat Telegram chỉ chứa link nên không chịu giới hạn upload file 10 GB của Telegram.

Tiến độ `/status` là số byte đã đọc / đưa vào luồng upload, chưa phải số byte đã commit trên OneDrive. “Hoàn tất” chỉ xuất hiện sau rclone thành công và OneDrive trả dung lượng khớp. Với Google API có MD5 metadata, bot kiểm tra MD5 luồng nguồn. Đây không phải đọc lại toàn bộ file OneDrive để so MD5.

Link thiếu `Content-Length` và không có metadata Drive được tải tạm tối đa 512 MiB; file Drive 10 GB dùng metadata biết trước kích thước nên được stream. Đặt `MAX_SPOOL_MIB=0` để từ chối nguồn không biết dung lượng.

## 6. Host dự phòng Render Free

Đưa **riêng thư mục dự án này** thành một Git repository; không dùng repository lớn chứa ROM. Kết nối repo với Render Blueprint qua `render.yaml`. Service mặc định `BOT_MODE=standby`: có health endpoint nhưng chưa đăng ký webhook và chưa nhận Telegram updates.

Điền secrets trong Render Dashboard:

- `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`: giống host chính.
- `RCLONE_CONFIG_B64`: nội dung Base64 của `config/rclone.conf`.
- `GOOGLE_TOKEN_B64`: nội dung Base64 của `config/google-token.json`.
- `WEBHOOK_URL`: URL gốc service, ví dụ `https://telegram-onedrive-standby.onrender.com`.
- `WEBHOOK_SECRET`: Blueprint sinh tự động; giữ giá trị 32–256 ký tự chữ, số, `_`, `-`.

Xuất Base64 **chỉ trong terminal riêng** rồi paste trực tiếp vào Render Secrets:

```powershell
python scripts/export_secrets.py .\config\rclone.conf
python scripts/export_secrets.py .\config\google-token.json
```

Base64 không phải mã hóa. Không chia sẻ output, không lưu vào README hoặc Git. Hãy cập nhật bản dự phòng sau khi đăng nhập lại / refresh credentials trên host chính; OAuth trên ổ đĩa tạm của Render mất sau restart và được dựng lại từ các secrets đã lưu.

**Khi chuyển sang dự phòng:**

1. Dừng bot host chính: `docker compose stop bot`. Nếu host mất kết nối, phải bảo đảm nó không tự quay lại nhận updates trong thời gian dùng dự phòng.
2. Đổi `BOT_MODE` của Render thành `webhook`, lưu và redeploy. Bot tự đặt webhook `/telegram` với secret và không xóa Telegram pending updates.
3. Mở `/health`, kiểm tra mode `webhook`, rồi gửi `/check` trong chat. Gửi lại link cho những tác vụ bị ngắt sau khi kiểm tra thư mục OneDrive để tránh bản trùng.

**Khi về host chính:**

1. Chờ / hủy job đang chạy ở Render; đổi Render về `standby` và redeploy hoàn tất.
2. Cập nhật cấu hình OAuth của host chính nếu có đăng nhập lại ở nơi khác, rồi chạy `docker compose up -d bot`. Chế độ polling tự xóa webhook và giữ pending updates.
3. Xác nhận Render đã standby, host chính `/health` mode `polling`, gửi `/check`.

Telegram chỉ có một webhook cho một bot; không chạy hai poller. Khóa file chỉ ngăn hai process trên cùng volume, không ngăn hai host khác nhau. Đây là **failover thủ công**, không có router / shared queue / bầu chọn leader tự động. Lịch xóa và file đã hoàn tất có thể phục hồi từ OneDrive manifest; hàng đợi và tiến độ **không đồng bộ giữa hai host**. Job đang chạy không tiếp tục từ byte cũ khi chuyển host. Render có thể ngủ hoặc restart ngay cả khi job có kết nối outbound; webhook không bảo đảm giữ host awake đến hết 10 GB hoặc xóa đúng giờ. Không dùng ping liên tục để né chính sách host miễn phí.

## Tốc độ và phục hồi

Luồng transfer với Google API: `Google Drive → aiohttp (block 1 MiB) → rclone stdin → OneDrive (chunk 10 MiB)`. Không có sao chép trực tiếp giữa Google và Microsoft ở phía server: dữ liệu đi qua host bot. RAM không tăng theo dung lượng file.

Chọn host không ngủ và có throughput tốt tới **cả Google lẫn Microsoft**. Singapore là điểm bắt đầu hợp lý cho VN / Thái Lan, chưa phải kết luận nhanh nhất nếu chưa đo. Khoảng cách Telegram chỉ ảnh hưởng nhận lệnh; throughput cloud mới quyết định thời gian file 10 GB. Giới hạn lý thuyết cho 10 GB: khoảng 13 phút 20 giây ở 100 Mbps hoặc 2 phút 40 giây ở 500 Mbps; thực tế lâu hơn do quota, API và mạng. Đây là phép tính, chưa phải benchmark.

Rclone có retry chunk khi backend cho phép, nhưng luồng không lưu đủ file nên **không resume toàn tác vụ sau restart / lỗi nguồn**. Job đang chạy khi host restart được đánh dấu `interrupted`; queued jobs còn trên volume host chính tiếp tục. Kiểm tra thư mục job nếu upload đã commit nhưng bot chưa kịp lưu trạng thái hoặc bạn hủy sát lúc hoàn tất. Bot không tự xóa file sau thất bại / hủy để tránh xóa kết quả đã commit.

Hàng đợi SQLite có thể giữ URL riêng tư / link ký trong khi job chờ; file OAuth và data chỉ nên được host owner đọc. URL nguồn được xóa khi job kết thúc, payload update xóa sau xử lý, lịch sử và ID dedup được dọn sau 7 ngày. Lịch xóa chưa hoàn thành / file có xung đột được giữ để không mất thông tin. Link nguồn hết hạn trong lúc chờ phải gửi lại. Redirect / DNS tới mạng nội bộ bị từ chối; bot không nhận cookies hoặc credentials người dùng gửi trong chat.

## Kiểm thử

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Test bao gồm subprocess streaming thật với rclone giả, file thiếu byte, MD5 sai, size OneDrive sai, OAuth upload lỗi, spool cleanup, hủy subprocess, dedup updates, giới hạn hàng đợi, quyền sở hữu job, webhook secret, SSRF, lịch xóa / phục hồi, bảo vệ file bị sửa, tìm file / link không gia hạn và HTTP Graph mô phỏng kiểm tra JSON / If-Match. GitHub Actions chạy tests và build Docker. Không thay thế việc kiểm thử tài khoản / quota thực tế. Sau đăng nhập, hãy thử file nhỏ trước rồi file 10 GB.

## Nguồn kỹ thuật / giới hạn host

- [Google: download files bằng Drive API](https://developers.google.com/workspace/drive/api/guides/manage-downloads)
- [Google: OAuth và vòng đời refresh token](https://developers.google.com/identity/protocols/oauth2)
- [rclone: OneDrive Education / Business](https://rclone.org/onedrive/)
- [rclone: streaming rcat và giới hạn retry](https://rclone.org/commands/rclone_rcat/)
- [Microsoft: link tải tạm thời của driveItem](https://learn.microsoft.com/en-us/graph/api/resources/driveitem?view=graph-rest-1.0)
- [Microsoft: xóa driveItem vào Recycle Bin và If-Match](https://learn.microsoft.com/en-us/graph/api/driveitem-delete?view=graph-rest-1.0)
- [Oracle: Always Free và thu hồi idle VM](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [Render: Free, cold start, disk và traffic](https://render.com/docs/free)
- [Koyeb: Free Instances, region và scale-to-zero](https://www.koyeb.com/docs/reference/instances)

Các giới hạn host được tra ngày 13/10/2026; kiểm tra lại trong Dashboard trước khi triển khai.
