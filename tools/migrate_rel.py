# Chuyển các khoản "Khác" mang tính quan hệ sang danh mục relationship + sửa vài khoản nhầm danh mục.
# Chạy thử (không đổi gì):  railway run --service "Postgres ThuChi" python tools/migrate_rel.py
# Áp dụng thật:              railway run --service "Postgres ThuChi" python tools/migrate_rel.py apply
import os, re, sys
sys.stdout.reconfigure(encoding='utf-8')
import psycopg2, psycopg2.extras

USER = 'trang01'
APPLY = len(sys.argv) > 1 and sys.argv[1] == 'apply'

REL = re.compile(r'quà|mừng|cưới|lì xì|li xi|thăm|sinh nhật|\bsn\b|biếu|hiếu|ủng hộ|từ thiện|cho mẹ|cho cậu|cho gấu|chuyển tiền cho|gender reveal|bóng đá cho|hoa quả về quê|bơ cho', re.I)
# Chuyển khoản cho một người (tên riêng 3-4 chữ viết hoa) trong "Khác" -> coi là quan hệ
NAME = re.compile(r'^[A-ZÀ-Ỹ][a-zà-ỹA-ZÀ-Ỹ]+(\s[A-ZÀ-Ỹ][a-zà-ỹA-ZÀ-Ỹ]+){2,4}(\s.*)?$')
# Khoản kinh doanh yến / không rõ -> giữ ở Khác
KEEP = re.compile(r'yến|yen|k nhớ|moca|iris|bottega|vốn', re.I)

# Sửa nhầm danh mục cụ thể (khớp theo detail, không phân biệt hoa thường)
FIXES = [
    (r'chatgpt', 'education', 'bills'),
]

url = os.environ.get('DATABASE_PUBLIC_URL') or os.environ.get('DATABASE_URL')
conn = psycopg2.connect(url)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("SELECT id, date::text, category, detail, amount FROM expenses WHERE user_code=%s ORDER BY date", (USER,))
rows = cur.fetchall()

changes = []  # (id, old, new, date, amount, detail)
for r in rows:
    d = (r['detail'] or '').strip()
    for rx, frm, to in FIXES:
        if r['category'] == frm and re.search(rx, d, re.I):
            changes.append((r['id'], frm, to, r['date'], r['amount'], d))
            break

# Trang thai hien tai de doi chieu: khoan con o Khac nhung KHONG bi chuyen, va khoan nghi nham
ids_changed = {c[0] for c in changes}
print("-- Con o Khac, giu nguyen --")
for r in rows:
    if r['category'] == 'other' and r['id'] not in ids_changed:
        print(f"   {r['date']}  {r['amount']/1e6:6.2f}M  {(r['detail'] or '')[:50]}")
print("-- Danh muc hien tai cua cac khoan tung nghi nham --")
for r in rows:
    d = (r['detail'] or '')
    if re.search(r'vinmec|paula|kcn|chatgpt|tin d|babi|xi o que|cuoi|cưới|lì xì', d, re.I):
        print(f"   {r['date']}  {r['category']:12s} {r['amount']/1e6:6.2f}M  {d[:50]}")
print()
print(f"{'ÁP DỤNG' if APPLY else 'CHẠY THỬ'}: {len(changes)} khoản sẽ đổi danh mục\n")
for _, old, new, date, amt, d in changes:
    print(f"{date}  {old:9s} -> {new:12s} {amt/1e6:6.2f}M  {d[:50]}")
tot = sum(c[4] for c in changes if c[2] == 'relationship')
print(f"\nTổng chuyển sang Quan hệ: {tot/1e6:.2f}M")

if APPLY and changes:
    for id_, old, new, *_ in changes:
        cur.execute("UPDATE expenses SET category=%s WHERE id=%s AND user_code=%s AND category=%s", (new, id_, USER, old))
    conn.commit()
    print(f"\nĐã cập nhật {len(changes)} khoản. Mở lại app để thấy thay đổi.")
elif not APPLY:
    print("\nChưa đổi gì. Thêm chữ  apply  vào cuối lệnh để áp dụng.")
cur.close(); conn.close()
