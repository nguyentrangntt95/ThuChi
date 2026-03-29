import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

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
    return jsonify({"ok": True})

@app.route("/api/expenses/<eid>", methods=["DELETE"])
def delete_expense(eid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM expenses WHERE id=%s", (eid,))
    conn.commit()
    cur.close()
    conn.close()
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
    return jsonify({"ok": True})

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
