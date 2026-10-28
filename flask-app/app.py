from flask import Flask, jsonify, request
import requests, os, psycopg2
import re, threading, time
from datetime import datetime
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from bs4 import BeautifulSoup
import cloudscraper

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")

NODE_URL = os.environ.get("NODE_URL", "").strip()
GROUP_ID = os.environ.get("GROUP_ID", "120363429067223078@g.us")
APP_CODE_VERSION = "2026-10-29-metadata-auto-poll-5"
SOURCE_URL = os.environ.get("SOURCE_URL", "https://tuktukcinemas.com/recent/feed/").strip()

# Canonicalize a legacy environment value that still points at the Cloudflare-protected HTML page.
# Keep the deployed app working even if Hostless still has SOURCE_URL=https://tuktukcinemas.com/recent/.
_source_parts = urlparse(SOURCE_URL)
if (
    (_source_parts.hostname or "").lower() in {
        "tuktukcinemas.com", "www.tuktukcinemas.com",
        "tuktukhd.com", "www.tuktukhd.com"
    }
    and _source_parts.path.rstrip("/").lower() == "/recent"
):
    SOURCE_URL = _source_parts._replace(
        scheme="https",
        netloc="tuktukcinemas.com",
        path="/recent/feed/",
        query="",
        fragment=""
    ).geturl()


AUTO_POLL_INTERVAL_SECONDS = 30
AUTO_POLL_MAX_ITEMS = 5
_CHECK_LOCK = threading.Lock()
_POLL_STATE = {
    "enabled": False,
    "interval_seconds": AUTO_POLL_INTERVAL_SECONDS,
    "max_items_per_check": AUTO_POLL_MAX_ITEMS,
    "last_started_at": None,
    "last_completed_at": None,
    "last_status": None,
    "last_sent": 0,
    "last_error": None
}



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
    """Fetch an individual public title page and extract year/genre/country metadata."""
    try:
        full_url = urljoin("https://tuktukcinemas.com/", url)
        response = scraper.get(full_url, timeout=18, allow_redirects=True)
        status = f"HTTP_{response.status_code}"
        response.encoding = "utf-8"
        if not response.ok:
            response.close()
            print("DETAIL ERROR:", full_url, status, flush=True)
            return [], "", "", "", status

        body = response.text or ""
        body_lower = body[:5000].lower()
        if any(marker in body_lower for marker in (
            "just a moment...",
            "enable javascript and cookies to continue",
            "checking your browser",
            "cf-chl-",
            "challenge-platform"
        )):
            response.close()
            return [], "", "", "", "CLOUDFLARE_CHALLENGE"

        genres, country, year = _extract_series_metadata(body, url=full_url)
        response.close()
        return genres, country, year, body, status
    except Exception as exc:
        print("DETAIL ERROR:", url, f"{type(exc).__name__}: {exc}", flush=True)
        return [], "", "", "", f"{type(exc).__name__}: {exc}"


def _local_name(tag):
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _child_text(parent, wanted_name):
    for child in list(parent):
        if _local_name(child.tag) == wanted_name:
            return "".join(child.itertext()).strip()
    return ""


def _normalized_url_key(url):
    if not url:
        return ""
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return url.strip().rstrip("/").lower()
    normalized = parsed._replace(fragment="")
    return normalized.geturl().rstrip("/").lower()


def _image_from_html(markup, base_url):
    if not markup:
        return None
    soup = BeautifulSoup(markup, "html.parser")
    for node in soup.find_all(["img", "source"]):
        for attr in ("data-src", "data-lazy-src", "data-original", "data-original-src", "src"):
            candidate = node.get(attr)
            if candidate and candidate.strip() and not candidate.strip().startswith("data:"):
                return urljoin(base_url, candidate.strip())
        srcset = node.get("srcset")
        if srcset:
            candidate = srcset.split(",")[0].strip().split(" ")[0]
            if candidate and not candidate.startswith("data:"):
                return urljoin(base_url, candidate)
    return None


def _rss_image(item, base_url):
    # Prefer per-item Media RSS thumbnails/content, then image enclosures.
    for element in item.iter():
        local = _local_name(element.tag).lower()
        image_url = (element.attrib.get("url") or "").strip()
        if not image_url:
            continue
        media_type = (element.attrib.get("type") or "").lower()
        medium = (element.attrib.get("medium") or "").lower()
        is_media_rss = "search.yahoo.com/mrss" in element.tag.lower()
        if local == "thumbnail":
            return urljoin(base_url, image_url)
        if local == "content" and is_media_rss:
            if medium == "video" or media_type.startswith("video/"):
                continue
            if medium == "image" or media_type.startswith("image/") or not media_type:
                return urljoin(base_url, image_url)
        if local == "enclosure" and (media_type.startswith("image/") or not media_type):
            return urljoin(base_url, image_url)
    return None


def _parse_source_response(response, source_url):
    """Normalize RSS/Atom, public WordPress REST JSON, or legacy HTML into source items."""
    response.raise_for_status()
    body = response.text or ""
    lower = body[:5000].lower()
    challenge_markers = (
        "just a moment...",
        "enable javascript and cookies to continue",
        "checking your browser",
        "cf-chl-",
        "challenge-platform"
    )
    if any(marker in lower for marker in challenge_markers):
        raise RuntimeError("Source returned a browser/Cloudflare challenge page, not content.")

    content_type = (response.headers.get("Content-Type") or "").lower()
    start = body.lstrip()[:200].lower()
    items = []

    if (
        "rss+xml" in content_type
        or "atom+xml" in content_type
        or "xml" in content_type
        or start.startswith("<?xml")
        or start.startswith("<rss")
        or start.startswith("<feed")
    ):
        root = ET.fromstring(response.content)
        root_type = _local_name(root.tag).lower()
        if root_type == "rss":
            source_entries = [
                node for node in root.iter()
                if _local_name(node.tag).lower() == "item"
            ]
            source_format = "rss"
        elif root_type == "feed":
            source_entries = [
                node for node in root.iter()
                if _local_name(node.tag).lower() == "entry"
            ]
            source_format = "atom"
        else:
            raise RuntimeError("The source returned XML, but it is not an RSS/Atom feed.")

        for entry in source_entries:
            title = _child_text(entry, "title")
            link = _child_text(entry, "link")
            if not link:
                # Atom uses <link href="..."> rather than text content.
                for element in entry:
                    if _local_name(element.tag).lower() == "link":
                        rel = (element.attrib.get("rel") or "alternate").lower()
                        if rel == "alternate" and element.attrib.get("href"):
                            link = element.attrib["href"].strip()
                            break
            if not link:
                link = _child_text(entry, "guid") or _child_text(entry, "id")
            link = urljoin(source_url, link.strip()) if link else ""

            description = _child_text(entry, "description")
            encoded = _child_text(entry, "encoded")
            content = _child_text(entry, "content")
            metadata_html = encoded or content or description
            image_url = _rss_image(entry, source_url) or _image_from_html(metadata_html, link or source_url)
            clean_title = BeautifulSoup(title, "html.parser").get_text(" ", strip=True)
            items.append({
                "title": clean_title or "جديد",
                "link": link,
                "image": image_url,
                "html": metadata_html,
                "source_format": source_format
            })
        return items, source_format

    if "json" in content_type or start.startswith("[") or start.startswith("{"):
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("The JSON source response is not a list of posts.")
        for post in payload:
            if not isinstance(post, dict):
                continue
            raw_title = post.get("title", "")
            title = raw_title.get("rendered", "") if isinstance(raw_title, dict) else str(raw_title or "")
            raw_content = post.get("content", "")
            content_html = raw_content.get("rendered", "") if isinstance(raw_content, dict) else str(raw_content or "")
            raw_excerpt = post.get("excerpt", "")
            excerpt_html = raw_excerpt.get("rendered", "") if isinstance(raw_excerpt, dict) else str(raw_excerpt or "")
            embedded = post.get("_embedded") or {}
            media = embedded.get("wp:featuredmedia") or []
            image_url = None
            if media and isinstance(media[0], dict):
                image_url = media[0].get("source_url")
            link = str(post.get("link") or post.get("guid", {}).get("rendered", "") or "")
            items.append({
                "title": BeautifulSoup(title, "html.parser").get_text(" ", strip=True) or "جديد",
                "link": link,
                "image": image_url or _image_from_html(content_html or excerpt_html, link or source_url),
                "html": content_html or excerpt_html,
                "source_format": "json",
                "post_id": post.get("id")
            })
        return items, "json"

    # Compatibility fallback for an unchallenged HTML listing if SOURCE_URL is deliberately configured to one.
    if "html" in content_type or "<html" in start or "<!doctype html" in start:
        soup = BeautifulSoup(body, "html.parser")
        for anchor in soup.find_all("a"):
            img = anchor.find("img")
            if not img:
                continue
            raw_link = anchor.get("href")
            if not raw_link:
                continue
            link = urljoin(source_url, raw_link.strip())
            image_url = _image_from_html(str(img), link)
            if not image_url:
                continue
            title = anchor.get("title") or img.get("alt") or anchor.get_text(" ", strip=True) or "جديد"
            items.append({
                "title": BeautifulSoup(title, "html.parser").get_text(" ", strip=True),
                "link": link,
                "image": image_url,
                "html": str(anchor),
                "source_format": "html"
            })
        return items, "html"

    raise RuntimeError(f"Unsupported source content type: {content_type or 'unknown'}")


def _enrich_missing_images(items, scraper, source_url):
    """Use the public WordPress API only to enrich entries already present in the selected feed."""
    missing = [item for item in items if item.get("link") and not item.get("image")]
    if not missing:
        return
    parsed = urlparse(source_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return
    api_url = f"{parsed.scheme}://{parsed.netloc}/wp-json/wp/v2/posts?per_page=100&_embed=1"
    try:
        response = scraper.get(api_url, timeout=12)
        if not response.ok:
            return
        posts = response.json()
        if not isinstance(posts, list):
            return
        by_url = {}
        by_title = {}
        for post in posts:
            if not isinstance(post, dict):
                continue
            post_url = str(post.get("link") or "")
            guid = post.get("guid") or {}
            guid_url = guid.get("rendered", "") if isinstance(guid, dict) else str(guid or "")
            embedded = post.get("_embedded") or {}
            media = embedded.get("wp:featuredmedia") or []
            image_url = media[0].get("source_url") if media and isinstance(media[0], dict) else None
            raw_title = post.get("title") or {}
            title_html = raw_title.get("rendered", "") if isinstance(raw_title, dict) else str(raw_title or "")
            title = BeautifulSoup(title_html, "html.parser").get_text(" ", strip=True).casefold()
            raw_content = post.get("content") or {}
            content_html = raw_content.get("rendered", "") if isinstance(raw_content, dict) else str(raw_content or "")
            if image_url:
                for key in (_normalized_url_key(post_url), _normalized_url_key(guid_url)):
                    if key:
                        by_url[key] = (image_url, content_html)
                if title:
                    by_title[title] = (image_url, content_html)
        for item in missing:
            match = by_url.get(_normalized_url_key(item.get("link", "")))
            if not match:
                title_key = str(item.get("title") or "").strip().casefold()
                match = by_title.get(title_key)
            if match:
                item["image"] = match[0]
                if not item.get("html"):
                    item["html"] = match[1]
        response.close()
    except Exception as exc:
        print("SOURCE IMAGE ENRICHMENT WARNING:", f"{type(exc).__name__}: {exc}", flush=True)


def _latin_digits(value):
    return str(value or "").translate(str.maketrans(
        "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
        "01234567890123456789"
    ))


_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_YEAR_LABEL_RE = re.compile(
    r"(?:موعد\s+الصدور|موعد\s+الإصدار|سنة\s+(?:الإنتاج|الانتاج|الإصدار|الاصدار)|"
    r"تاريخ\s+(?:الإنتاج|الانتاج)|release\s+year|release\s+date|year)"
    r"\s*[:：\-]?\s*(?:\([^)]*\)\s*)?((?:19|20)\d{2})",
    re.IGNORECASE
)


def _extract_year(title="", url="", markup=""):
    title_text = _latin_digits(BeautifulSoup(str(title or ""), "html.parser").get_text(" ", strip=True))
    match = _YEAR_RE.search(title_text)
    if match:
        return match.group(0)

    if markup:
        text = _latin_digits(BeautifulSoup(str(markup), "html.parser").get_text(" ", strip=True))
        match = _YEAR_LABEL_RE.search(text)
        if match:
            return match.group(1)

    path_text = _latin_digits(urlparse(str(url or "")).path.replace("-", " ").replace("_", " "))
    match = _YEAR_RE.search(path_text)
    return match.group(0) if match else ""


def _first_labeled_value(soup, labels):
    label_pattern = re.compile(
        r"^\s*(?:" + "|".join(labels) + r")\s*[:：\-]?\s*(.*?)\s*$",
        re.IGNORECASE
    )
    # Inspect likely field containers first, then more general containers.
    for tag_name in ("li", "tr", "dt", "dd", "p", "span", "div"):
        for node in soup.find_all(tag_name):
            text = " ".join(node.get_text(" ", strip=True).split())
            if not text or len(text) > 420:
                continue
            match = label_pattern.match(text)
            if not match:
                continue

            value = (match.group(1) or "").strip(" \t\r\n:：-–—")
            links = [
                " ".join(a.get_text(" ", strip=True).split())
                for a in node.find_all("a")
                if " ".join(a.get_text(" ", strip=True).split())
            ]
            # When the source links each genre/country value, prefer those clean labels.
            if links:
                return list(dict.fromkeys(links)), value
            return [], value
    return [], ""


def _extract_series_metadata(markup, title="", url=""):
    genres = []
    country = ""
    if markup:
        soup = BeautifulSoup(str(markup), "html.parser")
        genre_links, genre_text = _first_labeled_value(soup, [
            r"الأنواع", r"الانواع", r"النوع", r"نوع\s+العمل", r"genres?", r"genre"
        ])
        if genre_links:
            genres = genre_links
        elif genre_text:
            # Keep a multi-word genre value together if the page does not link genres separately.
            genres = [genre_text]

        country_links, country_text = _first_labeled_value(soup, [
            r"دولة\s+المسلسل", r"دولة\s+الفيلم", r"دولة\s+العمل",
            r"بلد\s+الإنتاج", r"بلد\s+الانتاج", r"الدولة", r"البلد",
            r"country(?:\s+of\s+origin)?"
        ])
        if country_links:
            country = country_links[0]
        elif country_text:
            country = country_text

    year = _extract_year(title=title, url=url, markup=markup)
    return genres, country, year


def check_updates(max_items=5):
    result = {
        "status": "starting",
        "code_version": APP_CODE_VERSION,
        "source": SOURCE_URL,
        "max_items": max(1, min(5, int(max_items))),
        "effective_source_url": SOURCE_URL,
        "fallback_source_url": None,
        "fallback_error": None,
        "source_format": None,
        "final_url": None,
        "response_content_type": None,
        "response_server": None,
        "response_snippet": None,
        "group_id": GROUP_ID,
        "node_url_configured": bool(NODE_URL),
        "source_http_status": None,
        "anchors_found": 0,
        "items_found": 0,
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

        items, source_format = _parse_source_response(response, result["source"])
        effective_source_url = result["source"]

        # Some WordPress custom feeds return a valid empty RSS document. When that
        # happens, fall back to the site's publicly reachable posts REST endpoint.
        if not items and source_format in ("rss", "atom"):
            parsed_source = urlparse(result["source"])
            if parsed_source.scheme in ("http", "https") and parsed_source.netloc:
                fallback_url = urljoin(
                    f"{parsed_source.scheme}://{parsed_source.netloc}",
                    "/wp-json/wp/v2/posts?per_page=20&orderby=date&order=desc&_embed=1"
                )
                result["fallback_source_url"] = fallback_url
                effective_source_url = fallback_url
                result["effective_source_url"] = fallback_url
                response.close()
                fallback_response = scraper.get(fallback_url, timeout=20)
                fallback_response.encoding = "utf-8"
                fallback_items, fallback_format = _parse_source_response(fallback_response, fallback_url)
                if fallback_items:
                    response = fallback_response
                    items = fallback_items
                    source_format = fallback_format
                    effective_source_url = fallback_url
                    result["effective_source_url"] = fallback_url
                    result["source_http_status"] = fallback_response.status_code
                    result["final_url"] = fallback_response.url
                    result["response_content_type"] = fallback_response.headers.get("Content-Type")
                    result["response_server"] = fallback_response.headers.get("Server")
                else:
                    fallback_response.close()
                    result["effective_source_url"] = fallback_url
                    result["fallback_error"] = "The public WordPress API returned no parseable posts."

        result["effective_source_url"] = effective_source_url
        result["source_format"] = source_format
        result["items_found"] = len(items)
        result["anchors_found"] = len(items)
        if any(not item.get("image") for item in items):
            _enrich_missing_images(items, scraper, effective_source_url)
        response.close()

        if not items:
            result["status"] = "no_candidates"
            result["errors"].append("The source was reachable, but the selected feed contained no parseable items.")
        else:
            for item in items:
                if result["attempts"] >= result["max_items"]:
                    break

                link = (item.get("link") or "").strip()
                if not link:
                    result["skipped_missing_link"] += 1
                    continue

                # Feed entries are newest-first: stop at the first known link before checking image availability.
                cur.execute("SELECT 1 FROM messages WHERE message=%s LIMIT 1", (link,))
                if cur.fetchone():
                    result["duplicate_stop"] = True
                    break

                image_url = (item.get("image") or "").strip()
                if not image_url:
                    result["skipped_missing_image"] += 1
                    continue

                result["candidates_found"] += 1
                title = (item.get("title") or "جديد").strip()
                metadata_html = item.get("html") or ""
                genres, country, year = _extract_series_metadata(
                    metadata_html, title=title, url=link
                )
                detail_status = "not_needed"
                if not genres or not country or not year:
                    detail_genres, detail_country, detail_year, detail_html, detail_status = get_series_details(
                        scraper, link
                    )
                    genres = genres or detail_genres
                    country = country or detail_country
                    year = year or detail_year
                    if detail_html:
                        metadata_html = detail_html

                year = year or _extract_year(title=title, url=link, markup=metadata_html)
                genre_text = "، ".join(genres) if genres else "غير محدد"
                country_text = country if country else "غير محددة"
                year_text = year if year else "غير محددة"
                caption = (
                    f"📺 {title}\n"
                    f"📅 السنة: {year_text}\n"
                    f"🎭 النوع: {genre_text}\n"
                    f"🌍 الدولة: {country_text}\n"
                    f"🔥 متاح الآن في الاستراحة!"
                )

                result["attempts"] += 1
                ok, details = send_to_whatsapp(image_url, caption)
                result["send_results"].append({
                    "title": title,
                    "link": link,
                    "year": year_text,
                    "genres": genres,
                    "country": country_text,
                    "metadata_page_status": detail_status,
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
        elif result["status"] not in ("no_candidates", "error"):
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
        details["body_excerpt"] = " ".join(body[:180].split())
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
    """Probe common public feed/API endpoints and inspect source items without sending messages."""
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
    source_inspection = {
        "url": SOURCE_URL,
        "effective_url": SOURCE_URL,
        "fallback_used": False,
        "format": None,
        "http_status": None,
        "items_found": 0,
        "items_with_images": 0,
        "sample_items": [],
        "error": None
    }
    try:
        response = requests.get(
            SOURCE_URL,
            timeout=12,
            allow_redirects=True,
            headers={
                "User-Agent": "WA-Bridge-Source-Diagnostic/1.0",
                "Accept": "application/rss+xml, application/atom+xml, application/json, application/xml, text/xml, */*;q=0.8"
            }
        )
        source_inspection["http_status"] = response.status_code
        response.encoding = "utf-8"
        items, source_format = _parse_source_response(response, SOURCE_URL)

        if not items and source_format in ("rss", "atom"):
            parsed_source = urlparse(SOURCE_URL)
            if parsed_source.scheme in ("http", "https") and parsed_source.netloc:
                fallback_url = urljoin(
                    f"{parsed_source.scheme}://{parsed_source.netloc}",
                    "/wp-json/wp/v2/posts?per_page=20&orderby=date&order=desc&_embed=1"
                )
                response.close()
                source_inspection["fallback_used"] = True
                source_inspection["effective_url"] = fallback_url
                response = requests.get(
                    fallback_url,
                    timeout=15,
                    allow_redirects=True,
                    headers={
                        "User-Agent": "WA-Bridge-Source-Diagnostic/1.0",
                        "Accept": "application/json"
                    }
                )
                response.encoding = "utf-8"
                source_inspection["http_status"] = response.status_code
                items, source_format = _parse_source_response(response, fallback_url)

        source_inspection["format"] = source_format
        source_inspection["items_found"] = len(items)
        if any(not item.get("image") for item in items):
            _enrich_missing_images(items, cloudscraper.create_scraper(), source_inspection["effective_url"])
        source_inspection["items_with_images"] = sum(1 for item in items if item.get("image"))
        source_inspection["sample_items"] = []
        diagnostic_scraper = cloudscraper.create_scraper()
        detail_checks = 0
        for item in items[:8]:
            item_title = (item.get("title") or "جديد").strip()
            item_link = (item.get("link") or "").strip()
            item_html = item.get("html") or ""
            genres, country, year = _extract_series_metadata(
                item_html, title=item_title, url=item_link
            )
            detail_status = "not_needed"
            if item_link and (not genres or not country or not year) and detail_checks < 3:
                detail_genres, detail_country, detail_year, detail_html, detail_status = get_series_details(
                    diagnostic_scraper, item_link
                )
                detail_checks += 1
                genres = genres or detail_genres
                country = country or detail_country
                year = year or detail_year
                if detail_html:
                    item_html = detail_html
            year = year or _extract_year(title=item_title, url=item_link, markup=item_html)
            source_inspection["sample_items"].append({
                "title": item_title,
                "link": item_link,
                "has_image": bool(item.get("image")),
                "image_host": urlparse(item["image"]).hostname if item.get("image") else None,
                "year": year or None,
                "genres": genres,
                "country": country or None,
                "metadata_page_status": detail_status
            })
        source_inspection["items_with_metadata_in_sample"] = sum(
            1 for item in source_inspection["sample_items"]
            if item.get("year") and item.get("genres") and item.get("country")
        )
        response.close()
    except Exception as exc:
        source_inspection["error"] = f"{type(exc).__name__}: {exc}"

    if source_inspection["items_found"] and source_inspection["items_with_images"]:
        next_step = (
            "The source parser found feed items and image URLs. This diagnostic is read-only. "
            "Call /api/check_updates only once to test delivery; it may send up to 5 new items."
        )
    elif source_inspection["items_found"]:
        next_step = (
            "The source parser found items but no image URLs. Do not call /api/check_updates yet; "
            "inspect the sample items and image fields first."
        )
    elif usable:
        next_step = (
            "The configured RSS feed was empty. The new code falls back to the public WordPress posts API. "
            "Inspect source_inspection.effective_url and sample_items before testing delivery."
        )
    else:
        next_step = (
            "No unchallenged public RSS/Atom or JSON endpoint was confirmed. Do not bypass Cloudflare; "
            "request an official feed/API or ask the site administrator to permit this service."
        )

    return {
        "status": "diagnostic_complete",
        "code_version": APP_CODE_VERSION,
        "source": SOURCE_URL,
        "message_sending": False,
        "probes": probes,
        "usable_feed_or_json_candidates": usable,
        "source_inspection": source_inspection,
        "automatic_polling": dict(_POLL_STATE),
        "next_step": next_step
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
    raw_limit = request.args.get("limit", "5")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return jsonify({
            "status": "invalid_limit",
            "error": "limit must be an integer from 1 to 5",
            "allowed_limits": [1, 2, 3, 4, 5],
            "message_sending": False
        }), 400
    if limit < 1 or limit > 5:
        return jsonify({
            "status": "invalid_limit",
            "error": "limit must be an integer from 1 to 5",
            "allowed_limits": [1, 2, 3, 4, 5],
            "message_sending": False
        }), 400
    if not _CHECK_LOCK.acquire(blocking=False):
        return jsonify({
            "status": "check_in_progress",
            "message_sending": False,
            "message": "A scheduled or manual update check is already running."
        }), 409
    try:
        result = check_updates(max_items=limit)
    finally:
        _CHECK_LOCK.release()
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


def _automatic_update_poller():
    # Allow the companion Node process and WhatsApp connection to settle after deployment.
    time.sleep(10)
    print(
        f"AUTO POLLER STARTED: interval={AUTO_POLL_INTERVAL_SECONDS}s max_items={AUTO_POLL_MAX_ITEMS}",
        flush=True
    )
    while True:
        cycle_started = time.monotonic()
        if _CHECK_LOCK.acquire(blocking=False):
            try:
                _POLL_STATE["last_started_at"] = datetime.now().isoformat(timespec="seconds")
                result = check_updates(max_items=AUTO_POLL_MAX_ITEMS)
                _POLL_STATE["last_completed_at"] = datetime.now().isoformat(timespec="seconds")
                _POLL_STATE["last_status"] = result.get("status")
                _POLL_STATE["last_sent"] = result.get("sent", 0)
                _POLL_STATE["last_error"] = (result.get("errors") or [None])[0]
                print(
                    "AUTO POLL SUMMARY: " + json.dumps({
                        "code_version": APP_CODE_VERSION,
                        "status": result.get("status"),
                        "sent": result.get("sent", 0),
                        "attempts": result.get("attempts", 0),
                        "interval_seconds": AUTO_POLL_INTERVAL_SECONDS
                    }, ensure_ascii=False),
                    flush=True
                )
            except Exception as exc:
                _POLL_STATE["last_completed_at"] = datetime.now().isoformat(timespec="seconds")
                _POLL_STATE["last_status"] = "error"
                _POLL_STATE["last_error"] = f"{type(exc).__name__}: {exc}"
                print("AUTO POLL ERROR:", _POLL_STATE["last_error"], flush=True)
            finally:
                _CHECK_LOCK.release()
        else:
            print("AUTO POLL SKIPPED: another update check is still running", flush=True)

        elapsed = time.monotonic() - cycle_started
        time.sleep(max(1, AUTO_POLL_INTERVAL_SECONDS - elapsed))


def start_automatic_update_poller():
    if _POLL_STATE["enabled"]:
        return
    _POLL_STATE["enabled"] = True
    thread = threading.Thread(
        target=_automatic_update_poller,
        name="wa-bridge-auto-update-poller",
        daemon=True
    )
    thread.start()


if __name__ == "__main__":
    init_db()
    start_automatic_update_poller()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))
