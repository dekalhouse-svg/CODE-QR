import os
import sqlite3
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "qr_admin.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "local-internal-key")
ALLOWED_AD_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif", "mp4", "webm", "mov"}
MAX_AD_SIZE = 25 * 1024 * 1024


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stats (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            visitors INTEGER NOT NULL DEFAULT 0,
            qr_generated INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO stats (id, visitors, qr_generated) VALUES (1, 0, 0);
        CREATE TABLE IF NOT EXISTS ads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            original_name TEXT NOT NULL,
            media_type TEXT NOT NULL,
            duration_days INTEGER NOT NULL DEFAULT 7,
            display_seconds INTEGER NOT NULL DEFAULT 5,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
    """)
    conn.commit(); conn.close()


def utc_now():
    return datetime.now(timezone.utc)


def expire_ads():
    """Automatically deactivate expired ads, but keep their records/files."""
    now = utc_now().isoformat()
    conn = db()
    conn.execute("UPDATE ads SET active = 0 WHERE active = 1 AND expires_at <= ?", (now,))
    conn.commit(); conn.close()


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper


def internal_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not secrets.compare_digest(request.headers.get("X-Internal-Key", ""), INTERNAL_API_KEY):
            return jsonify({"error": "Unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper


def is_allowed(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_AD_EXTENSIONS


def media_type(filename):
    return "video" if filename.rsplit(".", 1)[1].lower() in {"mp4", "webm", "mov"} else "image"


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if secrets.compare_digest(username, ADMIN_USERNAME) and secrets.compare_digest(password, ADMIN_PASSWORD):
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard"))
        error = "Identifiants incorrects."
    return render_template("admin_login.html", error=error)


@app.get("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.get("/admin")
@admin_required
def admin_dashboard():
    expire_ads()
    return render_template("admin.html")


@app.get("/api/admin/stats")
@admin_required
def admin_stats():
    expire_ads()
    conn = db()
    stats = conn.execute("SELECT visitors, qr_generated FROM stats WHERE id=1").fetchone()
    active = conn.execute("SELECT COUNT(*) AS c FROM ads WHERE active=1 AND expires_at > ?", (utc_now().isoformat(),)).fetchone()["c"]
    total = conn.execute("SELECT COUNT(*) AS c FROM ads").fetchone()["c"]
    conn.close()
    return jsonify({"visitors": stats["visitors"], "qr_generated": stats["qr_generated"], "active_ads": active, "total_ads": total})


@app.get("/api/admin/ads")
@admin_required
def admin_ads():
    expire_ads()
    conn = db(); rows = conn.execute("SELECT * FROM ads ORDER BY id DESC").fetchall(); conn.close()
    return jsonify([{
        "id": r["id"], "original_name": r["original_name"], "media_type": r["media_type"],
        "duration_days": r["duration_days"], "display_seconds": r["display_seconds"],
        "active": bool(r["active"]), "created_at": r["created_at"], "expires_at": r["expires_at"],
        "url": url_for("uploaded_file", filename=r["filename"]),
    } for r in rows])


@app.post("/api/admin/ads")
@admin_required
def admin_add_ad():
    if "file" not in request.files or not request.files["file"].filename:
        return jsonify({"error": "Aucun fichier reçu."}), 400
    file = request.files["file"]
    if not is_allowed(file.filename):
        return jsonify({"error": "Format non autorisé. Utilisez PNG, JPG, WEBP, GIF, MP4, WEBM ou MOV."}), 400
    if request.content_length and request.content_length > MAX_AD_SIZE + 2_000_000:
        return jsonify({"error": "Fichier trop volumineux. Maximum 25 MB."}), 413
    try:
        duration_days = max(1, min(3650, int(request.form.get("duration_days", "7"))))
        display_seconds = max(1, min(120, int(request.form.get("display_seconds", "5"))))
    except ValueError:
        return jsonify({"error": "Durée invalide."}), 400
    active = request.form.get("active", "1") == "1"
    from werkzeug.utils import secure_filename
    original = secure_filename(file.filename)
    ext = original.rsplit(".", 1)[1].lower()
    filename = f"{secrets.token_hex(16)}.{ext}"
    path = os.path.join(UPLOAD_DIR, filename)
    file.save(path)
    if os.path.getsize(path) > MAX_AD_SIZE:
        os.remove(path)
        return jsonify({"error": "Fichier trop volumineux. Maximum 25 MB."}), 413
    now = utc_now(); expires = now + timedelta(days=duration_days)
    conn = db()
    conn.execute("""INSERT INTO ads (filename, original_name, media_type, duration_days, display_seconds, active, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (filename, original, media_type(original), duration_days, display_seconds, int(active), now.isoformat(), expires.isoformat()))
    conn.commit(); conn.close()
    return jsonify({"ok": True})


@app.patch("/api/admin/ads/<int:ad_id>")
@admin_required
def admin_update_ad(ad_id):
    data = request.get_json(silent=True) or {}
    expire_ads()
    conn = db(); row = conn.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone()
    if not row:
        conn.close(); return jsonify({"error": "Publicité introuvable."}), 404
    if "active" in data:
        # Never allow an expired ad to be manually reactivated.
        active = bool(data["active"]) and row["expires_at"] > utc_now().isoformat()
        conn.execute("UPDATE ads SET active=? WHERE id=?", (1 if active else 0, ad_id))
    if "display_seconds" in data:
        try: seconds = max(1, min(120, int(data["display_seconds"])))
        except (ValueError, TypeError):
            conn.close(); return jsonify({"error": "Durée d'affichage invalide."}), 400
        conn.execute("UPDATE ads SET display_seconds=? WHERE id=?", (seconds, ad_id))
    conn.commit(); conn.close(); return jsonify({"ok": True})


@app.delete("/api/admin/ads/<int:ad_id>")
@admin_required
def admin_delete_ad(ad_id):
    conn = db(); row = conn.execute("SELECT filename FROM ads WHERE id=?", (ad_id,)).fetchone()
    if not row:
        conn.close(); return jsonify({"error": "Publicité introuvable."}), 404
    path = os.path.join(UPLOAD_DIR, row["filename"])
    if os.path.exists(path):
        try: os.remove(path)
        except OSError: pass
    conn.execute("DELETE FROM ads WHERE id=?", (ad_id,)); conn.commit(); conn.close()
    return jsonify({"ok": True})


# Public, read-only ad feed consumed by the separate User service.
@app.get("/public/ads")
def public_ads():
    expire_ads()
    conn = db()
    rows = conn.execute("SELECT id, filename, original_name, media_type, display_seconds FROM ads WHERE active=1 AND expires_at > ? ORDER BY id DESC", (utc_now().isoformat(),)).fetchall()
    conn.close()
    base = request.host_url.rstrip("/")
    return jsonify([{"id": r["id"], "url": f"{base}{url_for('uploaded_file', filename=r['filename'])}", "name": r["original_name"], "type": r["media_type"], "display_seconds": r["display_seconds"]} for r in rows])


@app.post("/internal/event")
@internal_required
def internal_event():
    event = (request.get_json(silent=True) or {}).get("event")
    if event not in {"visitor", "qr_generated"}:
        return jsonify({"error": "Unknown event"}), 400
    conn = db()
    field = "visitors" if event == "visitor" else "qr_generated"
    conn.execute(f"UPDATE stats SET {field} = {field} + 1 WHERE id=1")
    conn.commit(); conn.close()
    return jsonify({"ok": True})


@app.get("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "service": "admin"})


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)), debug=True)
