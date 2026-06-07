from flask import Flask, request, jsonify
import requests, os, psycopg2
from datetime import datetime
from bs4 import BeautifulSoup
import cloudscraper

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
NODE_URL = "https://wa-bridge-8lia.onrender.com/send"
GROUP_ID = "120363429067223078@g.us"

# ================= DB =================
def db():
    return psycopg2.connect(DATABASE_URL)

# ================= SEND TO NODE =================
def send_to_whatsapp(image, caption):
    requests.post(
        "https://wa-bridge-8lia.onrender.com/send",
        json={
            "groupId": "120363429067223078@g.us",
            "image": image,
            "caption": caption
        },
        timeout=60
    )

# ================= SCRAPER =================
from PIL import Image
from io import BytesIO
import tempfile
import requests

def send_to_whatsapp(image_url, caption):

    try:
        headers = {
            "User-Agent": "Mozilla/5.0"
        }

        r = requests.get(
            image_url,
            headers=headers,
            timeout=60
        )

        img = Image.open(
            BytesIO(r.content)
        )

        img = img.convert("RGB")

        tmp = tempfile.NamedTemporaryFile(
            suffix=".jpg",
            delete=False
        )

        img.save(
            tmp.name,
            "JPEG",
            quality=95
        )

        with open(tmp.name, "rb") as f:

            requests.post(
                "https://wa-bridge-8lia.onrender.com/send",
                files={
                    "image": f
                },
                data={
                    "groupId": "120363429067223078@g.us",
                    "caption": caption
                },
                timeout=120
            )

        os.remove(tmp.name)

    except Exception as e:
        print("SEND ERROR:", e)

# ================= ROUTES =================
@app.route("/")
def home():
    return "Flask + WhatsApp Bridge Running"

@app.route("/api/check_updates")
def force_check():
    check_updates()
    return jsonify({"status": "done"})

@app.route("/api/clear_messages")
def clear():
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM messages")
    conn.commit()
    return "cleared"

@app.route("/api/send_test")
def test():
    send_to_whatsapp(
        "https://i.imgur.com/example.jpg",
        "تجربة إرسال"
    )
    return "sent"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
