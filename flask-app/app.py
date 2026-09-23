from flask import Flask, jsonify
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


# ================= INIT DB =================
def init_db():

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            phone TEXT,
            message TEXT,
            sender TEXT,
            msg_time TEXT
        )
    """)

    conn.commit()
    cur.close()
    conn.close()


# ================= SEND TO NODE =================
def send_to_whatsapp(image, caption):

    try:
        requests.post(
            NODE_URL,
            json={
                "groupId": GROUP_ID,
                "image": image,
                "caption": caption
            },
            timeout=60
        )
    except Exception as e:
        print("SEND ERROR:", e)


# ================= SCRAPER =================
def get_series_details(scraper, url):

    try:
        from urllib.parse import urljoin

        full_url = urljoin("https://tuktukhd.com", url)

        res = scraper.get(full_url, timeout=30)
        res.raise_for_status()
        res.encoding = "utf-8"

        soup = BeautifulSoup(res.text, "html.parser")

        genres = []
        country = ""

        for li in soup.find_all("li"):
            text = li.get_text(" ", strip=True)

            if text.startswith("الانواع"):
                genres = [
                    a.get_text(" ", strip=True)
                    for a in li.find_all("a")
                    if a.get_text(" ", strip=True)
                ]
                break

        for li in soup.find_all("li"):
            text = li.get_text(" ", strip=True)

            if text.startswith("دولة المسلسل"):
                links = li.find_all("a")

                if links:
                    country = links[0].get_text(" ", strip=True)
                else:
                    country = text.replace("دولة المسلسل", "", 1).strip()
                break

        return genres, country

    except Exception as e:
        print("DETAIL ERROR:", url, e)
        return [], ""


def check_updates():

    conn = db()
    cur = conn.cursor()

    scraper = cloudscraper.create_scraper()
    res = scraper.get("https://tuktukhd.com/recent/", timeout=30)
    res.encoding = 'utf-8'

    soup = BeautifulSoup(res.text, "html.parser")
    items = soup.find_all("a")

    sent = 0

    for item in items:

        if sent >= 5:
            break

        img = item.find("img")
        if not img:
            continue

        title = item.get("title") or "جديد"
        link = item.get("href")
        img_url = img.get("data-src") or img.get("src")

        if not link:
            continue

        # منع التكرار
        cur.execute("SELECT 1 FROM messages WHERE message=%s", (link,))
        if cur.fetchone():
            break

        genres, country = get_series_details(scraper, link)
        genre_text = "، ".join(genres) if genres else "غير محدد"
        country_text = country if country else "غير محدد"

        msg = (
            f"📺 {title}\n"
            f"🎭 النوع: {genre_text}\n"
            f"🌍 الدولة: {country_text}\n"
            f"🔥 متاح الآن في الاستراحة!"
        )

        send_to_whatsapp(img_url, msg)

        cur.execute(
            "INSERT INTO messages(phone,message,sender,msg_time) VALUES('system',%s,'system',%s)",
            (link, datetime.now().strftime("%H:%M"))
        )

        conn.commit()
        sent += 1

    cur.close()
    conn.close()


# ================= ROUTES =================
@app.route("/")
def home():
    return "Flask + WA Bridge OK"


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
    cur.close()
    conn.close()
    return "cleared"


@app.route("/api/send_test")
def test():
    send_to_whatsapp(
        "https://i.imgur.com/example.jpg",
        "تجربة إرسال"
    )
    return "sent"


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)
