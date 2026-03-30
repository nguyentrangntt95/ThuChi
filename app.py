import os
import re
import json
import time
import queue
import base64
import psycopg2
import requests as http_requests
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, send_file, Response
from datetime import date

app = Flask(__name__)

clients = []

def notify_clients():
    dead = []
    for q in clients:
        try:
            q.put_nowait("update")
        except:
            dead.append(q)
    for q in dead:
        clients.remove(q)

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
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS budgets (
            month TEXT PRIMARY KEY,
            amount INTEGER NOT NULL
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

# ── AI Receipt Scanner (Google Gemini) ──

SCAN_PROMPT = """Bạn là trợ lý phân tích hóa đơn/chi tiêu. Hãy xem ảnh và trích xuất TẤT CẢ các khoản chi tiêu.

Nếu ảnh là 1 hóa đơn/bill duy nhất → trả về 1 khoản.
Nếu ảnh là sao kê ngân hàng, lịch sử giao dịch, hoặc có nhiều khoản riêng biệt → trả về NHIỀU khoản, mỗi giao dịch 1 khoản.

Chỉ lấy các khoản CHI (tiền ra), bỏ qua các khoản thu (tiền vào).

Với mỗi khoản, xác định:
- "date": ngày giao dịch (format YYYY-MM-DD). Nếu không rõ năm thì dùng năm {year}. Nếu không rõ ngày thì dùng "{today}".
- "category": PHẢI là 1 trong: food, transport, shopping, entertainment, bills, health, education, other
- "detail": mô tả ngắn gọn bằng tiếng Việt (VD: "Grab đi làm", "Cà phê Highland", "Tiền điện tháng 3")
- "amount": số tiền (số nguyên, đơn vị VND, KHÔNG có dấu chấm/phẩy)

CHỈ trả về JSON array, KHÔNG có text nào khác:
[{{"date":"2026-03-29","category":"food","detail":"Cà phê Highland","amount":45000}}]

Nếu không đọc được gì hữu ích, trả về: []"""

def scan_with_gemini(image_bytes, content_type):
    api_key = os.environ.get("GEMINI_API_KEY", "")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"

    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    today_str = date.today().isoformat()
    year = date.today().year
    prompt = SCAN_PROMPT.format(today=today_str, year=year)

    payload = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": content_type, "data": b64}},
                {"text": prompt}
            ]
        }],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2000}
    }

    resp = http_requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    # Extract JSON
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
        result.append({
            'date': item.get('date', today_str),
            'category': cat,
            'detail': item.get('detail', '')[:80],
            'amount': amt,
        })
    return result

@app.route("/api/scan", methods=["POST"])
def scan_receipt():
    if 'image' not in request.files:
        return jsonify({"error": "No image"}), 400
    file = request.files['image']
    image_bytes = file.read()
    content_type = file.content_type or 'image/jpeg'
    try:
        items = scan_with_gemini(image_bytes, content_type)
        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"error": str(e), "items": []}), 500

# SSE
@app.route("/api/events")
def events():
    def stream():
        q = queue.Queue()
        clients.append(q)
        try:
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            clients.remove(q)
    return Response(stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })

@app.route("/")
def index():
    return send_file("index.html")

# ── Expenses API ──

@app.route("/api/expenses")
def list_expenses():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM expenses ORDER BY date DESC, created_at DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(rows)

@app.route("/api/expenses", methods=["POST"])
def add_expense():
    data = request.json
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO expenses (id, date, category, detail, amount) VALUES (%s, %s, %s, %s, %s)",
        (data["id"], data["date"], data["category"], data.get("detail", ""), data["amount"])
    )
    conn.commit()
    cur.close()
    conn.close()
    notify_clients()
    return jsonify({"ok": True}), 201

@app.route("/api/expenses/bulk", methods=["POST"])
def add_expenses_bulk():
    items = request.json.get("items", [])
    if not items:
        return jsonify({"ok": False}), 400
    conn = get_db()
    cur = conn.cursor()
    for data in items:
        cur.execute(
            "INSERT INTO expenses (id, date, category, detail, amount) VALUES (%s, %s, %s, %s, %s)",
            (data["id"], data["date"], data["category"], data.get("detail", ""), data["amount"])
        )
    conn.commit()
    cur.close()
    conn.close()
    notify_clients()
    return jsonify({"ok": True, "count": len(items)}), 201

@app.route("/api/expenses/<eid>", methods=["PUT"])
def update_expense(eid):
    data = request.json
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE expenses SET date=%s, category=%s, detail=%s, amount=%s WHERE id=%s",
        (data["date"], data["category"], data.get("detail", ""), data["amount"], eid)
    )
    conn.commit()
    cur.close()
    conn.close()
    notify_clients()
    return jsonify({"ok": True})

@app.route("/api/expenses/<eid>", methods=["DELETE"])
def delete_expense(eid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM expenses WHERE id=%s", (eid,))
    conn.commit()
    cur.close()
    conn.close()
    notify_clients()
    return jsonify({"ok": True})

# ── Budgets API ──

@app.route("/api/budgets")
def list_budgets():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM budgets")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify({r["month"]: r["amount"] for r in rows})

@app.route("/api/budgets", methods=["POST"])
def set_budget():
    data = request.json
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO budgets (month, amount) VALUES (%s, %s) ON CONFLICT (month) DO UPDATE SET amount=%s",
        (data["month"], data["amount"], data["amount"])
    )
    conn.commit()
    cur.close()
    conn.close()
    notify_clients()
    return jsonify({"ok": True})

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
