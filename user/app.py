import os
import io
import secrets
from flask import Flask, render_template, request, jsonify, send_from_directory
import qrcode
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ADMIN_API_URL = os.environ.get("ADMIN_API_URL", "http://127.0.0.1:5001").rstrip("/")
INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "local-internal-key")

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"), static_folder=os.path.join(BASE_DIR, "static"))


def notify_admin(event):
    try:
        requests.post(f"{ADMIN_API_URL}/internal/event", json={"event": event}, headers={"X-Internal-Key": INTERNAL_API_KEY}, timeout=3)
    except requests.RequestException:
        pass


@app.route("/")
def index():
    notify_admin("visitor")
    return render_template("index.html")


@app.route("/manifest.json")
def manifest():
    return send_from_directory(app.static_folder, "manifest.json")


@app.route("/sw.js")
def service_worker():
    response = send_from_directory(app.static_folder, "sw.js")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.post("/api/generate")
def api_generate():
    data = request.get_json(silent=True) or {}
    content = str(data.get("content", "")).strip()
    if not content:
        return jsonify({"error": "Contenu vide."}), 400
    if len(content) > 4000:
        return jsonify({"error": "Le contenu est trop long."}), 400
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
    qr.add_data(content); qr.make(fit=True)
    img = qr.make_image(fill_color="#111827", back_color="white")
    buffer = io.BytesIO(); img.save(buffer, format="PNG")
    notify_admin("qr_generated")
    from base64 import b64encode
    return jsonify({"image": "data:image/png;base64," + b64encode(buffer.getvalue()).decode("ascii")})


@app.get("/api/ads")
def api_ads():
    try:
        response = requests.get(f"{ADMIN_API_URL}/public/ads", timeout=5)
        if not response.ok:
            return jsonify([])
        return jsonify(response.json())
    except requests.RequestException:
        return jsonify([])


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "service": "user"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
