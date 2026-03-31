import os
import re
import json
import queue
import base64
import hashlib
import secrets
import psycopg2
import requests as http_requests
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, send_file, Response
from datetime import date
from functools import wraps
app = Flask(__name__)

clients = []  # list of (user_code, queue) tuples

def notify_clients(user_code=None):
    dead = []
    for uc, q in clients:
        if user_code is None or uc == user_code:
            try:
                q.put_nowait("update")
            except:
                dead.append((uc, q))
    for item in dead:
        clients.remove(item)

def get_db():
    return psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=RealDictCursor)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            category TEXT NOT NULL,
            detail TEXT DEFAULT '',
            amount INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            user_code TEXT DEFAULT 'default'
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS budgets (
            month TEXT NOT NULL,
            amount INTEGER NOT NULL,
            user_code TEXT DEFAULT 'default',
            PRIMARY KEY (month, user_code)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_code TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            token TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    # Add columns if not exists (migration)
    try:
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS user_code TEXT DEFAULT 'default'")
        cur.execute("ALTER TABLE budgets DROP CONSTRAINT IF EXISTS budgets_pkey")
        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS user_code TEXT DEFAULT 'default'")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS token TEXT")
        cur.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'budgets_pkey_new') THEN
                    ALTER TABLE budgets ADD CONSTRAINT budgets_pkey_new PRIMARY KEY (month, user_code);
                END IF;
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$;
        """)
    except:
        pass
    conn.commit()
    cur.close()
    conn.close()

# ── Auth helpers ──

def generate_token():
    return secrets.token_hex(32)

def get_authenticated_user():
    """Verify token from Authorization header or query param, return user_code or None"""
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        token = auth[7:]
    else:
        token = request.args.get('token', '')
    if not token:
        return None
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_code FROM users WHERE token=%s", (token,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row['user_code'] if row else None

def require_auth(f):
    """Decorator: require valid token, inject user_code"""
    @wraps(f)
    def decorated(*args, **kwargs):
        user_code = get_authenticated_user()
        if not user_code:
            return jsonify({"error": "Unauthorized"}), 401
        request._user_code = user_code
        return f(*args, **kwargs)
    return decorated

def get_user_code():
    """Get user_code from authenticated request"""
    return getattr(request, '_user_code', 'default')

# ── AI Receipt Scanner ──

SCAN_PROMPT = """Bạn là trợ lý phân tích hóa đơn/chi tiêu. Hãy xem ảnh và trích xuất TẤT CẢ các khoản chi tiêu.

Nếu ảnh là 1 hóa đơn/bill duy nhất → trả về 1 khoản.
Nếu ảnh là sao kê ngân hàng, lịch sử giao dịch, hoặc có nhiều khoản riêng biệt → trả về NHIỀU khoản, mỗi giao dịch 1 khoản.

QUAN TRỌNG - PHÂN BIỆT TIỀN VÀO VÀ TIỀN RA:
- Chỉ lấy các khoản CHI (tiền ra/tiền trừ). BỎ QUA hoàn toàn các khoản THU (tiền vào/tiền cộng).
- Trong app ngân hàng: số tiền màu XANH LÁ/xanh dương thường là TIỀN VÀO (nhận tiền, hoàn tiền) → BỎ QUA.
- Số tiền màu ĐEN/ĐỎ hoặc có dấu trừ (-) thường là TIỀN RA (chi tiêu) → LẤY.
- Nếu có ký hiệu +/cộng trước số tiền → TIỀN VÀO → BỎ QUA.
- Nếu có ký hiệu -/trừ trước số tiền → TIỀN RA → LẤY.
- Nếu ghi "nhận tiền", "chuyển đến", "hoàn tiền", "tiền thưởng", "lương" → BỎ QUA.
- Nếu ghi "thanh toán", "chuyển tiền", "mua", "chi" → LẤY.

QUY TẮC PHÂN LOẠI (BẮT BUỘC tuân theo):
- MOCA, GrabFood, GrabMart, ShopeeFood, Baemin → food (ăn uống)
- Tên nhà hàng/quán ăn/cafe: Starbucks, Highland, Phúc Long, KFC, McDonald's, Jollibee, Pizza Hut, Lotteria, The Coffee House, Cộng Cà Phê, trà sữa, cơm, phở, bún, bánh mì... → food (ăn uống)
- Shopee, Lazada, Tiki, Sendo, TikTok Shop → shopping (mua sắm)
- Grab (đi xe), GrabBike, GrabCar, Be, Xanh SM, taxi, xe ôm → transport (di chuyển)
- Netflix, Spotify, YouTube Premium, game, rạp phim, CGV, Lotte Cinema → entertainment (giải trí)
- Tiền điện, nước, internet, điện thoại, thuê nhà → bills (hóa đơn)
- Bệnh viện, thuốc, khám, nha khoa → health (sức khỏe)
- Học phí, sách, khóa học, Udemy, Coursera → education (học tập)

Với mỗi khoản, xác định:
- "date": ngày giao dịch (format YYYY-MM-DD). Nếu không rõ năm thì dùng năm {year}. Nếu không rõ ngày thì dùng "{today}".
- "category": PHẢI là 1 trong: food, transport, shopping, entertainment, bills, health, education, other
- "detail": mô tả ngắn gọn bằng tiếng Việt (VD: "Grab đi làm", "Cà phê Highland", "Tiền điện tháng 3")
- "amount": số tiền GỐC trên hóa đơn (số nguyên, KHÔNG có dấu chấm/phẩy)
- "currency": đơn vị tiền tệ gốc. Nếu là VND/đồng thì ghi "VND". Nếu là USD/$ thì ghi "USD". Nếu là EUR/€ thì ghi "EUR". Mặc định "VND".

CHỈ trả về JSON array, KHÔNG có text nào khác:
[{{"date":"2026-03-29","category":"food","detail":"Cà phê Highland","amount":45000,"currency":"VND"}}]

Nếu không đọc được gì hữu ích, trả về: []"""

EXCHANGE_RATES = {
    'USD': 25500, 'EUR': 27500, 'GBP': 32000, 'JPY': 170,
    'KRW': 19, 'THB': 720, 'SGD': 19000, 'AUD': 16500, 'CNY': 3500,
}

def scan_with_groq(image_bytes, content_type):
    api_key = os.environ.get("GROQ_API_KEY", "")
    url = "https://api.groq.com/openai/v1/chat/completions"

    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    today_str = date.today().isoformat()
    year = date.today().year
    prompt = SCAN_PROMPT.format(today=today_str, year=year)

    payload = {
        "model": "meta-llama/llama-4-scout-17b-16e-instruct",
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{b64}"}},
            {"type": "text", "text": prompt}
        ]}],
        "temperature": 0.1, "max_tokens": 2000
    }

    resp = http_requests.post(url, json=payload, headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    text = data["choices"][0]["message"]["content"].strip()
    if text.startswith("```"):
        text = re.sub(r'^```\w*\n?', '', text)
        text = re.sub(r'\n?```$', '', text)

    items = json.loads(text)
    if not isinstance(items, list):
        items = [items]

    valid_cats = {'food','transport','shopping','entertainment','bills','health','education','other'}
    result = []
    for item in items:
        cat = item.get('category', 'other')
        if cat not in valid_cats:
            cat = 'other'
        amt = int(item.get('amount', 0))
        if amt <= 0:
            continue
        currency = item.get('currency', 'VND').upper().strip()
        detail = item.get('detail', '')[:80]
        entry = {
            'date': item.get('date', today_str),
            'category': cat,
            'detail': detail,
            'amount': amt,
        }
        if currency != 'VND' and currency in EXCHANGE_RATES:
            entry['original_amount'] = amt
            entry['original_currency'] = currency
            entry['exchange_rate'] = EXCHANGE_RATES[currency]
            entry['amount'] = int(amt * EXCHANGE_RATES[currency])
        result.append(entry)
    return result

# ── Public routes (no auth) ──

@app.route("/")
def index():
    return send_file("index.html")

@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.json
    user_code = (data.get("user_code") or "").strip()
    password = (data.get("password") or "").strip()
    if not user_code or not password:
        return jsonify({"error": "Vui lòng nhập mã và mật khẩu"}), 400
    if len(password) < 3:
        return jsonify({"error": "Mật khẩu ít nhất 3 ký tự"}), 400
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    token = generate_token()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_code FROM users WHERE user_code=%s", (user_code,))
    if cur.fetchone():
        cur.close()
        conn.close()
        return jsonify({"error": "Mã này đã được đăng ký"}), 409
    cur.execute("INSERT INTO users (user_code, password_hash, token) VALUES (%s, %s, %s)", (user_code, pw_hash, token))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True, "token": token, "user_code": user_code}), 201

@app.route("/api/login", methods=["POST"])
def login():
    data = request.json
    user_code = (data.get("user_code") or "").strip()
    password = (data.get("password") or "").strip()
    if not user_code or not password:
        return jsonify({"error": "Vui lòng nhập mã và mật khẩu"}), 400
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_code, token FROM users WHERE user_code=%s AND password_hash=%s", (user_code, pw_hash))
    user = cur.fetchone()
    if not user:
        cur.close()
        conn.close()
        return jsonify({"error": "Sai mã hoặc mật khẩu"}), 401
    # Reuse existing token so other devices stay logged in
    token = user.get('token')
    if not token:
        token = generate_token()
        cur.execute("UPDATE users SET token=%s WHERE user_code=%s", (token, user_code))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True, "token": token, "user_code": user_code})

@app.route("/api/verify", methods=["POST"])
def verify_token():
    """Verify if stored token is still valid"""
    user_code = get_authenticated_user()
    if not user_code:
        return jsonify({"ok": False}), 401
    return jsonify({"ok": True, "user_code": user_code})

@app.route("/api/reset-password", methods=["POST"])
def reset_password():
    data = request.json
    admin_key = (data.get("admin_key") or "").strip()
    if admin_key != os.environ.get("ADMIN_KEY", "thuchi-admin-2026"):
        return jsonify({"error": "Unauthorized"}), 403
    user_code = (data.get("user_code") or "").strip()
    new_password = (data.get("new_password") or "").strip()
    if not user_code or not new_password:
        return jsonify({"error": "Missing fields"}), 400
    pw_hash = hashlib.sha256(new_password.encode()).hexdigest()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET password_hash=%s WHERE user_code=%s", (pw_hash, user_code))
    updated = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True, "updated": updated})

# ── Protected routes (require auth) ──

@app.route("/api/scan", methods=["POST"])
@require_auth
def scan_receipt():
    if 'image' not in request.files:
        return jsonify({"error": "No image"}), 400
    file = request.files['image']
    image_bytes = file.read()
    content_type = file.content_type or 'image/jpeg'
    try:
        items = scan_with_groq(image_bytes, content_type)
        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"error": str(e), "items": []}), 500

@app.route("/api/events")
@require_auth
def events():
    user_code = get_user_code()
    def stream():
        q = queue.Queue()
        clients.append((user_code, q))
        try:
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            try:
                clients.remove((user_code, q))
            except:
                pass
    return Response(stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })

@app.route("/api/expenses")
@require_auth
def list_expenses():
    user_code = get_user_code()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, date, category, detail, amount FROM expenses WHERE user_code=%s ORDER BY date DESC, created_at DESC", (user_code,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(rows)

@app.route("/api/expenses", methods=["POST"])
@require_auth
def add_expense():
    data = request.json
    user_code = get_user_code()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO expenses (id, date, category, detail, amount, user_code) VALUES (%s, %s, %s, %s, %s, %s)",
        (data["id"], data["date"], data["category"], data.get("detail", ""), data["amount"], user_code)
    )
    conn.commit()
    cur.close()
    conn.close()
    notify_clients(user_code)
    return jsonify({"ok": True}), 201

@app.route("/api/expenses/bulk", methods=["POST"])
@require_auth
def add_expenses_bulk():
    items = request.json.get("items", [])
    if not items:
        return jsonify({"ok": False}), 400
    user_code = get_user_code()
    conn = get_db()
    cur = conn.cursor()
    for data in items:
        cur.execute(
            "INSERT INTO expenses (id, date, category, detail, amount, user_code) VALUES (%s, %s, %s, %s, %s, %s)",
            (data["id"], data["date"], data["category"], data.get("detail", ""), data["amount"], user_code)
        )
    conn.commit()
    cur.close()
    conn.close()
    notify_clients(user_code)
    return jsonify({"ok": True, "count": len(items)}), 201

@app.route("/api/expenses/<eid>", methods=["PUT"])
@require_auth
def update_expense(eid):
    data = request.json
    user_code = get_user_code()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE expenses SET date=%s, category=%s, detail=%s, amount=%s WHERE id=%s AND user_code=%s",
        (data["date"], data["category"], data.get("detail", ""), data["amount"], eid, user_code)
    )
    conn.commit()
    cur.close()
    conn.close()
    notify_clients(user_code)
    return jsonify({"ok": True})

@app.route("/api/expenses/<eid>", methods=["DELETE"])
@require_auth
def delete_expense(eid):
    user_code = get_user_code()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM expenses WHERE id=%s AND user_code=%s", (eid, user_code))
    conn.commit()
    cur.close()
    conn.close()
    notify_clients(user_code)
    return jsonify({"ok": True})

@app.route("/api/budgets")
@require_auth
def list_budgets():
    user_code = get_user_code()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT month, amount FROM budgets WHERE user_code=%s", (user_code,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify({r["month"]: r["amount"] for r in rows})

@app.route("/api/budgets", methods=["POST"])
@require_auth
def set_budget():
    data = request.json
    user_code = get_user_code()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO budgets (month, amount, user_code) VALUES (%s, %s, %s)
           ON CONFLICT (month, user_code) DO UPDATE SET amount=%s""",
        (data["month"], data["amount"], user_code, data["amount"])
    )
    conn.commit()
    cur.close()
    conn.close()
    notify_clients(user_code)
    return jsonify({"ok": True})

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
