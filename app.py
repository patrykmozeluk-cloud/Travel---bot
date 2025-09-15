import os
import logging
import asyncio
import httpx
import feedparser
import orjson
import time
import random
import html
from flask import Flask, request, jsonify
from google.cloud import storage
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, unquote
from typing import Dict, Any, Tuple, List
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# --- APP / GCS ---
app = Flask(__name__)
storage_client = storage.Client()

# --- ENV ---
def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name, default)
    return v if v is None else v.strip()

TG_TOKEN = env("TG_TOKEN")
TG_CHAT_ID = env("TG_CHAT_ID")
BUCKET_NAME = env("BUCKET_NAME", "travel-bot-storage-patrykmozeluk-cloud")
SENT_LINKS_FILE = env("SENT_LINKS_FILE", "sent_links.json")
HTTP_TIMEOUT = float(env("HTTP_TIMEOUT", "20.0"))
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL")  # np. https://twoja-usluga-abc-uc.a.run.app
TELEGRAM_SECRET = env("TELEGRAM_SECRET")
ENABLE_WEBHOOK = env("ENABLE_WEBHOOK", "1") in {"1", "true", "True", "yes", "YES"}
DISABLE_DEDUP = env("DISABLE_DEDUP", "0") in {"1", "true", "True", "yes", "YES"}
DEBUG_FEEDS = env("DEBUG_FEEDS", "0") in {"1", "true", "True", "yes", "YES"}
MAX_POSTS_PER_RUN = int(env("MAX_POSTS_PER_RUN", "0"))  # 0 = bez limitu

def dbg(msg: str):
    if DEBUG_FEEDS:
        log.info(f"DEBUG {msg}")

# --- GCS SENT LINKS STATE ---
_bucket = storage_client.bucket(BUCKET_NAME)
_blob = _bucket.blob(SENT_LINKS_FILE)

DROP_PARAMS = {
    "utm_source","utm_medium","utm_campaign","utm_term","utm_content",
    "fbclid","gclid","igshid","mc_cid","mc_eid","ref","ref_src","src"
}

def _strip_default_port(netloc: str, scheme: str) -> str:
    if scheme == "http" and netloc.endswith(":80"):  return netloc[:-3]
    if scheme == "https" and netloc.endswith(":443"): return netloc[:-4]
    return netloc

def canonicalize_url(url: str) -> str:
    try:
        u = unquote(url.strip())
        p = urlparse(u)
        scheme = (p.scheme or "https").lower()
        netloc = p.netloc.lower()
        for pref in ("www.","m.","amp."):
            if netloc.startswith(pref):
                netloc = netloc[len(pref):]
        netloc = _strip_default_port(netloc, scheme)
        path = p.path or "/"
        while "//" in path:
            path = path.replace("//","/")
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        q = [(k,v) for k,v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in DROP_PARAMS]
        q.sort()
        return urlunparse((scheme, netloc, path, p.params, urlencode(q, doseq=True), ""))
    except Exception:
        return url.strip()

def _default_state() -> Dict[str, Any]:
    return {"sent_links": {}}

def load_state() -> Tuple[Dict[str, Any], int | None]:
    try:
        if not _blob.exists():
            return _default_state(), None
        _blob.reload()
        data = _blob.download_as_bytes()
        generation = _blob.generation
        state_data = orjson.loads(data)
        if isinstance(state_data.get("sent_links"), list):
            state_data["sent_links"] = {
                link: datetime.now(timezone.utc).isoformat()
                for link in state_data["sent_links"]
            }
        return state_data, generation
    except Exception as e:
        log.warning(f"load_state fallback: {e}")
        return _default_state(), None

def save_state_atomic(state: Dict[str, Any], expected_generation: int | None, retries: int = 10):
    payload = orjson.dumps(state)
    for attempt in range(retries):
        try:
            if expected_generation is None:
                _blob.upload_from_string(payload, if_generation_match=0, content_type="application/json")
            else:
                _blob.upload_from_string(payload, if_generation_match=expected_generation, content_type="application/json")
            return
        except Exception as e:
            if "PreconditionFailed" in str(e) or "412" in str(e):
                time.sleep(0.5)
                _, expected_generation = load_state()
                continue
            raise
    raise RuntimeError("Atomic save failed.")

# --- FINGERPRINT / HEADERS (rotacja) ---
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]
AL_POOL = [
    "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "en-US,en;q=0.9,pl-PL;q=0.7",
    "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
]
BASE_HEADERS = {
    "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, text/html;q=0.7,*/*;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache", "Pragma": "no-cache", "DNT": "1",
    "Upgrade-Insecure-Requests": "1", "Connection": "keep-alive",
}

def build_headers(url: str) -> Dict[str, str]:
    h = dict(BASE_HEADERS)
    h["User-Agent"] = random.choice(UA_POOL)
    h["Accept-Language"] = random.choice(AL_POOL)
    try:
        p = urlparse(url)
        h["Referer"] = f"{p.scheme}://{p.netloc}/"
    except Exception:
        pass
    return h

def domain_root(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"

def make_async_client() -> httpx.AsyncClient:
    limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
    return httpx.AsyncClient(
        headers=BASE_HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True,
        http2=True, limits=limits, cookies=httpx.Cookies()
    )

# --- SOURCES ---
def get_web_sources() -> List[str]:
    try:
        with open('web_sources.txt', 'r', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
    except FileNotFoundError:
        return []

def get_rss_sources() -> List[str]:
    try:
        with open('rss_sources.txt', 'r', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
    except FileNotFoundError:
        return []

# --- IMG helper ---
async def find_og_image(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        r = await client.get(url, headers=build_headers(url))
        r.raise_for_status()
        s = BeautifulSoup(r.text, "html.parser")
        for sel in ('meta[property="og:image"]', 'meta[name="twitter:image"]'):
            tag = s.select_one(sel)
            if tag and tag.get("content") and tag["content"].strip().startswith("http"):
                return tag["content"].strip()
        main = s.select_one("article") or s
        img_tag = main.select_one("img")
        if img_tag and img_tag.get("src") and img_tag["src"].strip().startswith("http"):
            return img_tag["src"].strip()
    except Exception:
        pass
    return None

# --- TELEGRAM SENDER ---
async def send_telegram_message_async(title: str, link: str, chat_id: str | None = None) -> bool:
    chat = chat_id or TG_CHAT_ID
    if not TG_TOKEN or not chat:
        log.error("Brak TG_TOKEN/TG_CHAT_ID.")
        return False

    safe_title = html.escape(title, quote=False)
    caption = f"<b>{safe_title}</b>\n\n{link}"

    try:
        async with make_async_client() as client:
            img = await find_og_image(client, link)

        if img:
            method, payload = "sendPhoto", {"photo": img, "caption": caption}
        else:
            method, payload = "sendMessage", {"text": caption, "disable_web_page_preview": False}

        payload.update({"chat_id": chat, "parse_mode": "HTML"})
        url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"

        async with make_async_client() as client:
            r = await client.post(url, json=payload, timeout=HTTP_TIMEOUT)
            body_text = r.text
            r.raise_for_status()

            if '"ok":false' in body_text.replace(" ", "").lower():
                log.error(f"Telegram returned ok=false: body={body_text[:500]}")
                return False

            log.info(f"Message sent: {title[:80]}… (with_photo={bool(img)})")
            return True
    except Exception as e:
        log.error(f"Telegram send error for {link}: {e}")
        return False

# --- FETCH & SCRAPE ---
async def fetch_feed(client: httpx.AsyncClient, url: str) -> List[Tuple[str, str]]:
    posts: List[Tuple[str, str]] = []
    headers = build_headers(url)
    try:
        r = await client.get(url, headers=headers)
        if r.status_code == 200:
            feed = feedparser.parse(r.content)
            for entry in feed.entries:
                if t := entry.get("title"):
                    if l := entry.get("link"):
                        posts.append((t, l))
            log.info(f"Fetched {len(posts)} posts from RSS: {url}")
            return posts
        log.error(f"HTTP {r.status_code} for RSS: {url}")
    except Exception as e:
        log.error(f"Error fetching RSS {url}: {e}")
    return []

async def scrape_webpage(client: httpx.AsyncClient, url: str) -> List[Tuple[str, str]]:
    posts: List[Tuple[str, str]] = []
    headers = build_headers(url)
    try:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')

        selectors = []
        if "travel-dealz.com" in url:
            selectors = ['article.article-item h2 a', 'article.article h2 a']
        elif "secretflying.com" in url:
            selectors = ['article.post-item .post-title a', 'article h2 a']
        elif "wakacyjnipiraci.pl" in url:
            selectors = ['article.post-list__item a.post-list__link']
        
        for sel in selectors:
            for link_tag in soup.select(sel):
                if href := link_tag.get('href'):
                    title_tag = link_tag.find_parent('article').select_one('h2, h3') if 'wakacyjnipiraci' in url else link_tag
                    title = title_tag.get_text(strip=True) if title_tag else 'Brak tytułu'
                    posts.append((title, href))
        
        log.info(f"Scraped {len(posts)} posts from Web: {url}")
        return posts
    except Exception as e:
        log.error(f"Error scraping {url}: {e}")
        return []

# --- MAIN FLOW ---
async def process_feeds_async() -> str:
    log.info("Starting hybrid processing.")
    if not TG_TOKEN or not TG_CHAT_ID:
        return "Missing TG_TOKEN/TG_CHAT_ID."

    sources = get_rss_sources() + get_web_sources()
    if not sources:
        return "No sources found."

    random.shuffle(sources)
    state, generation = load_state()
    sent_links_dict: Dict[str, str] = state.get("sent_links", {})

    all_posts = []
    async with make_async_client() as client:
        tasks = []
        for url in sources:
            # Proste rozróżnienie na podstawie nazwy pliku lub zawartości
            if any(rss_domain in url for rss_domain in ["rss", "feed", ".xml"]):
                tasks.append(fetch_feed(client, url))
            else:
                tasks.append(scrape_webpage(client, url))
        
        results = await asyncio.gather(*tasks)
        for post_list in results:
            all_posts.extend(post_list)

    if MAX_POSTS_PER_RUN > 0:
        all_posts = all_posts[:MAX_POSTS_PER_RUN]

    candidates: List[Tuple[str, str]] = []
    for title, link in all_posts:
        canonical = canonicalize_url(link)
        if DISABLE_DEDUP or canonical not in sent_links_dict:
            candidates.append((title, link))
            sent_links_dict[canonical] = "processing" # Oznacz jako przetwarzany

    # Usuń duplikaty kandydatów
    unique_candidates = list({canonicalize_url(l): (t, l) for t, l in candidates}.values())
    
    log.info(f"Summary: seen={len(all_posts)}, new_candidates={len(unique_candidates)}")

    sent_count = 0
    now_utc_iso = datetime.now(timezone.utc).isoformat()
    if unique_candidates:
        results = await asyncio.gather(*[send_telegram_message_async(t, l) for t, l in unique_candidates])

        for (t, l), ok in zip(unique_candidates, results):
            if ok:
                sent_links_dict[canonicalize_url(l)] = now_utc_iso
                sent_count += 1
    
    # Czyszczenie starych linków i tych, których nie udało się wysłać
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    cleaned_sent_links = {
        link: ts for link, ts in sent_links_dict.items()
        if ts != "processing" and datetime.fromisoformat(ts.replace("Z", "+00:00")) > thirty_days_ago
    }
    state["sent_links"] = cleaned_sent_links
    
    try:
        save_state_atomic(state, generation)
    except RuntimeError as e:
        log.critical(f"State save failure: {e}")
        return f"Critical: {e}"

    result_msg = f"Done. Sent {sent_count} of {len(unique_candidates)} new posts."
    log.info(result_msg)
    return result_msg

# --- ROUTES ---
@app.route("/")
def index():
    return "Travel-Bot vHYBRID — działa!", 200

@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200

@app.route("/tasks/rss", methods=['POST'])
def handle_rss_task():
    log.info("Received /tasks/rss", extra={"event": "job_start", "ua": request.headers.get("User-Agent")})
    try:
        result = asyncio.run(process_feeds_async())
        code = 200 if "Critical" not in result else 500
        return result, code
    except Exception as e:
        log.exception("Unhandled error in /tasks/rss")
        return f"Server error: {e}", 500

if __name__ == "__main__":
    port = int(env("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
