"""Kiểm tra cú pháp JS của index.html trước khi push (một lỗi cú pháp làm trắng cả app).

Chạy:  python tools/check_syntax.py
Cần có node trong PATH. Tách khối <script type="module"> cuối cùng ra file tạm rồi chạy `node --check`.
"""
import io, os, re, subprocess, sys, tempfile
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
s = io.open(os.path.join(root, 'index.html'), encoding='utf-8').read()
scripts = re.findall(r'<script type="module">(.*?)</script>', s, re.S)
if not scripts:
    print('Không tìm thấy <script type="module"> trong index.html'); sys.exit(1)
tmp = os.path.join(tempfile.gettempdir(), 'thuchi_page_module.mjs')
io.open(tmp, 'w', encoding='utf-8').write(scripts[-1])
r = subprocess.run(['node', '--check', tmp], capture_output=True, text=True, shell=(os.name == 'nt'))
if r.returncode != 0:
    print(r.stderr or r.stdout); print('SYNTAX ERROR'); sys.exit(1)
print('OK: cú pháp JS hợp lệ (%d dòng)' % scripts[-1].count('\n'))
