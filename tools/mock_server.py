"""Mock backend để xem/test giao diện mà KHÔNG đụng dữ liệu thật.

Chạy:  python tools/mock_server.py
Mở:    http://127.0.0.1:8765  (dữ liệu giả, sinh quanh ngày hôm nay)

Đăng nhập: app đọc token từ localStorage. Trong DevTools console gõ:
  localStorage['tc-token'] = JSON.stringify('mock'); localStorage['tc-user'] = JSON.stringify('mock'); location.reload()
Chế độ tối: localStorage['tc-dark'] = 'true'
"""
import json, io, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import date, timedelta

INDEX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'index.html')
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
today = date.today()
d = lambda n: (today + timedelta(days=n)).isoformat()
expenses = []
_id = [0]
def add(day, amount, cat, detail):
    _id[0] += 1
    expenses.append({"id": "e%d" % _id[0], "date": day, "amount": amount, "category": cat, "detail": detail, "project_id": ""})
for i in range(100, -1, -1):
    day = d(-i)
    wd = (today - timedelta(days=i)).weekday()
    add(day, 65000 + (i * 7919) % 60000, "food", "An trua")
    if wd in (1, 4): add(day, 30000, "transport", "Grab")
    if wd == 6: add(day, 250000 + (i * 1237) % 200000, "shopping", "Shopee")
    if i % 9 == 0: add(day, 180000, "beauty", "Mua my pham")
    if i % 11 == 0: add(day, 120000, "entertainment", "Xem phim")
    dd = (today - timedelta(days=i)).day
    if dd == 20: add(day, 3500000, "bills", "Tien nha")
    if dd == 5: add(day, 260000, "entertainment", "Netflix")
    if dd == 8: add(day, 400000, "health", "Yoga")
    if i == 25: add(day, 5000000, "savings", "Gui tiet kiem")
cur_m = today.strftime("%Y-%m")
pm = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
add(cur_m + "-01", 90000, "savings", "Phat vuot Mua sam T%d (lo 500k)" % int(pm[5:]))
budgets = {cur_m: {"amount": 0, "income": 27100000, "savings_target": 12000000}, pm: {"amount": 0, "income": 27100000, "savings_target": 12000000}}
settings = {
    "cat_limits": {"food": 25, "transport": 5, "shopping": 8, "entertainment": 4, "bills": 25, "health": 5, "education": 5, "beauty": 5, "relationship": 6, "other": 2},
    "penalties": {pm: {"shopping": {"over": 500000, "fine": 90000, "fine0": 100000, "disc": 10, "carry": 500000, "expense_id": "e%d" % _id[0]}, "beauty": {"under": 200000, "bonus": 100000}}},
    "rewards": {"log": [
        {"id": "r1", "day": d(-3), "type": "fine", "pts": 500, "month": pm, "pct": 10, "amount": 10000},
        {"id": "r2", "day": d(-2), "type": "quota", "pts": 1000, "cat": "beauty", "month": cur_m, "amount": 200000},
        {"id": "r3", "day": d(-1), "type": "weekwin", "pts": 0, "week": d(-8), "to": d(-2), "saved": 480000, "amount": 50000, "streak": 2, "cat": None, "month": None},
    ]},
    "jar": {"name": "Chuyen di Da Lat", "emoji": "✈️", "target": 3000000},
    "challenge": None,
    "finance": {},
}
checkins = [{"day": d(-i), "points": 30 + (i % 4) * 5, "note": ""} for i in range(0, 12)]

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            body = io.open(INDEX, encoding="utf-8").read().encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        if p == "/api/expenses": return self._json(expenses)
        if p == "/api/budgets": return self._json(budgets)
        if p == "/api/category-patterns": return self._json({})
        if p == "/api/projects": return self._json([])
        if p == "/api/checkins": return self._json(checkins)
        if p == "/api/settings": return self._json(settings)
        if p == "/api/events": return self._json({"error": "no sse"}, 404)
        self._json({"error": "nf"}, 404)
    def do_POST(self):
        p = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if p == "/api/login": return self._json({"ok": True, "token": "mock", "user_code": "mock"})
        if p == "/api/verify": return self._json({"ok": True, "user_code": "mock"})
        if p == "/api/checkins":
            if any(c["day"] == data.get("day") for c in checkins): return self._json({"ok": True, "created": False})
            checkins.append({"day": data["day"], "points": data.get("points", 0), "note": data.get("note", "")}); return self._json({"ok": True, "created": True})
        if p == "/api/settings":
            for k, v in data.items():
                if isinstance(v, dict) and isinstance(settings.get(k), dict): settings[k] = {**settings[k], **v}
                else: settings[k] = v
            return self._json(settings)
        if p == "/api/expenses":
            expenses.append(data); return self._json({"ok": True})
        if p == "/api/expenses/bulk":
            expenses.extend(data.get("items", [])); return self._json({"ok": True})
        return self._json({"ok": True})
    def do_PUT(self): self._json({"ok": True})
    def do_DELETE(self): self._json({"ok": True})

print("mock on http://127.0.0.1:%d" % PORT); sys.stdout.flush()
ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
