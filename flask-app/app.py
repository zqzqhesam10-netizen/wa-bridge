from flask import Flask, jsonify
import requests, os, psycopg2
from datetime import datetime
from urllib.parse import urljoin
import json
from bs4 import BeautifulSoup
import cloudscraper

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")

NODE_URL = os.environ.get("NODE_URL", "").strip()
GROUP_ID = os.environ.get("GROUP_ID", "120363429067223078@g.us")
SOURCE_URL = os.environ.get("SOURCE_URL", "https://tuktukcinemas.com/recent/").strip()


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
    if not NODE_URL:
        return False, "NODE_URL environment variable is not configured"
    if not image:
        return False, "image URL is empty"
    if not GROUP_ID or not GROUP_ID.endswith("@g.us"):
        return False, "GROUP_ID is missing or is not a WhatsApp group JID"

    try:
        response = requests.post(
            NODE_URL,
            json={
                "groupId": GROUP_ID,
                "image": image,
                "caption": caption
            },
            timeout=60
        )
        if not response.ok:
            detail = f"Node send endpoint returned HTTP {response.status_code}: {response.text[:300]}"
            print("SEND ERROR:", detail, flush=True)
            return False, detail

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if payload.get("ok") is not True:
            detail = f"Node send endpoint did not confirm acceptance: {str(payload)[:300]}"
            print("SEND ERROR:", detail, flush=True)
            return False, detail

        message_id = payload.get("messageId")
        remote_jid = payload.get("remoteJid")
        print(
            f"SEND ACCEPTED: group={GROUP_ID} message_id={message_id or 'unknown'} remote_jid={remote_jid or 'unknown'}",
            flush=True
        )
        return True, {"message_id": message_id, "remote_jid": remote_jid}

    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        print("SEND ERROR:", detail, flush=True)
        return False, detail


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
    result = {
        "status": "starting",
        "source": SOURCE_URL,
        "final_url": None,
        "response_content_type": None,
        "response_server": None,
        "response_snippet": None,
        "group_id": GROUP_ID,
        "node_url_configured": bool(NODE_URL),
        "source_http_status": None,
        "anchors_found": 0,
        "candidates_found": 0,
        "attempts": 0,
        "sent": 0,
        "failed": 0,
        "skipped_missing_link": 0,
        "skipped_missing_image": 0,
        "duplicate_stop": False,
        "send_results": [],
        "errors": []
    }
    conn = None
    cur = None

    try:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL environment variable is not configured")
        if not NODE_URL:
            raise RuntimeError("NODE_URL environment variable is not configured")

        conn = db()
        cur = conn.cursor()
        scraper = cloudscraper.create_scraper()
        response = scraper.get(result["source"], timeout=30)
        result["source_http_status"] = response.status_code
        result["final_url"] = response.url
        result["response_content_type"] = response.headers.get("Content-Type")
        result["response_server"] = response.headers.get("Server")
        response.encoding = "utf-8"

        if not response.ok:
            error_text = BeautifulSoup(response.text[:5000], "html.parser").get_text(" ", strip=True)
            result["response_snippet"] = error_text[:300]
            print(
                "SOURCE HTTP ERROR: "
                + json.dumps({
                    "status": response.status_code,
                    "requested_url": result["source"],
                    "final_url": response.url,
                    "content_type": result["response_content_type"],
                    "server": result["response_server"],
                    "body_excerpt": result["response_snippet"]
                }, ensure_ascii=False),
                flush=True
            )
        response.raise_for_status()

        body_lower = response.text.lower()
        challenge_markers = (
            "just a moment...",
            "checking your browser",
            "cf-chl-",
            "challenge-platform"
        )
        if any(marker in body_lower for marker in challenge_markers):
            raise RuntimeError("TukTukHD returned a browser/Cloudflare challenge page, not the recent-items page")

        soup = BeautifulSoup(response.text, "html.parser")
        items = soup.find_all("a")
        result["anchors_found"] = len(items)

        if not items:
            result["status"] = "no_candidates"
            result["errors"].append("The source page returned no links; inspect the response HTML/source availability.")

        for item in items:
            if result["attempts"] >= 5:
                break

            img = item.find("img")
            if not img:
                continue

            raw_link = item.get("href")
            if not raw_link:
                result["skipped_missing_link"] += 1
                continue

            link = urljoin(result["source"], raw_link.strip())
            raw_image = (
                img.get("data-src")
                or img.get("data-lazy-src")
                or img.get("data-original")
                or img.get("src")
            )
            if not raw_image:
                result["skipped_missing_image"] += 1
                continue

            image_url = urljoin(link, raw_image.strip())
            result["candidates_found"] += 1

            cur.execute("SELECT 1 FROM messages WHERE message=%s LIMIT 1", (link,))
            if cur.fetchone():
                result["duplicate_stop"] = True
                break

            title = (item.get("title") or img.get("alt") or "جديد").strip()
            genres, country = get_series_details(scraper, link)
            genre_text = "، ".join(genres) if genres else "غير محدد"
            country_text = country if country else "غير محدد"
            caption = (
                f"📺 {title}\n"
                f"🎭 النوع: {genre_text}\n"
                f"🌍 الدولة: {country_text}\n"
                f"🔥 متاح الآن في الاستراحة!"
            )

            result["attempts"] += 1
            ok, details = send_to_whatsapp(image_url, caption)
            result["send_results"].append({
                "title": title,
                "accepted": bool(ok),
                "details": details
            })

            if not ok:
                result["failed"] += 1
                continue

            cur.execute(
                "INSERT INTO messages(phone,message,sender,msg_time) VALUES('system',%s,'system',%s)",
                (link, datetime.now().strftime("%H:%M"))
            )
            conn.commit()
            result["sent"] += 1

        if result["failed"]:
            result["status"] = "partial" if result["sent"] else "send_failed"
        elif result["sent"]:
            result["status"] = "done"
        elif result["status"] != "no_candidates":
            result["status"] = "no_new_items" if result["duplicate_stop"] else "no_candidates"

    except Exception as e:
        result["status"] = "error"
        result["errors"].append(f"{type(e).__name__}: {e}")
        print("UPDATE CHECK ERROR:", result["errors"][-1], flush=True)

    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    print("UPDATE CHECK RESULT: " + json.dumps(result, ensure_ascii=False, default=str), flush=True)
    return result


# ================= ROUTES =================
@app.route("/")
def home():
    return "Flask + WA Bridge OK"


@app.route("/api/check_updates")
def force_check():
    result = check_updates()
    status_code = 502 if result["status"] in ("error", "send_failed") else 200
    return jsonify(result), status_code


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
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))
