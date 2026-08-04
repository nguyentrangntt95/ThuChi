import os
import io
import re
import json
import time
import queue
import base64
import hashlib
import secrets
import psycopg2
import requests as http_requests
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, send_file, Response
from datetime import date, timedelta, datetime, timezone
from functools import wraps

try:
    from PIL import Image, ImageOps
    try:
        import pillow_heif  # iPhone HEIC/HEIF
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
except ImportError:
    Image = None

app = Flask(__name__)
# Reject absurd uploads before reading them into memory
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
VN_TZ = timezone(timedelta(hours=7))
def vn_today():
    return datetime.now(VN_TZ).date()

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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            emoji TEXT DEFAULT '',
            user_code TEXT DEFAULT 'default',
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    # Add columns if not exists (migration)
    try:
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS user_code TEXT DEFAULT 'default'")
        cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS project_id TEXT DEFAULT ''")
        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS income INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE budgets ADD COLUMN IF NOT EXISTS savings_target INTEGER DEFAULT 0")
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

# ── AI Receipt Scanner (3-step pipeline) ──

# llama-4-scout was decommissioned by Groq on 2026-07-17
VISION_MODEL = "qwen/qwen3.6-27b"
TEXT_MODEL = "openai/gpt-oss-120b"

# Step 1: Pure extraction - OCR text from image
# Kept deliberately short: every token here is charged against the 8K/min budget
# on each scan, which directly caps how many images can be read per minute.
EXTRACT_PROMPT = """Đọc ảnh hóa đơn/sao kê, trích xuất các khoản CHI thành JSON.

HÔM NAY={today}, HÔM QUA={yesterday}, năm {year}.

CHỈ LẤY TIỀN RA, BỎ QUA TIỀN VÀO:
- BỎ nếu: có dấu +, số màu xanh, hoặc ghi "nhận tiền", "chuyển đến", "hoàn tiền", "tiền thưởng", "lương".
- LẤY nếu: có dấu -, số màu đen/đỏ, hoặc ghi "thanh toán", "chuyển tiền", "mua", "chi".

NGÀY (bắt buộc): "hôm nay"/"today"/"hn"→{today}. "hôm qua"/"yesterday"/"hq"→{yesterday}. Ngày cụ thể (VD 9/4)→dùng ngày đó, năm {year}. Không có ngày→{today}. Không được nhầm hôm nay với hôm qua.

1 hóa đơn → 1 khoản; nếu có dòng TỔNG CỘNG thì lấy tổng, không tách từng món.
Sao kê/lịch sử giao dịch → mỗi giao dịch 1 khoản.

Mỗi khoản gồm:
- "date": YYYY-MM-DD
- "detail": tên gốc trên ảnh, giữ nguyên tên cửa hàng (VD "Grab đi làm", "Tiền điện tháng 3")
- "amount": số nguyên, KHÔNG dấu chấm/phẩy
- "currency": "VND" mặc định, "USD" nếu là $

Chỉ trả JSON array, không giải thích:
[{{"date":"2026-03-29","detail":"Cà phê Highland","amount":45000,"currency":"VND"}}]
Không đọc được gì → []"""

# Step 2: Categorize extracted items using AI + user history
CATEGORIZE_PROMPT = """Bạn là trợ lý phân loại chi tiêu. Hãy phân loại từng khoản chi tiêu dưới đây vào đúng category.

CÁC CATEGORY HỢP LỆ: food, transport, shopping, entertainment, bills, health, education, beauty, savings, other

QUY TẮC PHÂN LOẠI:
- MOCA, GrabFood, GrabMart, ShopeeFood, Baemin, tên nhà hàng/quán ăn/cafe (Starbucks, Highland, Phúc Long, KFC, McDonald's, Jollibee, Pizza Hut, Lotteria, The Coffee House, Cộng Cà Phê, trà sữa, cơm, phở, bún, bánh mì...) → food
- Shopee, Lazada, Tiki, Sendo, TikTok Shop → shopping
- Grab (đi xe), GrabBike, GrabCar, Be, Xanh SM, taxi, xe ôm → transport
- Netflix, Spotify, YouTube Premium, game, rạp phim, CGV, Lotte Cinema → entertainment
- Tiền điện, nước, internet, điện thoại, thuê nhà → bills
- Bệnh viện, thuốc, khám, nha khoa → health
- Học phí, sách, khóa học, Udemy, Coursera → education
- Mỹ phẩm, skincare, spa, làm tóc, làm nail, thẩm mỹ → beauty
- Gửi tiết kiệm, đầu tư, tích lũy, để dành → savings
- Không rõ → other

{history_rules}

DANH SÁCH CẦN PHÂN LOẠI:
{items_text}

Trả về JSON array với category được thêm vào mỗi khoản. CHỈ trả về JSON, KHÔNG text khác:
[{{"index":0,"category":"food"}},{{"index":1,"category":"transport"}}]"""

EXCHANGE_RATES = {
    'USD': 25500, 'EUR': 27500, 'GBP': 32000, 'JPY': 170,
    'KRW': 19, 'THB': 720, 'SGD': 19000, 'AUD': 16500, 'CNY': 3500,
}

def get_user_category_patterns(user_code):
    """Get user's most common detail→category mappings from their expense history"""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT detail, category, COUNT(*) as cnt
            FROM expenses
            WHERE user_code=%s AND detail != '' AND category != 'other'
            GROUP BY detail, category
            ORDER BY cnt DESC
            LIMIT 50
        """, (user_code,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        patterns = {}
        for r in rows:
            d = r['detail'].strip()
            if d and d not in patterns:
                patterns[d] = r['category']
        return patterns
    except:
        return {}

def find_potential_duplicates(user_code, items):
    """Step 3: Find potential duplicates by same amount within ±3 days"""
    try:
        conn = get_db()
        cur = conn.cursor()
        dates = list(set(it['date'] for it in items))
        if not dates:
            return []
        min_date = (date.fromisoformat(min(dates)) - timedelta(days=3)).isoformat()
        max_date = (date.fromisoformat(max(dates)) + timedelta(days=3)).isoformat()
        cur.execute("""
            SELECT id, date, detail, amount FROM expenses
            WHERE user_code=%s AND date >= %s AND date <= %s
        """, [user_code, min_date, max_date])
        existing = cur.fetchall()
        cur.close()
        conn.close()

        duplicates = []
        for i, item in enumerate(items):
            item_date = date.fromisoformat(item['date'])
            for ex in existing:
                ex_date = date.fromisoformat(ex['date'])
                days_diff = abs((item_date - ex_date).days)
                if days_diff <= 3 and ex['amount'] == item['amount']:
                    duplicates.append({
                        'scan_index': i,
                        'existing_id': ex['id'],
                        'existing_detail': ex['detail'],
                        'existing_date': ex['date'],
                        'match_reason': 'same_amount_nearby'
                    })
                    break
        return duplicates
    except:
        return []

MAX_IMAGE_BYTES = 700_000

# Groq counts prompt + image + max_tokens against the 8K/min budget, so a generous
# max_tokens directly costs throughput: at ~4000 tokens a scan only 2 fit per minute.
# A single receipt replies in well under 700 tokens; long statements are handled by
# retrying at EXTRACT_RETRY_LARGE only when the reply actually comes back truncated.
EXTRACT_ATTEMPTS = ((1024, 700), (768, 700), (640, 700))
EXTRACT_RETRY_LARGE = 2500


def _looks_truncated(text):
    t = (text or "").rstrip()
    return bool(t) and not t.endswith("]") and not t.endswith("}")


# Mirrors the rules in CATEGORIZE_PROMPT. Order matters: GrabFood must win over Grab.
KEYWORD_CATEGORIES = (
    ('food', ('grabfood', 'grabmart', 'shopeefood', 'baemin', 'gojek food', 'moca',
              'starbucks', 'highland', 'phúc long', 'phuc long', 'the coffee house',
              'cộng cà phê', 'kfc', 'mcdonald', 'jollibee', 'pizza', 'lotteria',
              'burger', 'texas chicken', 'popeyes', 'cà phê', 'ca phe', 'cafe',
              'coffee', 'trà sữa', 'tra sua', 'nhà hàng', 'quán ăn', 'cơm ', 'phở',
              'bún ', 'bánh mì', 'circle k', 'bakery', 'kichi', 'gogi', 'lotte mart',
              'vinmart', 'winmart', 'bách hóa xanh')),
    ('transport', ('grabbike', 'grabcar', 'grab', 'xanh sm', 'be group', 'taxi',
                   'mai linh', 'vinasun', 'xe ôm', 'vé xe', 'vé tàu', 'vé máy bay',
                   'vietjet', 'bamboo airways', 'vietnam airlines', 'xăng', 'petrolimex',
                   'gửi xe', 'giữ xe', 'vetc', 'epass')),
    ('shopping', ('shopee', 'lazada', 'tiki', 'sendo', 'tiktok shop', 'uniqlo',
                  'zara', 'h&m', 'nike', 'adidas', 'điện máy', 'thế giới di động',
                  'fpt shop')),
    ('entertainment', ('netflix', 'spotify', 'youtube premium', 'cgv', 'lotte cinema',
                       'bhd star', 'galaxy cinema', 'steam', 'playstation', 'game',
                       'rạp phim', 'karaoke')),
    ('bills', ('tiền điện', 'tien dien', 'evn', 'tiền nước', 'tien nuoc', 'internet',
               'fpt telecom', 'viettel', 'vinaphone', 'mobifone', 'thuê nhà',
               'thue nha', 'tiền nhà', 'phí quản lý', 'cước')),
    ('health', ('bệnh viện', 'benh vien', 'phòng khám', 'nhà thuốc', 'pharmacity',
                'long châu', 'guardian', 'nha khoa', 'thuốc', 'vaccine', 'xét nghiệm')),
    ('education', ('học phí', 'hoc phi', 'udemy', 'coursera', 'khóa học', 'khoa hoc',
                   'nhà sách', 'fahasa', 'sách', 'trung tâm anh ngữ', 'ielts')),
    ('beauty', ('spa', 'skincare', 'mỹ phẩm', 'my pham', 'làm tóc', 'lam toc', 'salon',
                'nail', 'thẩm mỹ', 'cocoon', 'the face shop', 'innisfree')),
    ('savings', ('tiết kiệm', 'tiet kiem', 'đầu tư', 'dau tu', 'tích lũy', 'để dành',
                 'chứng khoán', 'vàng sjc')),
)


def _local_category(detail):
    """Category from merchant name alone, or None if the AI should decide."""
    d = (detail or '').lower()
    if not d:
        return None
    for cat, keywords in KEYWORD_CATEGORIES:
        for kw in keywords:
            if kw in d:
                return cat
    return None


def _prepare_image(image_bytes, content_type, max_dim=1024):
    """Normalize any upload to a modest JPEG. Never trust the browser to have done it.

    Returns (bytes, content_type). Handles HEIC, EXIF rotation, and oversized photos.
    """
    if Image is None:
        return image_bytes, content_type
    try:
        img = Image.open(io.BytesIO(image_bytes))
        # Phone photos carry rotation in EXIF; without this the model reads them sideways
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        if max(img.size) > max_dim:
            img = img.copy()
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        for quality in (80, 70, 60):
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) <= MAX_IMAGE_BYTES:
                return data, "image/jpeg"
        return data, "image/jpeg"  # smallest attempt; better to try than to fail
    except Exception:
        # Unreadable by Pillow — let Groq have a go at the original
        return image_bytes, content_type


class GroqRateLimit(Exception):
    """Groq returned 429. Carries retry_after so the client can pace itself."""
    def __init__(self, retry_after, daily=False):
        self.retry_after = retry_after
        self.daily = daily
        super().__init__("rate limited")


class GroqRequestTooLarge(Exception):
    """Groq returned 413: this single request exceeds the per-minute token budget."""
    def __init__(self, detail=""):
        self.detail = detail
        super().__init__(detail or "request too large")


def _call_groq(payload, retries=1):
    """Helper to call Groq API. Retries briefly on 429, then hands off to the client."""
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise Exception("GROQ_API_KEY chưa được cấu hình")
    url = "https://api.groq.com/openai/v1/chat/completions"
    for attempt in range(retries + 1):
        try:
            resp = http_requests.post(url, json=payload, headers={"Authorization": f"Bearer {api_key}"}, timeout=60)
        except Exception as e:
            raise Exception(f"Không kết nối được Groq: {str(e)}")
        if resp.status_code != 429:
            break
        try:
            wait = float(resp.headers.get("retry-after", 0))
        except ValueError:
            wait = 0
        # Free tier is 8K tokens/minute. Sleep it off once if the window is short,
        # otherwise let the browser wait instead of holding a gunicorn worker.
        if attempt == retries or wait > 20:
            daily = "per day" in resp.text.lower() or "TPD" in resp.text
            raise GroqRateLimit(int(wait) if wait else 60, daily=daily)
        time.sleep(max(wait, 3))
    raw = resp.text
    if resp.status_code != 200:
        try:
            err = json.loads(raw)
            msg = err.get("error", {}).get("message", raw[:200])
        except:
            msg = raw[:200] if raw else f"HTTP {resp.status_code}"
        if resp.status_code == 413:
            raise GroqRequestTooLarge(msg)
        raise Exception(f"Groq API lỗi ({resp.status_code}): {msg}")
    if not raw or not raw.strip():
        raise Exception("Groq API trả về response rỗng")
    try:
        data = json.loads(raw)
    except:
        raise Exception(f"Groq trả về không phải JSON: {raw[:200]}")
    if not data.get("choices"):
        raise Exception(f"Groq không có choices: {raw[:200]}")
    content = data["choices"][0].get("message", {}).get("content", "")
    text = content.strip() if content else ""
    if not text:
        raise Exception("Groq trả về nội dung rỗng")
    # qwen3.6 is a reasoning model — strip any thinking block that leaks through
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    text = re.sub(r'^<think>.*', '', text, flags=re.DOTALL).strip()
    if text.startswith("```"):
        text = re.sub(r'^```\w*\n?', '', text)
        text = re.sub(r'\n?```$', '', text)
    return text


def _parse_json_array(text):
    """Pull a JSON array out of a model response, tolerating prose and truncation."""
    for candidate in (text, ):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    start = text.find('[')
    if start == -1:
        raise json.JSONDecodeError("no array", text, 0)
    end = text.rfind(']')
    if end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    # Output was cut off mid-array (max_tokens): salvage the complete objects
    objs = []
    depth = 0
    obj_start = None
    for i, ch in enumerate(text[start:], start):
        if ch == '{':
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and obj_start is not None:
                try:
                    objs.append(json.loads(text[obj_start:i + 1]))
                except json.JSONDecodeError:
                    pass
                obj_start = None
    if objs:
        return objs
    raise json.JSONDecodeError("unsalvageable", text, 0)


def _parse_amount(raw):
    """AI may return 45000, '45000', '45.000', '45,000đ' or '1.234.567 VND'."""
    if isinstance(raw, (int, float)):
        return int(raw)
    digits = re.sub(r'[^\d]', '', str(raw or ''))
    return int(digits) if digits else 0

def step1_extract(image_bytes, content_type):
    """Step 1: Extract all items from image as raw text/data"""
    today_str = vn_today().isoformat()
    yesterday_str = (vn_today() - timedelta(days=1)).isoformat()
    year = vn_today().year
    prompt = EXTRACT_PROMPT.format(today=today_str, yesterday=yesterday_str, year=year)

    def ask(max_dim, max_out):
        data, ctype = _prepare_image(image_bytes, content_type, max_dim=max_dim)
        b64 = base64.standard_b64encode(data).decode("utf-8")
        return _call_groq({
            "model": VISION_MODEL,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{ctype};base64,{b64}"}},
                {"type": "text", "text": prompt}
            ]}],
            "temperature": 0.1,
            "max_tokens": max_out,
            # Non-thinking mode: reasoning tokens burn the budget and corrupt JSON
            "reasoning_effort": "none",
            "reasoning_format": "hidden",
        })

    # If the request still overshoots the token budget, shrink and try again
    last_error = None
    text = None
    for max_dim, max_out in EXTRACT_ATTEMPTS:
        try:
            text = ask(max_dim, max_out)
        except GroqRequestTooLarge as e:
            last_error = e
            continue
        # Long statement cut off mid-array: pay for a bigger budget just this once.
        # If that retry is refused, keep the salvageable rows we already have.
        if _looks_truncated(text):
            try:
                text = ask(max_dim, EXTRACT_RETRY_LARGE) or text
            except (GroqRequestTooLarge, GroqRateLimit):
                pass
        break
    if text is None:
        raise Exception(
            "Ảnh này vượt hạn mức token mỗi phút của Groq free tier kể cả sau khi "
            f"đã thu nhỏ. Thử chụp riêng từng phần. (Groq: {getattr(last_error, 'detail', '')[:150]})"
        )
    if not text:
        return []
    try:
        items = _parse_json_array(text)
    except json.JSONDecodeError:
        raise Exception(f"AI không trả về dữ liệu đọc được. Thử chụp rõ hơn. (AI nói: {text[:120]})")
    if not isinstance(items, list):
        items = [items]

    # Validate and clean extracted items — one bad row must not kill the whole scan
    today_str = vn_today().isoformat()
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        amt = _parse_amount(item.get('amount'))
        if amt <= 0:
            continue
        currency = str(item.get('currency') or 'VND').upper().strip()
        detail = str(item.get('detail') or '')[:80]
        entry = {
            'date': item.get('date') or today_str,
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

def step2_categorize(items, user_code=None):
    """Step 2: Categorize extracted items using AI + user history"""
    if not items:
        return items

    # Build history rules
    history_rules = ""
    if user_code:
        patterns = get_user_category_patterns(user_code)
        if patterns:
            lines = [f'  "{d}" → {c}' for d, c in list(patterns.items())[:30]]
            history_rules = "QUY TẮC TỪ LỊCH SỬ NGƯỜI DÙNG (ƯU TIÊN CAO NHẤT):\n" + "\n".join(lines)

    # Most merchants are recognisable from the name alone. Settling those locally
    # skips a whole round trip, which is most of the per-scan latency.
    unresolved = []
    for i, item in enumerate(items):
        cat = _local_category(item.get('detail', ''))
        if cat:
            item['category'] = cat
        else:
            unresolved.append(i)
    if not unresolved:
        return items

    # Build items text — only for the ones keywords could not settle
    items_text = "\n".join([f'{i}. "{items[i]["detail"]}" - {items[i]["amount"]}đ ({items[i]["date"]})' for i in unresolved])

    prompt = CATEGORIZE_PROMPT.format(history_rules=history_rules, items_text=items_text)

    payload = {
        "model": TEXT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1, "max_tokens": 400
    }

    try:
        text = _call_groq(payload)
        json_match = re.search(r'\[.*\]', text, re.DOTALL)
        categories = json.loads(json_match.group() if json_match else text)
        if not isinstance(categories, list):
            categories = [categories]

        valid_cats = {'food','transport','shopping','entertainment','bills','health','education','beauty','savings','other'}
        cat_map = {}
        for c in categories:
            idx = c.get('index', -1)
            cat = c.get('category', 'other')
            if cat not in valid_cats:
                cat = 'other'
            cat_map[idx] = cat

        for i in unresolved:
            items[i]['category'] = cat_map.get(i, 'other')
    except:
        # Fallback: only the ones keywords could not settle
        for i in unresolved:
            items[i]['category'] = 'other'

    return items

def scan_with_groq(image_bytes, content_type, user_code=None):
    """Full 3-step pipeline: extract → categorize → duplicate detect"""
    # Step 1: Extract items from image
    items = step1_extract(image_bytes, content_type)

    # Step 2: Categorize using AI + user history
    items = step2_categorize(items, user_code)

    # Step 3: Duplicate detection (returned separately)
    duplicates = []
    if user_code and items:
        duplicates = find_potential_duplicates(user_code, items)

    return items, duplicates

# ── Public routes (no auth) ──

@app.errorhandler(413)
def too_large(e):
    return jsonify({
        "error": "Ảnh quá lớn (>25MB). Chụp lại hoặc dùng ảnh chụp màn hình.",
        "items": [], "duplicates": []
    }), 413


@app.route("/")
def index():
    # The whole app lives in this file; a cached copy leaves users on stale JS
    resp = send_file("index.html")
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp

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

@app.route("/api/category-patterns")
@require_auth
def category_patterns():
    """Return user's learned category patterns from expense history"""
    user_code = get_user_code()
    patterns = get_user_category_patterns(user_code)
    return jsonify(patterns)

@app.route("/api/scan", methods=["POST"])
@require_auth
def scan_receipt():
    if 'image' not in request.files:
        return jsonify({"error": "No image"}), 400
    file = request.files['image']
    image_bytes = file.read()
    content_type = file.content_type or 'image/jpeg'
    if not image_bytes:
        return jsonify({"error": "Ảnh rỗng", "items": [], "duplicates": []}), 400
    try:
        items, duplicates = scan_with_groq(image_bytes, content_type, user_code=get_user_code())
        return jsonify({"items": items, "duplicates": duplicates})
    except GroqRateLimit as e:
        if e.daily:
            msg = "Đã dùng hết lượt AI miễn phí trong ngày (200K token). Đợi sang ngày mai."
        else:
            msg = f"AI đang bận, tự thử lại sau {e.retry_after}s..."
        return jsonify({
            "error": msg, "rate_limited": True, "daily": e.daily,
            "retry_after": e.retry_after, "items": [], "duplicates": []
        }), 429
    except Exception as e:
        return jsonify({"error": str(e), "items": [], "duplicates": []}), 500

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
    cur.execute("SELECT id, date, category, detail, amount, COALESCE(project_id, '') as project_id FROM expenses WHERE user_code=%s ORDER BY date DESC, created_at DESC", (user_code,))
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
        "INSERT INTO expenses (id, date, category, detail, amount, user_code, project_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (data["id"], data["date"], data["category"], data.get("detail", ""), data["amount"], user_code, data.get("project_id", ""))
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
            "INSERT INTO expenses (id, date, category, detail, amount, user_code, project_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (data["id"], data["date"], data["category"], data.get("detail", ""), data["amount"], user_code, data.get("project_id", ""))
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
        "UPDATE expenses SET date=%s, category=%s, detail=%s, amount=%s, project_id=%s WHERE id=%s AND user_code=%s",
        (data["date"], data["category"], data.get("detail", ""), data["amount"], data.get("project_id", ""), eid, user_code)
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
    cur.execute("SELECT month, amount, COALESCE(income, 0) as income, COALESCE(savings_target, 0) as savings_target FROM budgets WHERE user_code=%s", (user_code,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    result = {}
    for r in rows:
        result[r["month"]] = {"amount": r["amount"], "income": r["income"], "savings_target": r["savings_target"]}
    return jsonify(result)

@app.route("/api/budgets", methods=["POST"])
@require_auth
def set_budget():
    data = request.json
    user_code = get_user_code()
    income = data.get("income", 0)
    savings_target = data.get("savings_target", 0)
    amount = income - savings_target if income > 0 else data.get("amount", 0)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO budgets (month, amount, income, savings_target, user_code) VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (month, user_code) DO UPDATE SET amount=%s, income=%s, savings_target=%s""",
        (data["month"], amount, income, savings_target, user_code, amount, income, savings_target)
    )
    conn.commit()
    cur.close()
    conn.close()
    notify_clients(user_code)
    return jsonify({"ok": True})

# ── Projects ──

@app.route("/api/projects")
@require_auth
def list_projects():
    user_code = get_user_code()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, name, emoji FROM projects WHERE user_code=%s ORDER BY created_at DESC", (user_code,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(rows)

@app.route("/api/projects", methods=["POST"])
@require_auth
def add_project():
    data = request.json
    user_code = get_user_code()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO projects (id, name, emoji, user_code) VALUES (%s, %s, %s, %s)",
        (data["id"], data["name"], data.get("emoji", ""), user_code)
    )
    conn.commit()
    cur.close()
    conn.close()
    notify_clients(user_code)
    return jsonify({"ok": True}), 201

@app.route("/api/projects/<pid>", methods=["DELETE"])
@require_auth
def delete_project(pid):
    user_code = get_user_code()
    conn = get_db()
    cur = conn.cursor()
    # Remove project_id from expenses that use this project
    cur.execute("UPDATE expenses SET project_id='' WHERE project_id=%s AND user_code=%s", (pid, user_code))
    cur.execute("DELETE FROM projects WHERE id=%s AND user_code=%s", (pid, user_code))
    conn.commit()
    cur.close()
    conn.close()
    notify_clients(user_code)
    return jsonify({"ok": True})

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
