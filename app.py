import os
import re
import json
import time
import queue
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, send_file, Response
from PIL import Image
import pytesseract
import io

app = Flask(__name__)

# SSE: list of queues, one per connected client
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

# ── OCR helpers ──

CAT_KEYWORDS = {
    'food': ['cafe','coffee','cà phê','trà','tea','cơm','phở','bún','mì','bánh','ăn','nhà hàng','quán','food','grab food','shopeefood','gà','bò','heo','cá','tôm','lẩu','nước','sinh tố','kem','pizza','burger','sushi','beer','bia','rượu','ăn sáng','ăn trưa','ăn tối','highland','starbucks','cheese','xôi','hủ tiếu','cháo','kfc','lotteria','jollibee','gong cha','tocotoco','phúc long','the coffee house','circle k','ministop','gs25','family mart','7-eleven','bách hóa xanh'],
    'transport': ['grab','taxi','xăng','gojek','be','uber','parking','đỗ xe','gửi xe','vé xe','xe buýt','bus','toll','phí cầu','sân bay','vé máy bay','tàu','metro'],
    'shopping': ['shopee','lazada','tiki','sendo','siêu thị','vinmart','coopmart','big c','lotte mart','aeon','quần','áo','giày','dép','túi','mỹ phẩm','đồ gia dụng','điện thoại','laptop','máy tính','tai nghe'],
    'entertainment': ['phim','cinema','cgv','game','karaoke','concert','du lịch','khách sạn','hotel','resort','spa','massage','netflix','spotify'],
    'bills': ['điện','nước','internet','wifi','thuê nhà','tiền nhà','bảo hiểm','thuế','trả góp','vay','credit','ngân hàng','gas'],
    'health': ['thuốc','bệnh viện','khám','pharmacy','nhà thuốc','bác sĩ','y tế','răng','mắt','vitamin','gym','yoga','fitness'],
    'education': ['sách','học','course','khóa học','udemy','coursera','học phí','trường','lớp','gia sư'],
}

BRANDS = ['highland','starbucks','phúc long','the coffee house','tocotoco','gong cha','kfc','lotteria','jollibee','mcdonalds','grab','shopee','lazada','tiki','circle k','ministop','gs25','family mart','7-eleven','vinmart','coopmart','big c','lotte','aeon','cgv','pizza','bách hóa xanh','pharmacity','long châu','an khang']

def classify_category(text):
    lower = text.lower()
    best, best_count = 'other', 0
    for cat, keywords in CAT_KEYWORDS.items():
        count = sum(1 for kw in keywords if kw in lower)
        if count > best_count:
            best_count = count
            best = cat
    return best

def extract_amount(text):
    patterns = [
        r'(?:tổng|total|thành tiền|thanh toán|tạm tính|amount|t\.toán)[\s:=]*(\d{1,3}(?:[.,]\d{3})+)',
        r'(\d{1,3}(?:[.,]\d{3})+)\s*(?:đ|vnd|vnđ|dong)',
        r'(\d{1,3}(?:[.,]\d{3})+)',
    ]
    for pat in patterns:
        matches = re.findall(pat, text, re.IGNORECASE)
        amounts = []
        for m in matches:
            num = int(m.replace('.', '').replace(',', ''))
            if 1000 <= num <= 999999999:
                amounts.append(num)
        if amounts:
            return max(amounts)
    return 0

def extract_detail(text):
    lower = text.lower()
    for b in BRANDS:
        if b in lower:
            return b.title()
    lines = [l.strip() for l in text.split('\n') if 4 <= len(l.strip()) <= 60]
    junk = re.compile(r'^[\d.,\s%:\/\-\(\)]+$|^\d{1,2}[\/\-]|^(tel|phone|đt|sđt|hotline|fax|email|www|http|địa chỉ|add|tax|mã|no\.|bill|order|inv|receipt|hóa đơn|-----)', re.IGNORECASE)
    good = [l for l in lines if not junk.match(l)]
    return good[0][:60] if good else ''

@app.route("/api/scan", methods=["POST"])
def scan_receipt():
    if 'image' not in request.files:
        return jsonify({"error": "No image"}), 400
    file = request.files['image']
    img = Image.open(io.BytesIO(file.read()))
    # Resize for speed: max 1200px wide
    if img.width > 1200:
        ratio = 1200 / img.width
        img = img.resize((1200, int(img.height * ratio)))
    # Convert to grayscale for better OCR
    img = img.convert('L')
    text = pytesseract.image_to_string(img, lang='vie+eng', config='--psm 6')
    amount = extract_amount(text)
    category = classify_category(text)
    detail = extract_detail(text)
    return jsonify({"amount": amount, "category": category, "detail": detail, "rawText": text})

# SSE endpoint
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

# Serve frontend
@app.route("/")
def index():
    return send_file("index.html")

# ── Expenses API ──

@app.route("/api/expenses")
def list_expenses():
    month = request.args.get("month")
    conn = get_db()
    cur = conn.cursor()
    if month:
        cur.execute("SELECT * FROM expenses WHERE substring(date,1,7) = %s ORDER BY date DESC, created_at DESC", (month,))
    else:
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
