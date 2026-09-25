#!/usr/bin/env python3
"""
WaveTies Logistics – Invoice Web App (Full)
Features: Admin/Worker auth (hashed), mark paid, customers, reports,
          email invoice, WhatsApp share, deployment-ready.
"""

import sys
import os
import json
import secrets
import sqlite3
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from collections import defaultdict

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, send_file, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from invoice_generator import (
    generate_invoice, load_db, save_db, create_invoice,
    COMPANY, OUTPUT_DIR
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
USERS_DB = DATA_DIR / "users.db"
SMTP_CONFIG_FILE = DATA_DIR / "smtp_config.json"


# ──────────────────────────────────────────────────────────────
# Users (SQLite + hashed passwords)
# ──────────────────────────────────────────────────────────────
def get_users_db():
    conn = sqlite3.connect(str(USERS_DB))
    conn.row_factory = sqlite3.Row
    return conn


def init_users_db():
    conn = get_users_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'worker',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT
        )
    """)
    conn.commit()
    # Seed default users if empty
    cur = conn.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        defaults = [
            ("admin", "admin123", "System Admin", "admin"),
            ("worker", "worker123", "Invoice Worker", "worker"),
            ("kofi", "kofi123", "Kofi Mensah", "worker"),
        ]
        for u, p, n, r in defaults:
            conn.execute(
                "INSERT INTO users (username, password_hash, name, role, created_at) VALUES (?,?,?,?,?)",
                (u, generate_password_hash(p), n, r, datetime.now().isoformat(timespec="seconds"))
            )
        conn.commit()
    conn.close()


def get_user(username):
    conn = get_users_db()
    row = conn.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_users():
    conn = get_users_db()
    rows = conn.execute("SELECT id, username, name, role, active, created_at FROM users ORDER BY username").fetchall()
    conn.close()
    return [dict(r) for r in rows]


init_users_db()


# ──────────────────────────────────────────────────────────────
# Auth decorators
# ──────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "username" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("role") != "admin":
            flash("Admin access required.", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated


# ──────────────────────────────────────────────────────────────
# Auth routes
# ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if "username" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        user = get_user(username)
        if user and check_password_hash(user["password_hash"], password):
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["name"] = user["name"]
            flash(f"Welcome back, {user['name']}!", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "danger")
    return render_template("login.html", company=COMPANY)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ──────────────────────────────────────────────────────────────
# Dashboard & Reports
# ──────────────────────────────────────────────────────────────
@app.route("/dashboard")
@login_required
def dashboard():
    db = load_db()
    invoices = db.get("invoices", {})
    sorted_invs = sorted(invoices.items(), key=lambda x: x[1].get("created_at", ""), reverse=True)

    total_amount = sum(i.get("total", 0) for i in invoices.values())
    outstanding = sum(i.get("balance_due", 0) for i in invoices.values() if i.get("balance_due", 0) > 0)
    paid_count = sum(1 for i in invoices.values() if i.get("balance_due", 0) <= 0)
    due_count = sum(1 for i in invoices.values() if i.get("balance_due", 0) > 0)

    stats = {
        "total": len(invoices),
        "due": due_count,
        "paid": paid_count,
        "total_amount": total_amount,
        "outstanding": outstanding,
    }
    return render_template(
        "dashboard.html",
        invoices=sorted_invs[:15],
        stats=stats,
        company=COMPANY,
        user=session,
    )


@app.route("/reports")
@login_required
def reports():
    db = load_db()
    invoices = db.get("invoices", {})

    # By month
    by_month = defaultdict(lambda: {"count": 0, "total": 0.0, "paid": 0.0, "outstanding": 0.0})
    by_worker = defaultdict(lambda: {"count": 0, "total": 0.0})
    by_transport = defaultdict(lambda: {"count": 0, "total": 0.0})

    for inv in invoices.values():
        created = inv.get("created_at") or inv.get("invoice_date") or ""
        month = created[:7] if len(created) >= 7 else "Unknown"
        by_month[month]["count"] += 1
        by_month[month]["total"] += inv.get("total", 0)
        by_month[month]["paid"] += inv.get("paid", 0)
        by_month[month]["outstanding"] += max(0, inv.get("balance_due", 0))

        worker = inv.get("created_by") or "unknown"
        by_worker[worker]["count"] += 1
        by_worker[worker]["total"] += inv.get("total", 0)

        for item in inv.get("items") or []:
            t = item.get("transport") or "OTHER"
            by_transport[t]["count"] += 1
            by_transport[t]["total"] += item.get("amount", 0)

    months = sorted(by_month.keys(), reverse=True)
    return render_template(
        "reports.html",
        by_month=[(m, by_month[m]) for m in months],
        by_worker=sorted(by_worker.items(), key=lambda x: -x[1]["total"]),
        by_transport=sorted(by_transport.items(), key=lambda x: -x[1]["total"]),
        company=COMPANY,
        user=session,
    )


# ──────────────────────────────────────────────────────────────
# Invoices
# ──────────────────────────────────────────────────────────────
@app.route("/invoices")
@login_required
def invoice_list():
    db = load_db()
    invoices = db.get("invoices", {})
    status_filter = request.args.get("status", "all")
    sorted_invs = sorted(invoices.items(), key=lambda x: x[1].get("created_at", ""), reverse=True)
    if status_filter == "due":
        sorted_invs = [(n, i) for n, i in sorted_invs if i.get("balance_due", 0) > 0]
    elif status_filter == "paid":
        sorted_invs = [(n, i) for n, i in sorted_invs if i.get("balance_due", 0) <= 0]
    return render_template(
        "invoices.html",
        invoices=sorted_invs,
        status_filter=status_filter,
        company=COMPANY,
        user=session,
    )


@app.route("/invoices/new", methods=["GET", "POST"])
@login_required
def create_invoice_page():
    db = load_db()
    customers = list(db.get("customers", {}).values())

    if request.method == "POST":
        try:
            customer_name = request.form.get("customer_name", "").strip()
            customer_email = request.form.get("customer_email", "").strip()
            customer_phone = request.form.get("customer_phone", "").strip()
            currency = request.form.get("currency", "GHS")
            tax = float(request.form.get("tax", 0) or 0)
            paid = float(request.form.get("paid", 0) or 0)
            due_days = int(request.form.get("due_days", 30) or 30)

            trackings = request.form.getlist("tracking[]")
            item_names = request.form.getlist("item_name[]")
            transports = request.form.getlist("transport[]")
            boxes_list = request.form.getlist("boxes[]")
            weights = request.form.getlist("weight_kg[]")
            vols = request.form.getlist("vol_cbm[]")
            order_nos = request.form.getlist("order_number[]")
            amounts = request.form.getlist("amount[]")

            items = []
            for i in range(len(trackings)):
                if not trackings[i].strip():
                    continue
                items.append({
                    "tracking": trackings[i].strip(),
                    "item_name": item_names[i].strip() if i < len(item_names) else "Goods",
                    "transport": transports[i].strip() if i < len(transports) else "SEA_FREIGHT",
                    "boxes": int(boxes_list[i] or 1) if i < len(boxes_list) else 1,
                    "weight_kg": float(weights[i] or 0) if i < len(weights) else 0,
                    "vol_cbm": float(vols[i] or 0) if i < len(vols) else 0,
                    "order_number": order_nos[i].strip() if i < len(order_nos) else "",
                    "amount": float(amounts[i] or 0) if i < len(amounts) else 0,
                })

            if not customer_name:
                flash("Customer name is required.", "danger")
                return redirect(url_for("create_invoice_page"))
            if not items:
                flash("At least one line item is required.", "danger")
                return redirect(url_for("create_invoice_page"))

            inv_date = datetime.now()
            due_date = inv_date + timedelta(days=due_days)
            data = {
                "customer_name": customer_name,
                "customer_email": customer_email,
                "customer_phone": customer_phone,
                "invoice_date": inv_date.strftime("%b %d, %Y"),
                "due_date": due_date.strftime("%b %d, %Y"),
                "currency": currency,
                "tax": tax,
                "paid": paid,
                "items": items,
                "momo": request.form.get("momo") or COMPANY.get("momo"),
                "bank_lines": [
                    line.strip()
                    for line in (request.form.get("bank_details") or "\n".join(COMPANY.get("bank_lines", []))).splitlines()
                    if line.strip()
                ],
                "contact_phone": request.form.get("contact_phone") or COMPANY.get("phone"),
            }
            inv_no = create_invoice(data, created_by=session.get("username", "worker"))
            flash(f"Invoice {inv_no} created successfully!", "success")
            return redirect(url_for("view_invoice", invoice_no=inv_no))
        except Exception as e:
            flash(f"Error creating invoice: {e}", "danger")
            return redirect(url_for("create_invoice_page"))

    return render_template(
        "create_invoice.html",
        company=COMPANY,
        user=session,
        customers=customers,
        default_momo=COMPANY.get("momo", ""),
        default_bank="\n".join(COMPANY.get("bank_lines", [])),
        default_phone=COMPANY.get("phone", ""),
    )


@app.route("/invoices/<path:invoice_no>")
@login_required
def view_invoice(invoice_no):
    db = load_db()
    inv = db["invoices"].get(invoice_no)
    if not inv:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoice_list"))
    return render_template(
        "view_invoice.html",
        inv=inv,
        invoice_no=invoice_no,
        company=COMPANY,
        user=session,
    )


@app.route("/invoices/<path:invoice_no>/pdf")
@login_required
def download_pdf(invoice_no):
    db = load_db()
    inv = db["invoices"].get(invoice_no)
    if not inv:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoice_list"))
    safe_name = invoice_no.replace("/", "-")
    pdf_path = OUTPUT_DIR / f"Invoice_{safe_name}.pdf"
    if not pdf_path.exists():
        generate_invoice(inv, str(pdf_path))
    return send_file(pdf_path, as_attachment=True, download_name=f"Invoice_{safe_name}.pdf", mimetype="application/pdf")


@app.route("/invoices/<path:invoice_no>/regenerate")
@login_required
def regenerate_pdf(invoice_no):
    db = load_db()
    inv = db["invoices"].get(invoice_no)
    if not inv:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoice_list"))
    safe_name = invoice_no.replace("/", "-")
    pdf_path = OUTPUT_DIR / f"Invoice_{safe_name}.pdf"
    generate_invoice(inv, str(pdf_path))
    flash("PDF regenerated.", "success")
    return redirect(url_for("view_invoice", invoice_no=invoice_no))


# ── Mark as Paid ──────────────────────────────────────────────
@app.route("/invoices/<path:invoice_no>/pay", methods=["GET", "POST"])
@login_required
def mark_paid(invoice_no):
    db = load_db()
    inv = db["invoices"].get(invoice_no)
    if not inv:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoice_list"))

    if request.method == "POST":
        try:
            amount = float(request.form.get("amount", 0) or 0)
            note = request.form.get("note", "").strip()
            if amount <= 0:
                flash("Payment amount must be greater than zero.", "danger")
                return redirect(url_for("mark_paid", invoice_no=invoice_no))

            inv["paid"] = round(inv.get("paid", 0) + amount, 2)
            inv["balance_due"] = round(max(0, inv.get("total", 0) - inv["paid"]), 2)
            inv.setdefault("payments", [])
            inv["payments"].append({
                "amount": amount,
                "note": note,
                "by": session.get("username"),
                "at": datetime.now().isoformat(timespec="seconds"),
            })
            db["invoices"][invoice_no] = inv
            save_db(db)

            # Regenerate PDF with updated paid/balance
            safe_name = invoice_no.replace("/", "-")
            generate_invoice(inv, str(OUTPUT_DIR / f"Invoice_{safe_name}.pdf"))

            if inv["balance_due"] <= 0:
                flash(f"Invoice {invoice_no} marked as fully paid!", "success")
            else:
                flash(f"Payment of {inv.get('currency','GHS')} {amount:.2f} recorded. Balance due: {inv['balance_due']:.2f}", "success")
            return redirect(url_for("view_invoice", invoice_no=invoice_no))
        except Exception as e:
            flash(f"Error recording payment: {e}", "danger")

    balance = inv.get("balance_due", inv.get("total", 0))
    return render_template(
        "mark_paid.html",
        inv=inv,
        invoice_no=invoice_no,
        balance=balance,
        company=COMPANY,
        user=session,
    )


# ── Email invoice ─────────────────────────────────────────────
def load_smtp_config():
    if SMTP_CONFIG_FILE.exists():
        with open(SMTP_CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_smtp_config(cfg):
    with open(SMTP_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


@app.route("/invoices/<path:invoice_no>/email", methods=["GET", "POST"])
@login_required
def email_invoice(invoice_no):
    db = load_db()
    inv = db["invoices"].get(invoice_no)
    if not inv:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoice_list"))

    smtp = load_smtp_config()
    if request.method == "POST":
        to_email = request.form.get("to_email", "").strip()
        subject = request.form.get("subject", f"Invoice {invoice_no} – WaveTies").strip()
        body = request.form.get("body", "").strip()
        if not to_email:
            flash("Recipient email is required.", "danger")
            return redirect(url_for("email_invoice", invoice_no=invoice_no))

        if not smtp.get("host") or not smtp.get("user") or not smtp.get("password"):
            flash("Email is not configured. Ask an admin to set SMTP settings.", "warning")
            return redirect(url_for("email_invoice", invoice_no=invoice_no))

        try:
            safe_name = invoice_no.replace("/", "-")
            pdf_path = OUTPUT_DIR / f"Invoice_{safe_name}.pdf"
            if not pdf_path.exists():
                generate_invoice(inv, str(pdf_path))

            msg = MIMEMultipart()
            msg["From"] = smtp.get("from_email") or smtp["user"]
            msg["To"] = to_email
            msg["Subject"] = subject
            msg.attach(MIMEText(body or f"Please find invoice {invoice_no} attached.\n\nWaveTies Logistics – Stronger Ties", "plain"))

            with open(pdf_path, "rb") as f:
                part = MIMEBase("application", "pdf")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename=Invoice_{safe_name}.pdf")
            msg.attach(part)

            with smtplib.SMTP(smtp["host"], int(smtp.get("port", 587))) as server:
                if smtp.get("use_tls", True):
                    server.starttls()
                server.login(smtp["user"], smtp["password"])
                server.send_message(msg)

            flash(f"Invoice emailed to {to_email}.", "success")
            return redirect(url_for("view_invoice", invoice_no=invoice_no))
        except Exception as e:
            flash(f"Email failed: {e}", "danger")

    default_body = (
        f"Dear {inv.get('customer_name', 'Customer')},\n\n"
        f"Please find attached invoice {invoice_no}.\n"
        f"Amount due: {inv.get('currency','GHS')} {inv.get('balance_due', inv.get('total', 0)):.2f}\n\n"
        f"Payment details are on the invoice.\n\n"
        f"Thank you,\nWaveTies Logistics And Supply Chain Solutions\nStronger Ties"
    )
    return render_template(
        "email_invoice.html",
        inv=inv,
        invoice_no=invoice_no,
        default_to=inv.get("customer_email", ""),
        default_body=default_body,
        smtp_configured=bool(smtp.get("host") and smtp.get("user")),
        company=COMPANY,
        user=session,
    )


# ──────────────────────────────────────────────────────────────
# Customers
# ──────────────────────────────────────────────────────────────
@app.route("/customers")
@login_required
def customers_list():
    db = load_db()
    customers = sorted(db.get("customers", {}).values(), key=lambda c: c.get("name", "").lower())
    return render_template("customers.html", customers=customers, company=COMPANY, user=session)


@app.route("/customers/new", methods=["GET", "POST"])
@login_required
def customer_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        if not name:
            flash("Name is required.", "danger")
            return redirect(url_for("customer_new"))
        db = load_db()
        key = email or name
        db.setdefault("customers", {})[key] = {"name": name, "email": email, "phone": phone}
        save_db(db)
        flash(f"Customer {name} saved.", "success")
        return redirect(url_for("customers_list"))
    return render_template("customer_form.html", customer=None, company=COMPANY, user=session)


@app.route("/customers/<path:key>/edit", methods=["GET", "POST"])
@login_required
def customer_edit(key):
    db = load_db()
    customer = db.get("customers", {}).get(key)
    if not customer:
        flash("Customer not found.", "danger")
        return redirect(url_for("customers_list"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        if not name:
            flash("Name is required.", "danger")
            return redirect(url_for("customer_edit", key=key))
        # Remove old key if email/name changed
        del db["customers"][key]
        new_key = email or name
        db["customers"][new_key] = {"name": name, "email": email, "phone": phone}
        save_db(db)
        flash("Customer updated.", "success")
        return redirect(url_for("customers_list"))
    return render_template("customer_form.html", customer=customer, customer_key=key, company=COMPANY, user=session)


@app.route("/customers/<path:key>/delete", methods=["POST"])
@login_required
@admin_required
def customer_delete(key):
    db = load_db()
    if key in db.get("customers", {}):
        name = db["customers"][key].get("name", key)
        del db["customers"][key]
        save_db(db)
        flash(f"Customer {name} deleted.", "success")
    return redirect(url_for("customers_list"))


# ──────────────────────────────────────────────────────────────
# Admin: Users & SMTP
# ──────────────────────────────────────────────────────────────
@app.route("/admin/users")
@login_required
@admin_required
def admin_users():
    return render_template("admin_users.html", users=list_users(), company=COMPANY, user=session)


@app.route("/admin/users/new", methods=["GET", "POST"])
@login_required
@admin_required
def admin_user_new():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        name = request.form.get("name", "").strip()
        role = request.form.get("role", "worker")
        if not username or not password or not name:
            flash("All fields are required.", "danger")
            return redirect(url_for("admin_user_new"))
        if get_user(username):
            flash("Username already exists.", "danger")
            return redirect(url_for("admin_user_new"))
        conn = get_users_db()
        conn.execute(
            "INSERT INTO users (username, password_hash, name, role, created_at) VALUES (?,?,?,?,?)",
            (username, generate_password_hash(password), name, role, datetime.now().isoformat(timespec="seconds"))
        )
        conn.commit()
        conn.close()
        flash(f"User {username} created.", "success")
        return redirect(url_for("admin_users"))
    return render_template("admin_user_form.html", edit_user=None, company=COMPANY, user=session)


@app.route("/admin/users/<int:uid>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def admin_user_edit(uid):
    conn = get_users_db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        conn.close()
        flash("User not found.", "danger")
        return redirect(url_for("admin_users"))
    edit_user = dict(row)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        role = request.form.get("role", "worker")
        password = request.form.get("password", "")
        active = 1 if request.form.get("active") else 0
        if password:
            conn.execute(
                "UPDATE users SET name=?, role=?, active=?, password_hash=? WHERE id=?",
                (name, role, active, generate_password_hash(password), uid)
            )
        else:
            conn.execute("UPDATE users SET name=?, role=?, active=? WHERE id=?", (name, role, active, uid))
        conn.commit()
        conn.close()
        flash("User updated.", "success")
        return redirect(url_for("admin_users"))
    conn.close()
    return render_template("admin_user_form.html", edit_user=edit_user, company=COMPANY, user=session)


@app.route("/admin/smtp", methods=["GET", "POST"])
@login_required
@admin_required
def admin_smtp():
    cfg = load_smtp_config()
    if request.method == "POST":
        cfg = {
            "host": request.form.get("host", "").strip(),
            "port": int(request.form.get("port", 587) or 587),
            "user": request.form.get("user", "").strip(),
            "password": request.form.get("password", "").strip() or cfg.get("password", ""),
            "from_email": request.form.get("from_email", "").strip(),
            "use_tls": bool(request.form.get("use_tls")),
        }
        save_smtp_config(cfg)
        flash("SMTP settings saved.", "success")
        return redirect(url_for("admin_smtp"))
    return render_template("admin_smtp.html", cfg=cfg, company=COMPANY, user=session)


# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("PORT", 5000))
    print("=" * 50)
    print("  WaveTies Invoice System")
    print(f"  http://127.0.0.1:{port}")
    print("=" * 50)
    print("  Default logins (change after first login):")
    print("    admin  / admin123")
    print("    worker / worker123")
    print("    kofi   / kofi123")
    print("=" * 50)
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "1") == "1")
