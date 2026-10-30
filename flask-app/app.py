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
APP_CODE_VERSION = "2026-10-31-myseed-clear-messages-18"
MYSEED_RECENT_URL = "https://m.myseed.tv/recently/"
MYSEED_ALLOWED_HOSTS = {"m.myseed.tv", "myseed.tv", "www.myseed.tv"}
SOURCE_URL = os.environ.get("SOURCE_URL", MYSEED_RECENT_URL).strip()

# The source migration is intentional: old Hostless SOURCE_URL values for Tuktuk
# are redirected to MySeed, which has now been verified reachable from Hostless.
_source_parts = urlparse(SOURCE_URL)
_source_host = (_source_parts.hostname or "").lower()
if _source_host in {
    "tuktukcinemas.com", "www.tuktukcinemas.com",
    "tuktukhd.com", "www.tuktukhd.com"
} or _source_host in MYSEED_ALLOWED_HOSTS:
    SOURCE_URL = MYSEED_RECENT_URL


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
                "content_html": encoded or content,
                "excerpt_html": description,
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

            # Embedded WP terms expose public category/tag/taxonomy labels even when
            # the theme's single-item HTML page is blocked by Cloudflare.
            term_records = []
            for term_group in (embedded.get("wp:term") or []):
                if not isinstance(term_group, list):
                    continue
                for term in term_group:
                    if not isinstance(term, dict):
                        continue
                    term_taxonomy = str(term.get("taxonomy") or "").strip()
                    if not term_taxonomy:
                        links = term.get("_links") or {}
                        collection = links.get("collection") or []
                        href = collection[0].get("href", "") if collection and isinstance(collection[0], dict) else ""
                        match = re.search(r"/wp/v2/([^/?]+)", href)
                        term_taxonomy = match.group(1) if match else ""
                    term_name = BeautifulSoup(str(term.get("name") or ""), "html.parser").get_text(" ", strip=True)
                    if term_name:
                        term_records.append({
                            "name": term_name,
                            "slug": str(term.get("slug") or ""),
                            "taxonomy": term_taxonomy,
                            "description": BeautifulSoup(str(term.get("description") or ""), "html.parser").get_text(" ", strip=True)
                        })

            public_meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
            link = str(post.get("link") or post.get("guid", {}).get("rendered", "") or "")
            items.append({
                "title": BeautifulSoup(title, "html.parser").get_text(" ", strip=True) or "جديد",
                "link": link,
                "image": image_url or _image_from_html(content_html or excerpt_html, link or source_url),
                "html": content_html or excerpt_html,
                "content_html": content_html,
                "excerpt_html": excerpt_html,
                "source_format": "json",
                "post_id": post.get("id"),
                "slug": post.get("slug") or "",
                "date": post.get("date") or "",
                "public_meta": public_meta,
                "terms": term_records
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
    # Prefer the site's explicit release-year taxonomy URL, for example:
    # <a href="https://tuktukcinemas.com/release-year/2026/">2026</a>
    # This is parsed locally from source HTML; no external metadata lookup is made.
    if markup:
        soup = BeautifulSoup(str(markup), "html.parser")
        for anchor in soup.find_all("a", href=True):
            href_path = urlparse(str(anchor.get("href") or "")).path
            if not re.search(r"(?:^|/)release-year/", href_path, re.IGNORECASE):
                continue
            match = _YEAR_RE.search(_latin_digits(href_path))
            if match:
                return match.group(0)

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


def _metadata_scalars(value, prefix=""):
    """Yield public REST meta key/value pairs, including nested plugin metadata."""
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from _metadata_scalars(child, path)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _metadata_scalars(child, f"{prefix}[{index}]")
    elif value is not None:
        text = " ".join(str(value).split())
        if text:
            yield prefix.casefold(), text


_COUNTRY_ALIASES = [
    (("تركيا", "تركي", "تركية", "turkey", "turkish"), "تركيا"),
    (("كوريا الجنوبية", "كوري جنوبي", "كورية جنوبية", "south korea", "south korean", "korean"), "كوريا الجنوبية"),
    (("كوريا", "كوري", "كورية", "korea"), "كوريا"),
    (("الصين", "صيني", "صينية", "china", "chinese"), "الصين"),
    (("اليابان", "ياباني", "يابانية", "japan", "japanese"), "اليابان"),
    (("الهند", "هندي", "هندية", "india", "indian"), "الهند"),
    (("مصر", "مصري", "مصرية", "egypt", "egyptian"), "مصر"),
    (("الولايات المتحدة", "أمريكا", "امريكا", "أمريكي", "أمريكية", "united states", "american"), "الولايات المتحدة"),
    (("بريطانيا", "المملكة المتحدة", "بريطاني", "britain", "british", "united kingdom"), "بريطانيا"),
    (("إسبانيا", "اسبانيا", "إسباني", "اسبانى", "spain", "spanish"), "إسبانيا"),
    (("فرنسا", "فرنسي", "فرنسية", "france", "french"), "فرنسا"),
    (("ألمانيا", "المانيا", "ألماني", "germany", "german"), "ألمانيا"),
    (("تايلاند", "تايلندي", "تايلندية", "thailand", "thai"), "تايلاند"),
    (("الفلبين", "فلبيني", "فلبينية", "philippines", "filipino"), "الفلبين"),
    (("إندونيسيا", "اندونيسيا", "إندونيسي", "indonesia", "indonesian"), "إندونيسيا"),
    (("إيران", "ايران", "إيراني", "iran", "iranian"), "إيران"),
    (("باكستان", "باكستاني", "pakistan", "pakistani"), "باكستان"),
    (("روسيا", "روسي", "روسية", "russia", "russian"), "روسيا"),
    (("كندا", "كندي", "كندية", "canada", "canadian"), "كندا"),
    (("أستراليا", "استراليا", "أسترالي", "australia", "australian"), "أستراليا"),
    (("إيطاليا", "ايطاليا", "إيطالي", "italy", "italian"), "إيطاليا"),
    (("المكسيك", "مكسيكي", "mexico", "mexican"), "المكسيك"),
    (("لبنان", "لبناني", "لبنانية", "lebanon", "lebanese"), "لبنان"),
    (("سوريا", "سوري", "سورية", "syria", "syrian"), "سوريا"),
    (("الأردن", "الاردن", "أردني", "jordan", "jordanian"), "الأردن"),
    (("المغرب", "مغربي", "مغربية", "morocco", "moroccan"), "المغرب"),
    (("الكويت", "كويتي", "kuwait", "kuwaiti"), "الكويت"),
    (("السعودية", "سعودي", "سعودية", "saudi arabia", "saudi"), "السعودية")
]

_GENRE_ALIASES = [
    (("أنمي", "انمي", "الأنمي", "الانمي", "الأنميات", "الانميات", "anime"), "أنمي"),
    (("دراما", "drama"), "دراما"),
    (("كوميدي", "كوميديا", "comedy", "comedies"), "كوميديا"),
    (("أكشن", "اكشن", "action"), "أكشن"),
    (("مغامرة", "مغامرات", "adventure"), "مغامرة"),
    (("رومانسي", "رومانسية", "romance", "romantic"), "رومانسي"),
    (("رعب", "horror"), "رعب"),
    (("إثارة", "اثارة", "thriller"), "إثارة"),
    (("غموض", "mystery"), "غموض"),
    (("خيال علمي", "science fiction", "sci-fi", "scifi"), "خيال علمي"),
    (("عائلي", "عائلي", "family"), "عائلي"),
    (("جريمة", "crime"), "جريمة"),
    (("حرب", "war"), "حرب"),
    (("وثائقي", "وثائقية", "documentary"), "وثائقي"),
    (("رسوم متحركة", "animation", "animated"), "رسوم متحركة"),
    (("فانتازيا", "fantasy"), "فانتازيا"),
    (("تاريخي", "تاريخية", "history", "historical"), "تاريخي"),
    (("western", "ويسترن", "الغرب الأمريكي", "الغرب الامريكي"), "ويسترن"),
    (("tv movie", "television movie", "فيلم تلفزيوني"), "فيلم تلفزيوني")
]


def _normalize_country(value):
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    folded = text.casefold()
    for aliases, canonical in _COUNTRY_ALIASES:
        if any(alias.casefold() in folded for alias in aliases):
            return canonical
    return text


def _normalize_genre(value):
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    folded = text.casefold()
    found = []
    for aliases, canonical in _GENRE_ALIASES:
        if any(alias.casefold() in folded for alias in aliases):
            found.append(canonical)
    if found:
        return "، ".join(dict.fromkeys(found))
    return text


def _metadata_from_public_post(item):
    """Extract year, genres and country from the REST response without opening the blocked theme page."""
    title = item.get("title") or ""
    url = item.get("link") or ""
    markup = item.get("html") or ""
    genres, country, year = _extract_series_metadata(markup, title=title, url=url)

    meta_year = ""
    meta_genres = []
    meta_country = ""
    for key, value in _metadata_scalars(item.get("public_meta") or {}):
        key_norm = key.replace("_", " ").replace("-", " ").replace(".", " ")
        key_norm = re.sub(r"\[\d+\]", " ", key_norm)
        key_norm = " ".join(key_norm.split())
        # Only accept obvious public year/release-year fields, not post-published dates.
        if not meta_year and (
            re.search(r"\byear\b", key_norm)
            or "سنة" in key_norm
            or "عام الإنتاج" in key_norm
            or "عام الانتاج" in key_norm
            or "سنة الإصدار" in key_norm
            or "سنة الاصدار" in key_norm
        ):
            match = _YEAR_RE.search(_latin_digits(value))
            if match:
                meta_year = match.group(0)
        if not meta_genres and (
            "genre" in key_norm or "النوع" in key_norm or "الانواع" in key_norm or "الأنواع" in key_norm
        ):
            values = [part.strip() for part in re.split(r"[,،|/]", value) if part.strip()]
            if values:
                meta_genres = [_normalize_genre(part) for part in values]
        if not meta_country and (
            "country" in key_norm or "بلد" in key_norm or "دولة" in key_norm
        ):
            meta_country = _normalize_country(value)

    genres = genres or meta_genres
    country = country or meta_country

    terms = item.get("terms") or []
    explicit_genres = []
    explicit_country = ""
    generic_genres = []
    generic_country = ""

    for term in terms:
        if not isinstance(term, dict):
            continue
        name = " ".join(str(term.get("name") or "").split()).strip()
        taxonomy = " ".join(str(term.get("taxonomy") or "").casefold().replace("-", "_").split())
        if not name:
            continue

        if re.search(r"genre|نوع|الانواع|الأنواع", taxonomy):
            explicit_genres.append(_normalize_genre(name))
        elif re.search(r"country|countries|دولة|بلد", taxonomy):
            explicit_country = explicit_country or _normalize_country(name)

        country_guess = ""
        for aliases, canonical in _COUNTRY_ALIASES:
            if any(alias.casefold() in name.casefold() for alias in aliases):
                country_guess = canonical
                break
        if country_guess and not explicit_country:
            generic_country = generic_country or country_guess

        genre_guess = _normalize_genre(name)
        if genre_guess and genre_guess != name:
            generic_genres.append(genre_guess)
        elif any(alias.casefold() in name.casefold() for aliases, _ in _GENRE_ALIASES for alias in aliases):
            generic_genres.append(genre_guess)

    genres = genres or list(dict.fromkeys(explicit_genres or generic_genres))
    country = country or explicit_country or generic_country

    # Some public meta implementations only expose keys whose names contain "year".
    # Never substitute the WordPress post publication date for a production/release year.
    year = year or meta_year or _extract_year(title=title, url=url, markup=markup)
    return list(dict.fromkeys(x for x in genres if x)), country, year


_STORY_META_KEY_RE = re.compile(
    r"(?:plot|synopsis|storyline|story|overview|summary|description|قصة|ملخص|نبذة|عن\s+العمل)",
    re.IGNORECASE
)
_STORY_LABEL_RE = re.compile(
    r"^\s*(?:قصة(?:\s+(?:الفيلم|المسلسل|العمل|الأنمي|الانمي))?|"
    r"ملخص(?:\s+(?:الفيلم|المسلسل|العمل))?|نبذة(?:\s+عن\s+العمل)?|"
    r"القصة|the\s+story|storyline|synopsis|plot|overview|summary)"
    r"\s*[:：\-–—]?\s*(.*)$",
    re.IGNORECASE
)
_STORY_JUNK_RE = re.compile(
    r"(?:مشاهدة\s+مباشرة|شاهد\s+الآن|تحميل\s+الآن|جودة\s+(?:عالية|عاليه)|"
    r"السيرفر|سيرفر\s+\d+|اختر\s+السيرفر|روابط\s+المشاهدة|"
    r"اضغط\s+هنا|للمشاهدة|adsbygoogle|jwplayer|video-js|iframe|"
    r"عرض\s+المزيد|قد\s+يعجبك\s+أيضاً|قد\s+يعجبك\s+ايضا|"
    r"جميع\s+الحقوق\s+محفوظة)",
    re.IGNORECASE
)


def _clean_story_text(value):
    text = BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\[[^\]]{0,80}\]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _trim_story(value, max_chars=650):
    text = _clean_story_text(value)
    if not text:
        return ""
    text = re.sub(r"^(?:القصة|قصة\s+(?:الفيلم|المسلسل|العمل)|ملخص|الملخص|synopsis|plot|story)\s*[:：\-–—]?\s*",
                  "", text, flags=re.IGNORECASE).strip()
    if len(text) > max_chars:
        cut = text.rfind(" ", 0, max_chars + 1)
        if cut < int(max_chars * 0.65):
            cut = max_chars
        text = text[:cut].rstrip(" ،,؛;:-") + "…"
    return text


def _extract_story(item, max_chars=650):
    """Extract a plot only from public text; never invent a synopsis."""
    if not isinstance(item, dict):
        return "", "unavailable"

    # First look for explicitly-named public metadata keys.
    explicit_values = []
    for key, value in _metadata_scalars(item.get("public_meta") or {}):
        clean_key = re.sub(r"[_\-.]+", " ", key).strip()
        clean_key = re.sub(r"\[\d+\]", " ", clean_key)
        if not _STORY_META_KEY_RE.search(clean_key):
            continue
        if re.search(r"(?:enable|disable|hide|show|layout|color|font|player|ads?|footnotes?)", clean_key, re.I):
            continue
        if isinstance(value, str):
            text = _clean_story_text(value)
            if len(text) >= 45 and not _STORY_JUNK_RE.search(text):
                explicit_values.append((len(text), text, f"public_meta:{clean_key}"))
    if explicit_values:
        _, value, source = max(explicit_values, key=lambda entry: entry[0])
        return _trim_story(value, max_chars), source

    # Inspect the content and excerpt separately. If the text explicitly labels a
    # story/synopsis section, take that before considering generic descriptive paragraphs.
    for source_key, source_name in (
        ("content_html", "public_content"),
        ("excerpt_html", "public_excerpt"),
        ("html", "public_html")
    ):
        markup = item.get(source_key) or ""
        if not markup:
            continue
        soup = BeautifulSoup(str(markup), "html.parser")
        for node in soup.find_all(["script", "style", "noscript", "iframe", "video", "audio", "button",
                                   "nav", "footer", "header", "form", "svg", "canvas"]):
            node.decompose()

        paragraphs = []
        # Avoid nested div text being repeated: extract paragraphs and list items first.
        for node in soup.find_all(["p", "blockquote", "li"]):
            text = " ".join(node.get_text(" ", strip=True).split())
            if not text or len(text) < 45 or _STORY_JUNK_RE.search(text):
                continue
            if re.search(r"(?:الحلقة\s+\d+|الموسم\s+\d+|سنة\s+(?:الإنتاج|الانتاج)|دولة\s+(?:المسلسل|الفيلم)|الأنواع|الانواع)\s*[:：]", text):
                continue
            paragraphs.append(text)

        # Labelled paragraphs may contain the plot directly.
        for paragraph in paragraphs:
            match = _STORY_LABEL_RE.match(paragraph)
            if match:
                labelled_text = (match.group(1) or "").strip()
                if len(labelled_text) >= 35 and not _STORY_JUNK_RE.search(labelled_text):
                    return _trim_story(labelled_text, max_chars), f"{source_name}:labelled"

        # A concise excerpt is preferable to a player/technical page dump. From content,
        # select the most story-like paragraph (not navigation, taxonomy or a long shortcode).
        if paragraphs:
            preferred = []
            for text in paragraphs:
                normalized = text.casefold()
                score = len(text)
                if re.search(r"\b(?:القصة|القصة تدور|تدور أحداث|تدور القصة|يحكي الفيلم|يحكي العمل|تتناول الأحداث|تحكي القصة|بعد أن|عندما|عقب|the story|follows|after|when)\b", normalized):
                    score += 250
                if re.search(r"(?:تحميل|مشاهدة|سيرفر|جودة|الرابط|الموقع الرسمي|إعلان تشويقي)", text, re.I):
                    score -= 250
                if score > 0:
                    preferred.append((score, text))
            if preferred:
                _, story = max(preferred, key=lambda entry: entry[0])
                return _trim_story(story, max_chars), f"{source_name}:paragraph"

    return "", "unavailable"


def _infer_work_type(item):
    """Infer the broad title type from its title and public WordPress categories/tags."""
    title = " ".join(str(item.get("title") or "").split()).casefold()
    terms = item.get("terms") or []
    names = [
        " ".join(str(term.get("name") or "").split()).casefold()
        for term in terms if isinstance(term, dict) and term.get("name")
    ]
    taxonomy_text = " ".join(names)
    slug = str(item.get("slug") or urlparse(str(item.get("link") or "")).path).casefold()

    # Use anime/animation categories first to avoid classifying anime episodes as generic series.
    if (
        re.search(r"\b(?:انمي|أنمي|الانميات|الأنميات|anime)\b", title + " " + taxonomy_text)
        or "رسوم متحركة" in taxonomy_text
        or "animation" in taxonomy_text
    ):
        return "أنمي"

    if (
        re.search(r"^\s*(?:فيلم|الفيلم)\b", title)
        or re.search(r"\b(?:أفلام|افلام|فيلم|films?|movies?)\b", taxonomy_text)
    ):
        return "فيلم"

    if (
        re.search(r"^\s*(?:مسلسل|المسلسل)\b", title)
        or re.search(r"\b(?:حلقات|مسلسلات|مسلسل|series|episodes?)\b", taxonomy_text)
        or re.search(r"s\d{1,2}[-_. ]?e\d{1,3}", slug)
    ):
        return "مسلسل"

    return ""


def _get_post_publication_year(item):
    # This is the WordPress post date, not necessarily the production/release year.
    date_text = _latin_digits(str(item.get("date") or ""))
    match = _YEAR_RE.search(date_text)
    return match.group(0) if match else ""


def _extract_rating(item):
    """Read the site's displayed rating from its own HTML, without calling TMDB."""
    if not isinstance(item, dict):
        return "", "unavailable"

    # The REST API may expose body and excerpt in separate fields. Inspect only
    # markup already delivered by the source API/feed; never request the TMDB URL.
    for source_key in ("content_html", "excerpt_html", "html"):
        markup = item.get(source_key) or ""
        if not markup:
            continue
        soup = BeautifulSoup(str(markup), "html.parser")
        for anchor in soup.find_all("a", href=True):
            parsed = urlparse(str(anchor.get("href") or ""))
            host = (parsed.hostname or "").casefold().rstrip(".")
            if host.startswith("www."):
                host = host[4:]
            if host != "themoviedb.org":
                continue

            # Site markup displays the rating in a <strong>, e.g. <strong>4</strong>.
            for node in anchor.find_all("strong"):
                raw_value = _latin_digits(node.get_text(" ", strip=True)).strip()
                match = re.fullmatch(r"(?:10(?:[.,]0{1,3})?|[0-9](?:[.,][0-9]{1,3})?)", raw_value)
                if match:
                    return match.group(0).replace(",", "."), f"{source_key}:embedded_rating_anchor"

    return "", "unavailable"




def _myseed_detail_url_allowed(url):
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/").lower()
    if host not in MYSEED_ALLOWED_HOSTS or not path or path == "/recently":
        return False
    if any(part in path for part in (
        "/category/", "/tag/", "/page/", "/search/", "/author/",
        "/genre/", "/country/", "/release-year/", "/language/",
        "/quality/", "/feed/"
    )):
        return False
    if re.search(r"\.(?:jpg|jpeg|png|webp|gif|svg|css|js|xml)$", path):
        return False
    return True


def _myseed_card_rating(card):
    """Read the rating printed in the MySeed listing card; no external rating lookup."""
    # Prefer a compact element whose class explicitly describes a rating.
    for node in card.find_all(True):
        classes = " ".join(node.get("class") or []).casefold()
        if "rating" not in classes and "star" not in classes:
            continue
        node_text = _myseed_text(node, 80)
        match = re.search(r"(?<!\d)(10|[0-9])(?:[.,](\d{1,2}))?(?!\d)", node_text)
        if match:
            value = float(match.group(1) + (("." + match.group(2)) if match.group(2) else ""))
            if 0 <= value <= 10:
                return f"{value:g}", "myseed_listing_rating_element"

    # In the verified cards the rating is printed after the genre and before
    # the episode/quality marker (e.g. "Action & Adventure 7.9 الحلقة 1").
    card_text = _myseed_text(card, 3500)
    prefix = re.split(
        r"(?:الحلقة\s*\d+|الموسم\s*\d+|WEB\s*[- ]?\s*DL|BLU[\s-]?RAY|BLURAY)",
        card_text,
        maxsplit=1,
        flags=re.IGNORECASE
    )[0]
    matches = re.findall(r"(?<!\d)(10|[0-9])[.,](\d{1,2})(?!\d)", prefix)
    for whole, fraction in matches:
        value = float(f"{whole}.{fraction}")
        if 0 <= value <= 10:
            return f"{value:g}", "myseed_listing_card"
    return "", "unavailable"


def _parse_myseed_recent_response(response):
    """Parse the actual MySeed .item__contents listing cards in page order."""
    response.raise_for_status()
    body = response.text or ""
    lower = body[:12000].casefold()
    if any(marker in lower for marker in (
        "just a moment", "enable javascript and cookies to continue",
        "checking your browser", "cf-chl-", "challenge-platform",
        "access denied", "request blocked"
    )):
        raise RuntimeError("MySeed returned a browser/anti-bot challenge page.")

    soup = BeautifulSoup(body, "html.parser")
    cards = soup.select(".item__contents")
    items = []
    seen = set()
    for card in cards:
        detail_url = ""
        for anchor in card.find_all("a", href=True):
            candidate = urljoin(response.url, anchor.get("href", ""))
            if _myseed_detail_url_allowed(candidate):
                detail_url = candidate
                break
        if not detail_url or detail_url in seen:
            continue
        seen.add(detail_url)

        image_url = _image_from_html(str(card), response.url)
        if not image_url:
            continue

        title = ""
        for node in card.find_all(["h1", "h2", "h3", "h4"]):
            candidate_title = _myseed_text(node, 240)
            if candidate_title and len(candidate_title) > 3:
                title = candidate_title
                break
        if not title:
            image_node = card.find("img")
            title = (
                str((image_node or {}).get("alt") or "").strip()
                or str((card.find("a") or {}).get("title") or "").strip()
                or "جديد"
            )

        rating, rating_source = _myseed_card_rating(card)
        items.append({
            "title": title,
            "link": detail_url,
            "image": image_url,
            "html": str(card),
            "card_text": _myseed_text(card, 3500),
            "rating": rating or "",
            "rating_source": rating_source,
            "source_format": "myseed_html",
            "myseed": True
        })
    return items


def _myseed_labeled_li(soup, label):
    pattern = re.compile(r"^\s*" + re.escape(label) + r"\s*[:：]\s*(.*)$", re.IGNORECASE)
    for node in soup.find_all("li"):
        text = _myseed_text(node, 900)
        match = pattern.match(text)
        if match:
            return node, (match.group(1) or "").strip()
    return None, ""


def _myseed_link_values(node, path_prefix):
    values = []
    if node is None:
        return values
    for anchor in node.find_all("a", href=True):
        parsed = urlparse(urljoin(MYSEED_RECENT_URL, anchor.get("href", "")))
        if not parsed.path.casefold().startswith(path_prefix.casefold()):
            continue
        value = _myseed_text(anchor, 160)
        if value and value not in values:
            values.append(value)
    return values


def _myseed_extract_duration(soup):
    """Read MySeed's duration when available; keep the site's original duration wording."""
    _, duration_raw = _myseed_labeled_li(soup, "مدة العرض")
    duration = re.sub(r"\s+", " ", str(duration_raw or "")).strip(" :：-")
    if duration:
        return duration

    # Fallback for layouts where the duration is rendered in the details panel
    # but is not wrapped in a labeled list item.
    info_node = soup.select_one(".info__side")
    info_text = _myseed_text(info_node, 5000) if info_node else ""
    match = re.search(
        r"(?:^|\s)مدة\s+العرض\s*[:：]?\s*(.+?)"
        r"(?=\s+(?:سنة\s+العرض|لغة\s+العرض|جودة\s+العرض|"
        r"بلد\s+العرض|تصنيف\s+العرض|نوع\s+العرض)\s*[:：]?|$)",
        info_text
    )
    return re.sub(r"\s+", " ", match.group(1)).strip(" :：-") if match else ""


def _myseed_extract_detail(item, soup):
    """Extract fields from MySeed's verified detail-page labels and taxonomy links."""
    title = item.get("title") or "جديد"
    title_node = soup.find("meta", attrs={"property": "og:title"})
    if title_node and title_node.get("content"):
        title = _myseed_text(title_node.get("content"), 260)
    else:
        title_node = soup.find("title")
        if title_node:
            title = _myseed_text(title_node, 300).split("|")[0].strip() or title

    # The page title is a stable fallback; prefer the content heading if one exists.
    for heading in soup.find_all(["h1", "h2", "h3"]):
        heading_text = _myseed_text(heading, 260)
        if heading_text and re.search(r"(?:فيلم|مسلسل|انمي|أنمي|movie|series|anime)", heading_text, re.I):
            title = heading_text
            break

    year_node, year_raw = _myseed_labeled_li(soup, "سنة العرض")
    year = ""
    if year_node is not None:
        for anchor in year_node.find_all("a", href=True):
            href_path = urlparse(urljoin(MYSEED_RECENT_URL, anchor.get("href", ""))).path
            if "/release-year/" in href_path.casefold():
                match = _YEAR_RE.search(_latin_digits(href_path + " " + _myseed_text(anchor, 80)))
                if match:
                    year = match.group(0)
                    break
        if not year:
            match = _YEAR_RE.search(_latin_digits(year_raw))
            year = match.group(0) if match else ""

    duration = _myseed_extract_duration(soup)

    country_node, country_raw = _myseed_labeled_li(soup, "بلد العرض")
    country_values = _myseed_link_values(country_node, "/country/")
    country = _normalize_country(country_values[0] if country_values else country_raw)

    genre_node, genre_raw = _myseed_labeled_li(soup, "نوع العرض")
    genre_values = _myseed_link_values(genre_node, "/genre/")
    if not genre_values and genre_raw:
        # Keep the field as displayed by the site; normalization maps known genres.
        genre_values = [genre_raw]
    genres = []
    for value in genre_values:
        normalized = _normalize_genre(value)
        if normalized:
            genres.extend([part.strip() for part in normalized.split("،") if part.strip()])
    genres = list(dict.fromkeys(genres))

    category_node, category_raw = _myseed_labeled_li(soup, "تصنيف العرض")
    category_values = _myseed_link_values(category_node, "/category/")
    if not category_values and category_raw:
        category_values = [category_raw]
    terms = [{"name": value, "taxonomy": "category"} for value in category_values]

    meta_description = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    story = (meta_description.get("content") or "").strip() if meta_description else ""
    if not story:
        og_description = soup.find("meta", attrs={"property": "og:description"})
        story = (og_description.get("content") or "").strip() if og_description else ""
    story = _clean_story_text(story)
    # MySeed's description starts with SEO wording. Keep the actual plot after its
    # explicit "حيث تدور الأحداث حول" clause when present.
    # Remove MySeed's SEO lead-in through the first comma, and optional "حيث".
    # Keep the actual plot opening whether it says "تدور الأحداث", "تنطلق مهمة",
    # "يخوض..." or another verb; don't require one guessed sentence pattern.
    story = re.sub(
        r"^مشاهدة\s+وتحميل\s+.+?(?:،|,)\s*(?:حيث\s*)?",
        "",
        story,
        count=1,
        flags=re.IGNORECASE
    )
    if not story:
        story, _ = _extract_story({
            "content_html": str(soup),
            "excerpt_html": "",
            "html": str(soup),
            "public_meta": {}
        }, max_chars=900)

    rating = item.get("rating") or ""
    rating_source = item.get("rating_source") or "unavailable"
    if not rating:
        # Use MySeed's own visitor rating only when it has at least one vote.
        rating_node = soup.select_one(".star__rating")
        rating_text = _myseed_text(rating_node, 160) if rating_node else ""
        match = re.search(
            r"(\d{1,2}(?:[.,]\d{1,2})?)\s*/\s*10\s*\(\s*(\d+)\s*(?:صوت|أصوات)",
            rating_text
        )
        if match and int(match.group(2)) > 0:
            value = float(match.group(1).replace(",", "."))
            if 0 <= value <= 10:
                rating = f"{value:g}"
                rating_source = "myseed_visitor_rating"

    work_type = _infer_work_type({
        "title": title,
        "link": item.get("link", ""),
        "terms": terms
    })
    item.update({
        "title": title,
        "year": year,
        "duration": duration,
        "genres": genres,
        "country": country,
        "work_type": work_type,
        "terms": terms,
        "story": _trim_story(story, 650) if story else "",
        "story_source": "myseed_meta_description" if story else "unavailable",
        "rating": rating,
        "rating_source": rating_source,
        "detail_status": "HTTP_200"
    })
    return item


def _enrich_myseed_item(item, scraper):
    """Read one MySeed detail page; never follows TMDB or other external metadata links."""
    detail_url = item.get("link") or ""
    if not _myseed_detail_url_allowed(detail_url):
        item["detail_status"] = "invalid_detail_url"
        return item
    detail_response = None
    try:
        detail_response = scraper.get(
            detail_url,
            timeout=18,
            allow_redirects=True,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
                "Referer": MYSEED_RECENT_URL
            }
        )
        if not detail_response.ok:
            item["detail_status"] = f"HTTP_{detail_response.status_code}"
            return item
        detail_body = detail_response.text or ""
        lower = detail_body[:12000].casefold()
        if any(marker in lower for marker in (
            "just a moment", "enable javascript and cookies to continue",
            "checking your browser", "cf-chl-", "challenge-platform",
            "access denied", "request blocked"
        )):
            item["detail_status"] = "challenge"
            return item
        soup = BeautifulSoup(detail_body, "html.parser")
        _myseed_extract_detail(item, soup)
        return item
    except Exception as exc:
        item["detail_status"] = f"{type(exc).__name__}: {exc}"
        return item
    finally:
        if detail_response is not None:
            detail_response.close()



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
        "errors": [],
        "baseline_initialized": False,
        "baseline_items_recorded": 0
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

        effective_source_url = result["source"]
        if urlparse(response.url or result["source"]).hostname in MYSEED_ALLOWED_HOSTS:
            items = _parse_myseed_recent_response(response)
            source_format = "myseed_html"
        else:
            items, source_format = _parse_source_response(response, result["source"])

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
        elif source_format == "myseed_html":
            # Baseline existing MySeed items once to prevent a source migration from
            # blasting the WhatsApp group with up to five already-published titles.
            baseline_marker = "__MYSEED_BASELINE_V1__"
            cur.execute(
                "SELECT 1 FROM messages WHERE phone=%s AND sender=%s AND message=%s LIMIT 1",
                ("myseed-baseline", "system", baseline_marker)
            )
            if not cur.fetchone():
                baseline_count = 0
                for known_item in items:
                    known_link = (known_item.get("link") or "").strip()
                    if not known_link:
                        continue
                    cur.execute("SELECT 1 FROM messages WHERE message=%s LIMIT 1", (known_link,))
                    if not cur.fetchone():
                        cur.execute(
                            "INSERT INTO messages(phone,message,sender,msg_time) VALUES('myseed-baseline',%s,'system',%s)",
                            (known_link, datetime.now().strftime("%H:%M"))
                        )
                        baseline_count += 1
                cur.execute(
                    "INSERT INTO messages(phone,message,sender,msg_time) VALUES('myseed-baseline',%s,'system',%s)",
                    (baseline_marker, datetime.now().strftime("%H:%M"))
                )
                conn.commit()
                result["baseline_initialized"] = True
                result["baseline_items_recorded"] = baseline_count
                result["status"] = "baseline_initialized"
        if items and not result.get("baseline_initialized"):
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
                if item.get("myseed"):
                    _enrich_myseed_item(item, scraper)
                    title = (item.get("title") or title).strip()
                    genres = item.get("genres") or []
                    country = item.get("country") or ""
                    year = item.get("year") or ""
                    duration = item.get("duration") or ""
                    work_type = item.get("work_type") or _infer_work_type(item)
                    detail_status = item.get("detail_status") or "not_fetched"
                    story_text = item.get("story") or ""
                    story_source = item.get("story_source") or "unavailable"
                    rating = item.get("rating") or ""
                    rating_source = item.get("rating_source") or "unavailable"
                    publication_year = ""
                else:
                    # Retain compatibility for explicitly configured non-MySeed sources.
                    genres, country, year = _metadata_from_public_post(item)
                    work_type = _infer_work_type(item)
                    detail_status = "public_api_only_page_blocked"
                    year = year or _extract_year(title=title, url=link, markup=metadata_html)
                    duration = ""
                    publication_year = _get_post_publication_year(item)
                    story_text, story_source = _extract_story(item, max_chars=650)
                    rating, rating_source = _extract_rating(item)
                if year:
                    year_line = f"📅 سنة العرض: {year}"
                    year_source = "work_year"
                elif publication_year:
                    year_line = f"🗓️ سنة الإضافة: {publication_year}"
                    year_source = "post_publication_year"
                else:
                    year_line = "📅 سنة العرض: غير منشورة"
                    year_source = "unavailable"
                # Keep work format (film/series/anime) separate from actual genres
                # (Drama/Action/Comedy). Never display "فيلم" as a genre fallback.
                duration_line = f"⏱️ المدة: {duration}\n" if duration else ""
                work_kind_text = work_type or "غير محدد"
                genre_text = "، ".join(genres) if genres else "غير محدد"
                country_text = country if country else "غير محددة"
                if not item.get("myseed"):
                    story_text, story_source = _extract_story(item, max_chars=650)
                    rating, rating_source = _extract_rating(item)
                story_line = f"📖 القصة: {story_text}" if story_text else "📖 القصة: غير متاحة في المصدر العام"
                rating_line = f"⭐ التقييم: {rating}" if rating else "⭐ التقييم: غير متاح في بيانات المصدر العامة"
                caption = (
                    f"📺 {title}\n"
                    f"{year_line}\n"
                    f"{duration_line}"
                    f"🎬 نوع العمل: {work_kind_text}\n"
                    f"🎭 التصنيف: {genre_text}\n"
                    f"🌍 الدولة: {country_text}\n"
                    f"{rating_line}\n"
                    f"{story_line}\n"
                    f"🔥 متاح الآن في الاستراحة!"
                )

                result["attempts"] += 1
                ok, details = send_to_whatsapp(image_url, caption)
                result["send_results"].append({
                    "title": title,
                    "link": link,
                    "سنة العرض": year or None,
                    "المدة": duration or None,
                    "year_source": year_source,
                    "publication_year": publication_year or None,
                    "genres": genres,
                    "work_type": work_type or None,
                    "country": country_text,
                    "rating": rating or None,
                    "rating_source": rating_source,
                    "story": story_text or None,
                    "story_source": story_source,
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
        elif result["status"] not in ("no_candidates", "error", "baseline_initialized"):
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
        urljoin(origin, "/wp-json/wp/v2/types"),
        urljoin(origin, "/wp-json/wp/v2/taxonomies"),
        urljoin(origin, "/wp-json/wp/v2/categories?per_page=100"),
        urljoin(origin, "/wp-sitemap.xml")
    ]
    unique_urls = list(dict.fromkeys(urls))
    probes = [None] * len(unique_urls)
    with ThreadPoolExecutor(max_workers=min(5, len(unique_urls))) as pool:
        futures = {pool.submit(_probe_public_source, url): index for index, url in enumerate(unique_urls)}
        for future in as_completed(futures):
            probes[futures[future]] = future.result()

    usable = [p["url"] for p in probes if p and p["usable_feed_or_json_candidate"]]

    # Discover whether WordPress exposes a public custom post type that might contain
    # richer series/movie metadata. This does not send messages or alter data.
    rest_registry = {
        "types_http_status": None,
        "taxonomies_http_status": None,
        "public_types": [],
        "public_taxonomies": [],
        "custom_rest_base_probes": [],
        "error": None
    }
    registry_headers = {
        "User-Agent": "WA-Bridge-Source-Diagnostic/1.0",
        "Accept": "application/json"
    }
    try:
        types_url = urljoin(origin, "/wp-json/wp/v2/types")
        types_response = requests.get(types_url, timeout=10, allow_redirects=True, headers=registry_headers)
        rest_registry["types_http_status"] = types_response.status_code
        if types_response.ok and isinstance(types_response.json(), dict):
            types_data = types_response.json()
            for slug, data in types_data.items():
                if not isinstance(data, dict):
                    continue
                rest_base = str(data.get("rest_base") or slug).strip("/")
                labels = data.get("labels") if isinstance(data.get("labels"), dict) else {}
                rest_registry["public_types"].append({
                    "slug": slug,
                    "rest_base": rest_base,
                    "name": labels.get("name") or labels.get("singular_name") or slug,
                    "taxonomies": data.get("taxonomies") or [],
                    "has_rest_base": bool(rest_base)
                })
        types_response.close()

        taxonomies_url = urljoin(origin, "/wp-json/wp/v2/taxonomies")
        taxonomies_response = requests.get(
            taxonomies_url, timeout=10, allow_redirects=True, headers=registry_headers
        )
        rest_registry["taxonomies_http_status"] = taxonomies_response.status_code
        if taxonomies_response.ok and isinstance(taxonomies_response.json(), dict):
            taxonomies_data = taxonomies_response.json()
            for slug, data in taxonomies_data.items():
                if not isinstance(data, dict):
                    continue
                labels = data.get("labels") if isinstance(data.get("labels"), dict) else {}
                rest_registry["public_taxonomies"].append({
                    "slug": slug,
                    "rest_base": data.get("rest_base") or slug,
                    "name": labels.get("name") or labels.get("singular_name") or slug,
                    "types": data.get("types") or [],
                    "hierarchical": bool(data.get("hierarchical"))
                })
        taxonomies_response.close()

        known_bases = {
            "posts", "pages", "media", "categories", "tags", "users",
            "comments", "types", "taxonomies", "statuses", "blocks",
            "templates", "template-parts", "global-styles", "navigation"
        }
        candidates = []
        for entry in rest_registry["public_types"]:
            base = str(entry.get("rest_base") or "").strip("/")
            if not base or base.lower() in known_bases or "/" in base:
                continue
            if base not in candidates:
                candidates.append(base)

        for base in candidates[:5]:
            candidate_url = urljoin(origin, f"/wp-json/wp/v2/{base}?per_page=2&_embed=1")
            probe = {
                "rest_base": base,
                "url": candidate_url,
                "http_status": None,
                "item_count": 0,
                "sample": [],
                "error": None
            }
            try:
                custom_response = requests.get(
                    candidate_url, timeout=8, allow_redirects=True, headers=registry_headers
                )
                probe["http_status"] = custom_response.status_code
                if custom_response.ok:
                    custom_data = custom_response.json()
                    if isinstance(custom_data, list):
                        probe["item_count"] = len(custom_data)
                        for record in custom_data[:2]:
                            if not isinstance(record, dict):
                                continue
                            raw_title = record.get("title") or {}
                            title = raw_title.get("rendered", "") if isinstance(raw_title, dict) else str(raw_title)
                            raw_content = record.get("content") or {}
                            content_html = raw_content.get("rendered", "") if isinstance(raw_content, dict) else str(raw_content)
                            embedded = record.get("_embedded") or {}
                            probe["sample"].append({
                                "id": record.get("id"),
                                "slug": record.get("slug"),
                                "link": record.get("link"),
                                "title": BeautifulSoup(title, "html.parser").get_text(" ", strip=True),
                                "meta_keys": sorted((record.get("meta") or {}).keys())[:40]
                                    if isinstance(record.get("meta"), dict) else [],
                                "content_excerpt": BeautifulSoup(content_html, "html.parser").get_text(" ", strip=True)[:240],
                                "embedded_keys": sorted(embedded.keys())[:20]
                            })
                custom_response.close()
            except Exception as exc:
                probe["error"] = f"{type(exc).__name__}: {exc}"
            rest_registry["custom_rest_base_probes"].append(probe)
        types_response = None
    except Exception as exc:
        rest_registry["error"] = f"{type(exc).__name__}: {exc}"

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
            genres, country, year = _metadata_from_public_post(item)
            # Theme detail pages return HTTP 403 from this service; keep diagnostics read-only
            # and inspect the public post fields/terms instead of repeating that request.
            detail_status = "public_api_only_page_blocked"
            year = year or _extract_year(title=item_title, url=item_link, markup=item_html)
            publication_year = _get_post_publication_year(item)
            work_type = _infer_work_type(item)
            public_meta = item.get("public_meta") or {}
            term_records = item.get("terms") or []
            story_preview, story_source = _extract_story(item, max_chars=360)
            rating, rating_source = _extract_rating(item)
            source_inspection["sample_items"].append({
                "title": item_title,
                "link": item_link,
                "has_image": bool(item.get("image")),
                "image_host": urlparse(item["image"]).hostname if item.get("image") else None,
                "year": year or None,
                "year_source": "work_year" if year else ("post_publication_year" if publication_year else "unavailable"),
                "publication_year": publication_year or None,
                "work_type": work_type or None,
                "genres": genres,
                "country": country or None,
                "story_available": bool(story_preview),
                "story_source": story_source,
                "story_preview": story_preview or None,
                "rating": rating or None,
                "rating_available": bool(rating),
                "rating_source": rating_source,
                "metadata_page_status": detail_status,
                "public_meta_keys": sorted(str(key) for key in public_meta.keys())[:60],
                "public_terms": [
                    {
                        "name": term.get("name"),
                        "taxonomy": term.get("taxonomy"),
                        "slug": term.get("slug")
                    }
                    for term in term_records[:30]
                ]
            })
        source_inspection["items_with_metadata_in_sample"] = sum(
            1 for item in source_inspection["sample_items"]
            if item.get("year") or item.get("publication_year") or item.get("genres")
            or item.get("work_type") or item.get("country")
        )
        source_inspection["items_with_work_type_in_sample"] = sum(
            1 for item in source_inspection["sample_items"] if item.get("work_type")
        )
        source_inspection["items_with_country_in_sample"] = sum(
            1 for item in source_inspection["sample_items"] if item.get("country")
        )
        source_inspection["items_with_detailed_genres_in_sample"] = sum(
            1 for item in source_inspection["sample_items"] if item.get("genres")
        )
        source_inspection["items_with_story_in_sample"] = sum(
            1 for item in source_inspection["sample_items"] if item.get("story_available")
        )
        source_inspection["items_with_rating_in_sample"] = sum(
            1 for item in source_inspection["sample_items"] if item.get("rating_available")
        )
        source_inspection["items_with_year_or_publication_year_in_sample"] = sum(
            1 for item in source_inspection["sample_items"]
            if item.get("year") or item.get("publication_year")
        )
        response.close()
    except Exception as exc:
        source_inspection["error"] = f"{type(exc).__name__}: {exc}"

    if source_inspection["items_found"] and source_inspection["items_with_images"]:
        if source_inspection.get("items_with_metadata_in_sample", 0):
            next_step = (
                "Feed items, images, broad work-type classifications, and some year/country data were found. "
                "Detailed genres and some countries are absent from the standard public post fields, while the theme pages "
                "are blocked. The read-only rest_registry section checks whether a public custom REST type exposes richer data. "
                "Automatic polling is enabled every 30 seconds and processes up to 5 newest unrecorded items per check. "
                "This diagnostic did not send messages."
            )
        else:
            next_step = (
                "Feed items and images are available. The diagnostic now reports work year versus post-publication year, "
                "broad work type inferred from title/categories, public genres and country. A publication year is explicitly "
                "labelled as 'سنة الإضافة', not as the work's release year. Automatic polling is enabled every 30 seconds, "
                "up to 5 newest unrecorded items per check. This diagnostic did not send messages."
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
        "rest_registry": rest_registry,
        "source_inspection": source_inspection,
        "automatic_polling": dict(_POLL_STATE),
        "next_step": next_step
    }



# ================= MYSEED READ-ONLY SOURCE PROBE =================
def _myseed_text(value, limit=700):
    text = BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())[:limit]


def _myseed_dom_summary(node, base_url):
    if node is None:
        return None
    classes = node.get("class") or []
    image_urls = []
    for image in node.find_all("img", limit=4):
        candidate = image.get("data-src") or image.get("data-lazy-src") or image.get("src")
        if candidate:
            image_urls.append(urljoin(base_url, candidate))
    links = []
    for anchor in node.find_all("a", href=True, limit=8):
        absolute = urljoin(base_url, anchor.get("href", ""))
        parsed = urlparse(absolute)
        if parsed.hostname in MYSEED_ALLOWED_HOSTS and absolute not in links:
            links.append(absolute)
    return {
        "tag": getattr(node, "name", None),
        "id": node.get("id") if hasattr(node, "get") else None,
        "classes": classes[:12] if isinstance(classes, list) else [],
        "text": _myseed_text(node, 900),
        "images": image_urls,
        "links": links
    }


def _myseed_field_blocks(soup):
    labels = (
        "قصة العرض", "سنة العرض", "بلد العرض", "نوع العرض",
        "تصنيف العرض", "التقييم", "تقييم الزوار"
    )
    found = {}
    all_nodes = soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "dt", "dd", "div", "span", "strong"])
    for label in labels:
        matches = []
        for node in all_nodes:
            node_text = _myseed_text(node, 1600)
            if label.casefold() not in node_text.casefold():
                continue
            if not node_text:
                continue
            matches.append((len(node_text), node))
        matches.sort(key=lambda pair: pair[0])
        found[label] = [
            _myseed_dom_summary(node, MYSEED_RECENT_URL)
            for _, node in matches[:3]
        ]
    return found


def diagnose_myseed_source():
    """Read-only connectivity/DOM probe. Never writes to DB or sends WhatsApp messages."""
    result = {
        "status": "starting",
        "code_version": APP_CODE_VERSION,
        "source": MYSEED_RECENT_URL,
        "message_sending": False,
        "database_writes": False,
        "source_http_status": None,
        "final_url": None,
        "content_type": None,
        "server": None,
        "body_bytes": 0,
        "challenge_detected": False,
        "error": None,
        "listing": {},
        "detail_samples": []
    }
    session = None
    response = None
    try:
        session = cloudscraper.create_scraper()
        response = session.get(
            MYSEED_RECENT_URL,
            timeout=25,
            allow_redirects=True,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
                "Referer": "https://m.myseed.tv/"
            }
        )
        result["source_http_status"] = response.status_code
        result["final_url"] = response.url
        result["content_type"] = response.headers.get("Content-Type")
        result["server"] = response.headers.get("Server")
        body = response.text or ""
        result["body_bytes"] = len(response.content or b"")
        body_lower = body[:12000].casefold()
        result["challenge_detected"] = any(marker in body_lower for marker in (
            "just a moment", "enable javascript and cookies to continue",
            "checking your browser", "cf-chl-", "challenge-platform",
            "access denied", "request blocked"
        ))
        if not response.ok:
            result["error"] = f"Listing returned HTTP {response.status_code}"
            result["status"] = "listing_http_error"
            return result
        if result["challenge_detected"]:
            result["error"] = "Listing returned an anti-bot/challenge page"
            result["status"] = "listing_challenge"
            return result

        soup = BeautifulSoup(body, "html.parser")
        title_node = soup.find("title")
        canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
        listing = {
            "page_title": _myseed_text(title_node, 250),
            "canonical_url": urljoin(response.url, canonical.get("href")) if canonical and canonical.get("href") else None,
            "body_text_preview": _myseed_text(soup.body or soup, 2200),
            "image_count": len(soup.find_all("img")),
            "anchor_count": len(soup.find_all("a", href=True)),
            "candidate_detail_links": [],
            "card_dom_samples": []
        }

        candidates = {}
        for anchor in soup.find_all("a", href=True):
            absolute = urljoin(response.url, anchor.get("href", ""))
            parsed = urlparse(absolute)
            if parsed.hostname not in MYSEED_ALLOWED_HOSTS:
                continue
            path = parsed.path.rstrip("/")
            if not path or path == "/recently":
                continue
            lowered = path.casefold()
            if any(part in lowered for part in (
                "/category/", "/tag/", "/page/", "/search/", "/author/",
                "/genre/", "/country/", "/release-year/", "/feed/"
            )):
                continue
            if re.search(r"\.(?:jpg|jpeg|png|webp|gif|svg|css|js|xml)$", lowered):
                continue

            image_present = anchor.find("img") is not None
            anchor_text = _myseed_text(anchor, 180)
            score = (5 if image_present else 0) + (2 if len(anchor_text) >= 12 else 0)
            score += 1 if any(word in " ".join(anchor.get("class") or []).casefold()
                              for word in ("title", "post", "item", "movie", "serie")) else 0
            current = candidates.get(absolute)
            if current is None or score > current["score"]:
                candidates[absolute] = {
                    "url": absolute,
                    "score": score,
                    "anchor_text": anchor_text,
                    "image_present": image_present,
                    "anchor": anchor
                }

        ordered = sorted(candidates.values(), key=lambda item: (-item["score"], item["url"]))
        for candidate in ordered[:12]:
            listing["candidate_detail_links"].append({
                "url": candidate["url"],
                "anchor_text": candidate["anchor_text"],
                "image_present": candidate["image_present"],
                "score": candidate["score"]
            })
            anchor = candidate["anchor"]
            node = anchor
            selected = None
            for _ in range(7):
                if node is None:
                    break
                if getattr(node, "name", None) in ("article", "li", "div", "section"):
                    node_text = _myseed_text(node, 2400)
                    if len(node_text) >= 35 and (node.find("img") is not None or len(node.find_all("a", href=True)) >= 2):
                        selected = node
                        break
                node = node.parent
            if selected is not None and len(listing["card_dom_samples"]) < 5:
                summary = _myseed_dom_summary(selected, response.url)
                summary["detail_url"] = candidate["url"]
                listing["card_dom_samples"].append(summary)

        result["listing"] = listing

        # Read no more than three detail pages, never following or fetching TMDB links.
        detail_candidates = [item["url"] for item in ordered if item["score"] >= 2][:3]
        for detail_url in detail_candidates:
            detail = {
                "url": detail_url,
                "http_status": None,
                "final_url": None,
                "challenge_detected": False,
                "error": None,
                "page_title": None,
                "headings": [],
                "meta_description": None,
                "visible_text_preview": None,
                "field_blocks": {},
                "tmdb_links_seen_but_not_opened": 0
            }
            detail_response = None
            try:
                detail_response = session.get(
                    detail_url,
                    timeout=20,
                    allow_redirects=True,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
                        "Referer": MYSEED_RECENT_URL
                    }
                )
                detail["http_status"] = detail_response.status_code
                detail["final_url"] = detail_response.url
                detail_body = detail_response.text or ""
                detail_lower = detail_body[:12000].casefold()
                detail["challenge_detected"] = any(marker in detail_lower for marker in (
                    "just a moment", "enable javascript and cookies to continue",
                    "checking your browser", "cf-chl-", "challenge-platform",
                    "access denied", "request blocked"
                ))
                if detail_response.ok and not detail["challenge_detected"]:
                    detail_soup = BeautifulSoup(detail_body, "html.parser")
                    detail_title = detail_soup.find("title")
                    meta_desc = detail_soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
                    detail["page_title"] = _myseed_text(detail_title, 300)
                    detail["meta_description"] = (meta_desc.get("content") or "")[:700] if meta_desc else None
                    detail["headings"] = [
                        _myseed_text(node, 180)
                        for node in detail_soup.find_all(["h1", "h2", "h3", "h4"], limit=20)
                    ]
                    detail["visible_text_preview"] = _myseed_text(detail_soup.body or detail_soup, 5200)
                    detail["field_blocks"] = _myseed_field_blocks(detail_soup)
                    detail["tmdb_links_seen_but_not_opened"] = sum(
                        1 for anchor in detail_soup.find_all("a", href=True)
                        if (urlparse(anchor.get("href", "")).hostname or "").casefold().removeprefix("www.") == "themoviedb.org"
                    )
                elif not detail_response.ok:
                    detail["error"] = f"Detail page returned HTTP {detail_response.status_code}"
                elif detail["challenge_detected"]:
                    detail["error"] = "Detail page returned an anti-bot/challenge page"
            except Exception as exc:
                detail["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                if detail_response is not None:
                    detail_response.close()
            result["detail_samples"].append(detail)

        result["status"] = "probe_complete"
        return result
    except Exception as exc:
        result["status"] = "probe_error"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        if response is not None:
            response.close()
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


# ================= ROUTES =================
@app.route("/")
def home():
    return "Flask + WA Bridge OK"


@app.route("/api/source_diagnostic")
def source_diagnostic():
    # Read-only: probes public endpoints and never sends WhatsApp messages.
    return jsonify(diagnose_public_sources()), 200


@app.route("/api/myseed_diagnostic")
def myseed_diagnostic():
    # Read-only MySeed connectivity and DOM probe. No WhatsApp sends and no database writes.
    result = diagnose_myseed_source()
    return jsonify(result), 200


@app.route("/api/myseed_preview")
def myseed_preview():
    # Uses the same MySeed parser as the poller, but is strictly read-only.
    session = None
    response = None
    try:
        session = cloudscraper.create_scraper()
        response = session.get(
            MYSEED_RECENT_URL,
            timeout=25,
            allow_redirects=True,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
                "Referer": MYSEED_RECENT_URL
            }
        )
        if not response.ok:
            return jsonify({
                "status": "source_http_error",
                "http_status": response.status_code,
                "message_sending": False,
                "database_writes": False
            }), 502
        items = _parse_myseed_recent_response(response)
        preview = []
        for item in items[:5]:
            _enrich_myseed_item(item, session)
            preview.append({
                "title": item.get("title"),
                "link": item.get("link"),
                "image": item.get("image"),
                "سنة العرض": item.get("year") or None,
                "المدة": item.get("duration") or None,
                "work_type": item.get("work_type") or None,
                "genres": item.get("genres") or [],
                "country": item.get("country") or None,
                "rating": item.get("rating") or None,
                "rating_source": item.get("rating_source"),
                "story": item.get("story") or None,
                "story_source": item.get("story_source"),
                "detail_status": item.get("detail_status")
            })
        return jsonify({
            "status": "preview_complete",
            "code_version": APP_CODE_VERSION,
            "source": MYSEED_RECENT_URL,
            "items_found": len(items),
            "items_previewed": len(preview),
            "message_sending": False,
            "database_writes": False,
            "items": preview
        }), 200
    except Exception as exc:
        return jsonify({
            "status": "preview_error",
            "code_version": APP_CODE_VERSION,
            "message_sending": False,
            "database_writes": False,
            "error": f"{type(exc).__name__}: {exc}"
        }), 502
    finally:
        if response is not None:
            response.close()
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


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
    # Serialize the clear operation with the automatic/manual poller.
    # Clearing the dedupe table must not race with check_updates().
    if not _CHECK_LOCK.acquire(blocking=False):
        return jsonify({
            "status": "check_in_progress",
            "message_sending": False,
            "database_writes": False,
            "message": "An update check is running; no records were cleared. Retry after it finishes."
        }), 409

    session = None
    response = None
    conn = None
    cur = None
    try:
        # Refresh the MySeed baseline as part of the clear operation. This keeps
        # already-published items marked as seen, so clearing history cannot
        # accidentally resend old items on the next 30-second poll.
        session = cloudscraper.create_scraper()
        response = session.get(
            MYSEED_RECENT_URL,
            timeout=25,
            allow_redirects=True,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
                "Referer": MYSEED_RECENT_URL
            }
        )
        response.raise_for_status()
        source_items = _parse_myseed_recent_response(response)
        baseline_links = list(dict.fromkeys(
            (item.get("link") or "").strip()
            for item in source_items
            if (item.get("link") or "").strip()
        ))
        if not baseline_links:
            return jsonify({
                "status": "source_empty",
                "message_sending": False,
                "database_writes": False,
                "message": "MySeed returned no usable items; existing records were left untouched."
            }), 502

        conn = db()
        cur = conn.cursor()
        cur.execute("DELETE FROM messages")
        deleted_records = cur.rowcount

        now_text = datetime.now().strftime("%H:%M")
        for link in baseline_links:
            cur.execute(
                "INSERT INTO messages(phone,message,sender,msg_time) VALUES(%s,%s,%s,%s)",
                ("myseed-baseline", link, "system", now_text)
            )

        baseline_marker = "__MYSEED_BASELINE_V1__"
        cur.execute(
            "INSERT INTO messages(phone,message,sender,msg_time) VALUES(%s,%s,%s,%s)",
            ("myseed-baseline", baseline_marker, "system", now_text)
        )
        conn.commit()

        return jsonify({
            "status": "cleared",
            "deleted_records": deleted_records,
            "baseline_items_recorded": len(baseline_links),
            "baseline_marker_preserved": True,
            "source": MYSEED_RECENT_URL,
            "message_sending": False,
            "database_writes": True,
            "message": "Old message history cleared. Current MySeed items were retained as seen to prevent accidental resend."
        }), 200
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        print("CLEAR MESSAGES ERROR:", f"{type(exc).__name__}: {exc}", flush=True)
        return jsonify({
            "status": "clear_failed",
            "message_sending": False,
            "database_writes": False,
            "error": f"{type(exc).__name__}: {exc}"
        }), 502
    finally:
        if response is not None:
            response.close()
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
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
        _CHECK_LOCK.release()


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
