import os
import logging
import asyncio
import httpx
import feedparser
import orjson
import time
from flask import Flask, request, jsonify
from google.cloud import storage
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
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
TG_TOKEN = os.environ.get('TG_TOKEN')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID')
BUCKET_NAME = os.environ.get('BUCKET_NAME', 'travel-bot-storage-patrykmozeluk-cloud')
SENT_LINKS_FILE = os.environ.get('SENT_LINKS_FILE', 'sent_links.json')
HTTP_TIMEOUT = float(os.environ.get('HTTP_TIMEOUT', "20.0"))
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage" if TG_TOKEN else None

# DEBUG (włącz: DEBUG_FEEDS=1 w Cloud Run)
DEBUG_FEEDS = os.environ.get("DEBUG_FEEDS", "0") in {"1", "true", "True", "yes", "YES"}
def dbg(msg: str):
    if DEBUG_FEEDS:
        log.info(f"DEBUG {msg}")

# --- GCS SENT LINKS STATE ---
_bucket = storage_client.bucket(BUCKET_NAME)
_blob = _bucket.blob(SENT_LINKS_FILE)

DROP_PARAMS = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","fbclid","gclid","igshid","mc_cid","mc_eid"}

def canonicalize_url(url: str) -> str:
    try:
        p = urlparse(url.strip())
        scheme = p.scheme.lower() or "https"
        netloc = p.netloc.lower()
        path = p.path or "/"
        q = sorted([(k, v) for k, v in parse_qsl(p.query) if k.lower() not in DROP_PARAMS])
        return urlunparse((scheme, netloc, path, p.params, urlencode(q), ""))
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
        # migracja starego formatu (lista -> dict)
        if isinstance(state_data.get("sent_links"), list):
            state_data["sent_links"] = {
                link: datetime.now(timezone.utc).isoformat()
                for link in state_data["sent_links"]
            }
        return state_data, generation
    except Exception:
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
            # wyścig zapisu -> odśwież generation i powtórz
            if "PreconditionFailed" in str(e) or "412" in str(e):
                time.sleep(0.5)
                _, expected_generation = load_state()
                continue
            raise
    raise RuntimeError("Atomic save failed.")

# --- HTTP KLIENT (anti-bot fingerprint) ---
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/139.0.0.0 Safari/537.36"),
    "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9,pl-PL;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "DNT": "1",
}

def build_headers(url: str) -> Dict[str, str]:
    h = dict(BROWSER_HEADERS)
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
        headers=BROWSER_HEADERS,
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
        http2=True,
        limits=limits,
    )

# --- SOURCES ---
def get_web_sources() -> List[str]:
    try:
        with open('web_sources.txt', 'r', encoding='utf-8') as f:
            sources = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
            log.info(f"Found {len(sources)} web sources for scraping.")
            return sources
    except FileNotFoundError:
        log.info("web_sources.txt not found. Skipping scraping.")
        return []

def get_rss_sources() -> List[str]:
    try:
        with open('rss_sources.txt', 'r', encoding='utf-8') as f:
            sources = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
            log.info(f"Found {len(sources)} RSS sources.")
            return sources
    except FileNotFoundError:
        log.error("CRITICAL: rss_sources.txt not found.")
        return []

# --- FETCH FEED (z pełnym śladem) ---
async def fetch_feed(client: httpx.AsyncClient, url: str) -> List[Tuple[str, str]]:
    posts: List[Tuple[str, str]] = []
    headers = build_headers(url)
    base = domain_root(url)

    # pre-warm: cookies z homepage (często potrzebne przy WAF)
    try:
        warm = await client.get(base, headers=headers)
        dbg(f"warmup {base} -> {warm.status_code}")
    except Exception as e:
        dbg(f"warmup error {base}: {e}")

    last_status = None
    for attempt in range(1, 4):  # 3 próby
        try:
            r = await client.get(url, headers=headers)
            last_status = r.status_code
            dbg(f"GET {url} -> {r.status_code}")
            if r.status_code == 200:
                feed = feedparser.parse(r.content)  # PARSUJ bytes
                dbg(f"Feed parsed: entries={len(feed.entries)}")

                for i, entry in enumerate(feed.entries):
                    t = entry.get("title")
                    l = entry.get("link")
                    if not t or not l:
                        dbg(f"[{i}] SKIP (brak tytułu/linku) keys={list(entry.keys())}")
                        continue
                    dbg(f"[{i}] FETCHED title='{t[:90]}' link={l}")
                    posts.append((t, l))

                log.info(f"Fetched {len(posts)} posts from {url}")
                return posts

            if r.status_code in (403, 429, 503):
                delay = 1.5 * attempt
                log.warning(f"{url} -> {r.status_code}, retry in {delay:.1f}s")
                await asyncio.sleep(delay)
                continue

            r.raise_for_status()

        except httpx.HTTPStatusError as he:
            code = he.response.status_code if he.response else None
            log.error(f"HTTP {code} for {url}: {he}")
            break
        except Exception as e:
            log.error(f"Error fetching RSS {url}: {e}")
            await asyncio.sleep(1.0 * attempt)

    # Fallback: spróbuj znaleźć alternatywny link RSS na homepage
    try:
        html = await client.get(base, headers=headers)
        if html.status_code == 200:
            soup = BeautifulSoup(html.text, "html.parser")
            alt = soup.select_one('link[rel="alternate"][type*="rss"]')
            if alt and alt.get("href"):
                href = alt["href"]
                alt_url = href if "://" in href else base.rstrip("/") + "/" + href.lstrip("/")
                if alt_url != url:
                    log.info(f"Fallback: trying alt feed {alt_url}")
                    return await fetch_feed(client, alt_url)
    except Exception as e:
        log.warning(f"Fallback HTML failed for {base}: {e}")

    log.error(f"Feed blocked or unavailable: {url} (last_status={last_status})")
    return []

# --- SCRAPER (te same nagłówki/klient) ---
async def scrape_webpage(client: httpx.AsyncClient, url: str) -> List[Tuple[str, str]]:
    posts: List[Tuple[str, str]] = []
    headers = build_headers(url)
    try:
        try:
            await client.get(domain_root(url), headers=headers)
        except Exception:
            pass

        r = await client.get(url, headers=headers)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')

        if "travel-dealz.com" in url:
            for article in soup.select('article.article-item'):
                link_tag = article.select_one('h2.article-title a')
                if link_tag and (href := link_tag.get('href')):
                    posts.append((link_tag.get_text(strip=True), href))

        elif "secretflying.com" in url:
            for article in soup.select('article.post-item'):
                link_tag = article.select_one('.post-title a')
                if link_tag and (href := link_tag.get('href')):
                    posts.append((link_tag.get_text(strip=True), href))

        elif "wakacyjnipiraci.pl" in url:
            for article in soup.select('article.post-list__item'):
                link_tag = article.select_one('a.post-list__link')
                if link_tag and (href := link_tag.get('href')):
                    title_tag = article.select_one('h2.post-list__title')
                    if title_tag:
                        posts.append((title_tag.get_text(strip=True), href))

        log.info(f"Scraped {len(posts)} posts from {url}")
        return posts

    except httpx.HTTPStatusError as he:
        code = he.response.status_code if he.response else None
        log.error(f"Scrape HTTP {code} {url}: {he}")
        return []
    except Exception as e:
        log.error(f"Error scraping webpage {url}: {e}")
        return []

# --- TELEGRAM ---
async def send_telegram_message_async(client: httpx.AsyncClient, title: str, link: str):
    if not TELEGRAM_API_URL:
        log.error("TG_TOKEN is not set.")
        return
    message = f"<b>{title}</b>\n\n{link}"
    payload = {'chat_id': TG_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}
    try:
        r = await client.post(TELEGRAM_API_URL, json=payload, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        log.info(f"Message sent: {title[:80]}…")
    except Exception as e:
        log.error(f"Telegram error for link {link}: {e}")

# --- MAIN FLOW ---
async def process_feeds_async() -> str:
    log.info("Starting hybrid processing (RSS + Scraping).")
    if not TG_TOKEN or not TG_CHAT_ID:
        return "Missing TG_TOKEN/TG_CHAT_ID."

    rss_sources = get_rss_sources()
    web_sources = get_web_sources()
    if not rss_sources and not web_sources:
        return "No RSS or Web sources found."

    state, generation = load_state()
    sent_links_dict: Dict[str, str] = state.get("sent_links", {})

    async with make_async_client() as client:
        rss_tasks = [fetch_feed(client, url) for url in rss_sources]
        scrape_tasks = [scrape_webpage(client, url) for url in web_sources]
        results = await asyncio.gather(*(rss_tasks + scrape_tasks), return_exceptions=False)

    all_posts = [post for post_list in results for post in post_list]
    dbg(f"ALL_POSTS total={len(all_posts)}")

    new_posts: List[Tuple[str, str]] = []
    now_utc = datetime.now(timezone.utc)

    dup_count = 0
    dup_samples: List[str] = []

    for idx, (title, link) in enumerate(all_posts):
        canonical = canonicalize_url(link)
        if canonical not in sent_links_dict:
            new_posts.append((title, link))
            sent_links_dict[canonical] = now_utc.isoformat()
            dbg(f"NEW [{idx}] {canonical}  title='{title[:90]}'")
        else:
            dup_count += 1
            if len(dup_samples) < 5:
                dup_samples.append(canonical)
            dbg(f"DUP  [{idx}] {canonical}")

    log.info(
        f"Summary: seen={len(all_posts)}, new={len(new_posts)}, duplicates={dup_count}"
        + (f", dup_samples={dup_samples}" if DEBUG_FEEDS and dup_samples else "")
    )

    sent_count = 0
    if new_posts:
        log.info(f"Found {len(new_posts)} new posts. Sending.")
        async with make_async_client() as client:
            await asyncio.gather(*[
                send_telegram_message_async(client, t, l) for t, l in new_posts
            ])
        sent_count = len(new_posts)
        if DEBUG_FEEDS:
            sample_titles = [t[:90] for t, _ in new_posts[:5]]
            log.info(f"Sent sample titles: {sample_titles}")
    else:
        log.info("Found 0 new posts. Nothing to send.")

    # cleanup >30 dni
    thirty_days_ago = now_utc - timedelta(days=30)
    cleaned_sent_links = {
        link: ts for link, ts in sent_links_dict.items()
        if datetime.fromisoformat(ts) > thirty_days_ago
    }
    log.info(f"Memory cleanup: before={len(sent_links_dict)}, after={len(cleaned_sent_links)}")
    state["sent_links"] = cleaned_sent_links

    try:
        save_state_atomic(state, generation)
    except RuntimeError as e:
        log.critical(f"State save failure: {e}")
        return f"Critical: {e}"

    result_msg = f"Done. Sent {sent_count} posts."
    log.info(result_msg)
    return result_msg

# --- ROUTES ---
@app.get("/")
def index():
    return "Travel-Bot vHYBRID — działa!", 200

@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200

@app.post("/tg/webhook")
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    msg = update.get("message", {})
    text = msg.get("text")
    if text == "/start":
        async def _run():
            async with make_async_client() as client:
                await send_telegram_message_async(
                    client, title="Cześć!", link="Bot działa — poluję na okazje i sam po sobie sprzątam."
                )
        asyncio.run(_run())
    return "OK", 200

@app.post("/tasks/rss")
def handle_rss_task():
    log.info("Received /tasks/rss", extra={"event": "job_start", "ua": request.headers.get("User-Agent")})
    try:
        result = asyncio.run(process_feeds_async())
        code = 200 if "Critical" not in result else 500
        log.info("Finished /tasks/rss", extra={"event": "job_done", "status": code, "result": result})
        return result, code
    except Exception as e:
        log.exception("Unhandled error in /tasks/rss")
        return f"Server error: {e}", 500

# DODATKOWE, pomocne podglądy (opcjonalnie)
@app.get("/debug/state")
def debug_state():
    state, _ = load_state()
    sent = state.get("sent_links", {})
    last = sorted(sent.items(), key=lambda kv: kv[1], reverse=True)[:10]
    return jsonify({
        "count": len(sent),
        "sample_last": [{"url": k, "ts": v} for k, v in last]
    }), 200

@app.get("/debug/check")
def debug_check():
    return jsonify({
        "env": {
            "TG_CHAT_ID": os.environ.get("TG_CHAT_ID"),
            "BUCKET_NAME": os.environ.get("BUCKET_NAME"),
            "SENT_LINKS_FILE": os.environ.get("SENT_LINKS_FILE"),
            "HTTP_TIMEOUT": os.environ.get("HTTP_TIMEOUT"),
            "DEBUG_FEEDS": os.environ.get("DEBUG_FEEDS"),
        }
    }), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
