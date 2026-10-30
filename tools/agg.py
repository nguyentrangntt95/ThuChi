import os, json, collections, sys
import psycopg2, psycopg2.extras

url = os.environ.get('DATABASE_PUBLIC_URL') or os.environ.get('DATABASE_URL')
if not url:
    print("NO DATABASE_URL in env; keys:", [k for k in os.environ if 'DATA' in k or 'PG' in k]); sys.exit(1)
conn = psycopg2.connect(url)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("SELECT user_code, COUNT(*) n, MIN(date) d0, MAX(date) d1 FROM expenses GROUP BY user_code ORDER BY n DESC")
users = cur.fetchall()
print("USERS:", [(u['user_code'], u['n'], str(u['d0']), str(u['d1'])) for u in users])
uc = users[0]['user_code']
cur.execute("SELECT date::text, category, amount, detail FROM expenses WHERE user_code=%s", (uc,))
rows = cur.fetchall()
cur.execute("SELECT month, amount, COALESCE(income,0) income, COALESCE(savings_target,0) sav FROM budgets WHERE user_code=%s ORDER BY month", (uc,))
buds = cur.fetchall()
here = os.path.dirname(os.path.abspath(__file__))
json.dump([dict(r) for r in rows], open(os.path.join(here, '_real_exp.json'), 'w', encoding='utf-8'), ensure_ascii=False)
json.dump([dict(b) for b in buds], open(os.path.join(here, '_real_bud.json'), 'w', encoding='utf-8'), ensure_ascii=False)
print("BUDGETS:", [(b['month'], b['amount'], b['income'], b['sav']) for b in buds])
bm = collections.defaultdict(lambda: collections.defaultdict(int))
for e in rows:
    bm[e['date'][:7]][e['category']] += e['amount']
cats = ['food', 'transport', 'shopping', 'entertainment', 'bills', 'health', 'education', 'beauty', 'other', 'savings']
print("month   " + "".join(f"{c[:6]:>8}" for c in cats) + "   tot(ex-sav)")
for m in sorted(bm):
    row = bm[m]
    tot = sum(v for k, v in row.items() if k != 'savings')
    print(m + " " + "".join(f"{row.get(c, 0) / 1e6:8.2f}" for c in cats) + f"  {tot / 1e6:8.2f}")
print("Saved tools/_real_exp.json and tools/_real_bud.json (git-ignored).")
