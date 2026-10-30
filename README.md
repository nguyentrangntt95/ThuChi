# Sổ Thu Chi

App quản lý chi tiêu cá nhân. Flask + PostgreSQL ở backend, frontend một file `index.html` (Preact + htm). Chạy trên Railway, deploy tự động khi push `main`.

## Làm việc trên máy mới (5 phút)

```bash
# 1. Lấy code
gh auth login                       # tài khoản GitHub nguyentrangntt95
gh repo clone nguyentrangntt95/ThuChi
cd ThuChi

# 2. Xem giao diện với dữ liệu giả (không cần database)
python tools/mock_server.py         # mở http://127.0.0.1:8765, cách đăng nhập giả ở đầu file

# 3. Kiểm tra cú pháp trước khi push (cần node)
python tools/check_syntax.py

# 4. Deploy = push
git push origin main                # Railway tự build, ~30 giây
```

Dùng Claude Code: mở thư mục repo, file `CLAUDE.md` chứa toàn bộ quy ước, logic nghiệp vụ và cách lấy dữ liệu thật.

## Dữ liệu thật

Chỉ nằm trong Postgres trên Railway (project "Trang Thu Chi"). Để đọc ra máy:

```bash
railway login
railway link                        # chọn "Trang Thu Chi"
railway run --service "Postgres ThuChi" python tools/agg.py
```

Script tạo `tools/_real_exp.json` và `tools/_real_bud.json`. Mọi file bắt đầu bằng `_` đã git-ignore, không bao giờ lên GitHub.

## Biến môi trường (đặt trên Railway)

| Tên | Dùng cho |
|---|---|
| `DATABASE_URL` | Postgres |
| `GROQ_API_KEY` | quét hoá đơn bằng AI |
| `ADMIN_KEY` | endpoint quản trị |

## Cấu trúc

- `app.py` API Flask, `init_db()` tạo bảng khi khởi động (xem `Dockerfile`).
- `index.html` toàn bộ giao diện và logic ngân sách, giới hạn, thưởng phạt.
- `sheet_sync.gs` Apps Script trong Google Sheet "Investment 2026", đẩy tình hình tài chính sang app mỗi sáng.
- `tools/` script phụ: mock server, kiểm tra cú pháp, đọc và sửa dữ liệu.
