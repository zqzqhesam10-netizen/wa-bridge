from flask import Flask, jsonify
import requests, os, psycopg2
from datetime import datetime
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
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


# ================= READ-ONLY SOURCE DIAGNOSTICS =================
def _probe_public_source(url):
    """Test a fixed public URL without CloudScraper and without sending messages."""
    details = {
        "url": url,
        "final_url": None,
        "http_status": None,
        "content_type": None,
        "server": None,
        "challenge_detected": False,
        "format_guess": "unknown",
        "publicly_reachable": False,
        "usable_feed_or_json_candidate": False,
        "body_excerpt": None,
        "error": None
    }
    try:
        response = requests.get(
            url,
            timeout=8,
            allow_redirects=True,
            headers={
                "User-Agent": "WA-Bridge-Source-Diagnostic/1.0",
                "Accept": "application/rss+xml, application/atom+xml, application/json, application/xml, text/xml, text/html;q=0.8, */*;q=0.5"
            }
        )
        details["final_url"] = response.url
        details["http_status"] = response.status_code
        details["content_type"] = response.headers.get("Content-Type")
        details["server"] = response.headers.get("Server")
        body = response.text or ""
        lower = body[:5000].lower()
        challenge_markers = (
            "just a moment...",
            "enable javascript and cookies to continue",
            "checking your browser",
            "cf-chl-",
            "challenge-platform"
        )
        details["challenge_detected"] = any(token in lower for token in challenge_markers)
        details["body_excerpt"] = body[:180].replace("\\r", " ").replace("\\n", " ").strip()
        content_type = (details["content_type"] or "").lower()
        body_start = body.lstrip()[:200].lower()

        if details["challenge_detected"]:
            details["format_guess"] = "cloudflare_challenge"
        elif "rss+xml" in content_type or "atom+xml" in content_type or "<rss" in body_start or "<feed" in body_start or "<rdf:rdf" in body_start:
            details["format_guess"] = "rss_or_atom"
        elif "json" in content_type or body_start.startswith("[") or body_start.startswith("{"):
            details["format_guess"] = "json"
        elif "xml" in content_type or body_start.startswith("<?xml"):
            details["format_guess"] = "xml"
        elif "html" in content_type or "<html" in body_start or "<!doctype html" in body_start:
            details["format_guess"] = "html"

        details["publicly_reachable"] = 200 <= response.status_code < 300 and not details["challenge_detected"]
        details["usable_feed_or_json_candidate"] = (
            details["publicly_reachable"] and details["format_guess"] in ("rss_or_atom", "json")
        )
        response.close()
    except Exception as exc:
        details["error"] = f"{type(exc).__name__}: {exc}"
    return details


def diagnose_public_sources():
    """Probe common public feed/API endpoints; this function never sends WhatsApp messages."""
    parsed = urlparse(SOURCE_URL)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {
            "status": "invalid_source_url",
            "source": SOURCE_URL,
            "message_sending": False,
            "probes": [],
            "error": "SOURCE_URL must be an absolute HTTP(S) URL"
        }

    origin = f"{parsed.scheme}://{parsed.netloc}"
    urls = [
        SOURCE_URL,
        urljoin(origin, "/feed/"),
        urljoin(origin, "/recent/feed/"),
        urljoin(origin, "/wp-json/wp/v2/posts?per_page=3&_embed=1"),
        urljoin(origin, "/wp-sitemap.xml")
    ]
    unique_urls = list(dict.fromkeys(urls))
    probes = [None] * len(unique_urls)
    with ThreadPoolExecutor(max_workers=min(5, len(unique_urls))) as pool:
        futures = {pool.submit(_probe_public_source, url): index for index, url in enumerate(unique_urls)}
        for future in as_completed(futures):
            probes[futures[future]] = future.result()

    usable = [p["url"] for p in probes if p and p["usable_feed_or_json_candidate"]]
    return {
        "status": "diagnostic_complete",
        "source": SOURCE_URL,
        "message_sending": False,
        "probes": probes,
        "usable_feed_or_json_candidates": usable,
        "next_step": (
            "A public RSS/Atom or JSON endpoint is accessible; select and implement it as a source."
            if usable else
            "No unchallenged public RSS/Atom or JSON endpoint was confirmed. Do not bypass Cloudflare; request an official feed/API or ask the site administrator to permit this service."
        )
    }


# ================= ROUTES =================
@app.route("/")
def home():
    return "Flask + WA Bridge OK"


@app.route("/api/source_diagnostic")
def source_diagnostic():
    # Read-only: probes public endpoints and never sends WhatsApp messages.
    return jsonify(diagnose_public_sources()), 200


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
