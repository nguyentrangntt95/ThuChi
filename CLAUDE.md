# Sổ Thu Chi — hướng dẫn làm việc cho Claude Code

Đây là app quản lý chi tiêu cá nhân THẬT của Trang, đang chạy production. Mọi thứ cần để làm việc tiếp nằm trong repo này; không phụ thuộc máy nào.

## Hạ tầng
- **Repo:** GitHub `nguyentrangntt95/ThuChi`, nhánh `main`.
- **Production:** Railway, project "Trang Thu Chi", services `ThuChi` (web) + `Postgres ThuChi`. URL: https://thuchi-production-1d0c.up.railway.app
- **Deploy:** `git push origin main` → Railway tự build và deploy trong vài chục giây. Không có bước thủ công.
- **Biến môi trường** (đặt trên Railway, không có trong repo): `DATABASE_URL`, `GROQ_API_KEY` (quét hoá đơn AI), `ADMIN_KEY`.
- Có một bản biến thể riêng cho mẹ Trang: repo `Mom-ThuChi` (không đụng tới khi làm app này).

## Cấu trúc
- `app.py`: Flask API + psycopg2. `init_db()` tạo bảng. `/api/settings` POST merge **một cấp**: giá trị dict được gộp nông vào dict cũ.
- `index.html`: TOÀN BỘ frontend trong một file (~4k dòng), Preact + htm qua esm.sh, không build step.
  - CSS là tập utility **viết tay giống Tailwind**, KHÔNG phải Tailwind CDN. Chỉ class có trong `<style>` mới hoạt động. Layout lạ thì dùng inline style.
  - Khối theme 2026 nằm cuối `<style>` (font Inter, dark xanh đen, nút gradient). Dark mode: class `dark` + inline style theo biến `dark`.
- `sheet_sync.gs`: Apps Script gắn trong Google Sheet "Investment 2026", 6h sáng POST tình hình tài chính vào `/api/settings`.
- `tools/`: script phụ (xem dưới).
- `habit-reminder.html`: trang phụ cũ, ít dùng.

## Quy trình bắt buộc khi sửa code
1. Sửa `index.html` / `app.py`. Với patch lớn vào `index.html`, viết script Python (dùng Write tool, không heredoc bash vì hỏng escape) thay chuỗi có `assert count == 1`.
2. `python tools/check_syntax.py` — một lỗi cú pháp JS làm trắng cả app.
3. Xem thử trên mock: `python tools/mock_server.py` rồi mở http://127.0.0.1:8765 (xem docstring để đăng nhập giả). KHÔNG đăng nhập production để test.
4. Commit bằng file message: `git commit -F msg.txt` (message nhiều dòng qua `-m` trên PowerShell bị hỏng). Message tiếng Việt, kết thúc bằng dòng `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
5. `git push origin main` ngay, **không hỏi lại** Trang có muốn deploy không. Sửa xong là deploy.
6. **BẮT BUỘC sau mỗi lần thêm / đổi tính năng hoặc đổi logic: cập nhật `CLAUDE.md` trong cùng commit.** Thêm vào mục "Logic nghiệp vụ cần nhớ" (tên hàm, hằng số, quy tắc), sửa các dòng đã lỗi thời, và ghi một dòng vào "Nhật ký tính năng" cuối file (ngày, commit, tóm tắt). Trang không nhớ hết các thay đổi; file này là trí nhớ duy nhất của project giữa các phiên AI. Chưa cập nhật CLAUDE.md thì chưa được coi là xong việc.

## Dữ liệu thật
- Tài khoản của Trang: `user_code = trang01`. Dữ liệu chỉ nằm trong Postgres trên Railway.
- Claude không tự chạy được lệnh đọc production. Nhờ Trang chạy trong terminal (tiền tố `!`):
  `! railway run --service "Postgres ThuChi" python tools/agg.py`
  Script in bảng tháng × danh mục và lưu `tools/_real_exp.json`, `tools/_real_bud.json` (đã git-ignore).
- Máy mới cần: `railway login` (tài khoản nguyentrangntt95) rồi `railway link` chọn project "Trang Thu Chi". Phải chọn service **Postgres** vì chỉ nó có `DATABASE_PUBLIC_URL`.
- `tools/migrate_rel.py`: ví dụ script sửa dữ liệu hàng loạt, chạy thử trước, thêm `apply` mới ghi.

## Logic nghiệp vụ cần nhớ (hằng số ở đầu các khối trong index.html)
- Danh mục: food, transport, shopping, entertainment, bills, health, education, beauty, relationship, other, savings. Tiết kiệm (`SAVINGS_CAT`) không tính là chi tiêu.
- Ngân sách tháng = lương − mục tiêu tiết kiệm (`budgets[m]`). Tháng chưa có bản ghi: `estimateBudget` = trung bình của (chi cố định) và (TB 3 tháng gần nhất) theo từng mục, không thấp hơn sàn; nhãn "ước tính". Mặc định cuối cùng 15M.
- Giới hạn theo mục: `cat_limits` (% ngân sách) + `cat_limits._min` (sàn tiền tuyệt đối, mặc định health 3.5M vì yoga). `baseLimitOf = max(%×ngân sách, sàn)`.
- Phạt vượt (shopping/beauty, từ 2026-10, `PENALTY_START`): theo bậc 20/30/40/50% phần vượt, tối thiểu 100k, KHÔNG trần (Trang yêu cầu), phần vượt trừ vào hạn mức tháng sau (`carryInto`).
- Điểm check-in đổi được: `REWARD_QUOTA` 1000đ → +200k hạn mức, `REWARD_PATCH` 300đ → vá ngày thiếu, `REWARD_FINE` 500đ → giảm 10% phạt tháng trước (tối đa 30%).
- Hũ thưởng phạt (`jar`), thử thách 3 ngày (`CHALLENGE`), streak tuần (`weekGoalInfo`).
- Chúc mừng tháng tiêu ít nhất: `monthStanding` so cùng mốc ngày với các tháng trước (≥8 khoản, cần ≥2 tháng trước), modal `CheerModal` hiện một lần (localStorage `tc-cheer-*`).

## Cách Trang muốn làm việc
- Trả lời tiếng Việt, ngắn, thân mật. Trang thường gõ nhanh không dấu; đoán ý rồi làm, chỉ hỏi khi thật sự mơ hồ.
- Yêu cầu "chỉ đẹp hơn" nghĩa là giữ nguyên tính năng 100%.
- Sửa xong là deploy, không hỏi. Khi xong, tóm tắt ngắn những gì đã lên.
- Mỗi lần xong một tính năng: cập nhật `CLAUDE.md` (xem bước 6) trước khi báo xong.

## Nhật ký tính năng
Mỗi lần thêm/đổi tính năng ghi một dòng: `YYYY-MM-DD · commit · tóm tắt`. Mới nhất ở trên.

- 2026-10-30 · 9617b95 · Đưa tools/, CLAUDE.md, README lên repo; project không còn phụ thuộc máy local.
- 2026-10-30 · 33eec9e · Chúc mừng tháng tiêu ít nhất (`monthStanding`, `CheerModal`, huy hiệu 🏆).
- 2026-10-26 · 92a6d39 · Ngân sách ước tính cho tháng chưa đặt (`estimateBudget`), sàn theo mục (`_min`, health 3.5M), hũ gọn một dòng.
- 2026-10-26 · 464876e · Biểu đồ ngày: nhãn chỉ số ngày.
- 2026-10-26 · 920ab51 · Giao diện mới: Inter, dark xanh đen, nút gradient (không đổi tính năng).
