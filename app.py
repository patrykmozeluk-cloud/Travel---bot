import os
import logging
import asyncio
import httpx
import feedparser
import orjson
import time
import random
from flask import Flask, request, jsonify
from google.cloud import storage
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from typing import Dict, Any, Tuple, List
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup

# --- KONFIG I CAŁA RESZTA BEZ ZMIAN ---
# ... (Wszystkie sekcje aż do "LOGIKA SCRAPINGU" pozostają identyczne)
TG_TOKEN = os.environ.get('TG_TOKEN')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID')
BUCKET_NAME = os.environ.get('BUCKET_NAME', 'travel-bot-storage-patrykmozeluk-cloud')
SENT_LINKS_FILE = os.environ.get('SENT_LINKS_FILE', 'sent_links.json')
HTTP_TIMEOUT = float(os.environ.get('HTTP_TIMEOUT', "20.0"))
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage" if TG_TOKEN else None
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0',
    'Mozilla/5.0 (Linux; Android 10; SM-G960F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.101 Mobile Safari/537.36',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1'
]
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)
app = Flask(__name__)
storage_client = storage.Client()
_bucket = storage_client.bucket(BUCKET_NAME)
_blob = _bucket.blob(SENT_LINKS_FILE)
DROP_PARAMS = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","fbclid","gclid","igshid","mc_cid","mc_eid"}
def canonicalize_url(url: str) -> str:
    try:
        p = urlparse(url.strip()); scheme = p.scheme.lower() or "https"; netloc = p.netloc.lower(); path = p.path or "/"; q = sorted([(k, v) for k, v in parse_qsl(p.query) if k.lower() not in DROP_PARAMS]); return urlunparse((scheme, netloc, path, p.params, urlencode(q), ""))
    except Exception: return url.strip()
def _default_state() -> Dict[str, Any]: return {"sent_links": {}}
def load_state() -> Tuple[Dict[str, Any], int | None]:
    try:
        if not _blob.exists(): return _default_state(), None
        _blob.reload(); data = _blob.download_as_bytes(); generation = _blob.generation
        state_data = orjson.loads(data)
        if isinstance(state_data.get("sent_links"), list): state_data["sent_links"] = {link: datetime.now(timezone.utc).isoformat() for link in state_data["sent_links"]}
        return state_data, generation
    except Exception: return _default_state(), None
def save_state_atomic(state: Dict[str, Any], expected_generation: int | None, retries: int = 5):
    payload = orjson.dumps(state)
    for attempt in range(retries):
        try:
            if expected_generation is None: _blob.upload_from_string(payload, if_generation_match=0, content_type="application/json")
            else: _blob.upload_from_string(payload, if_generation_match=expected_generation, content_type="application/json")
            return
        except Exception as e:
            if "PreconditionFailed" in str(e) or "412" in str(e): time.sleep(0.5); _, expected_generation = load_state(); continue
            raise
    raise RuntimeError("Atomic save failed.")

# --- LOGIKA SCRAPINGU (ZMIANY TUTAJ!) ---

def get_web_sources() -> List[str]:
    # ... (ta funkcja pozostaje bez zmian)
    try:
        with open('web_sources.txt', 'r', encoding='utf-8') as f: sources = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]; log.info(f"Found {len(sources)} web sources for scraping."); return sources
    except FileNotFoundError: log.info("web_sources.txt not found. Skipping scraping."); return []

async def scrape_webpage(client: httpx.AsyncClient, url: str) -> List[Tuple[str, str]]:
    posts: List[Tuple[str, str]] = []
    headers = { 'User-Agent': random.choice(USER_AGENTS) }
    try:
        r = await client.get(url, timeout=HTTP_TIMEOUT, follow_redirects=True, headers=headers)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        
        # --- Specyficzna logika dla travel-dealz.com ---
        if "travel-dealz.com" in url:
            for article in soup.select('article.article-item'):
                if link_tag := article.select_one('h2.article-title a'):
                    if link := link_tag.get('href'): posts.append((link_tag.get_text(strip=True), link))
        
        # --- NOWOŚĆ: Specyficzna logika dla secretflying.com ---
        elif "secretflying.com" in url:
            for article in soup.select('article.post-item'):
                if link_tag := article.select_one('.post-title a'):
                    if link := link_tag.get('href'): posts.append((link_tag.get_text(strip=True), link))

        # --- NOWOŚĆ: Specyficzna logika dla wakacyjnipiraci.pl ---
        elif "wakacyjnipiraci.pl" in url:
            for article in soup.select('article.post-list__item'):
                if link_tag := article.select_one('a.post-list__link'):
                     if link := link_tag.get('href'):
                        # Tytuł jest w innym miejscu
                        if title_tag := article.select_one('h2.post-list__title'):
                            posts.append((title_tag.get_text(strip=True), link))

        log.info(f"Scraped {len(posts)} posts from {url}")
        return posts
    except Exception as e:
        log.error(f"Error scraping webpage {url}: {e}")
        return []

# --- RSS / TG / GŁÓWNA LOGIKA / ROUTES ---
# ... (reszta kodu pozostaje DOKŁADNIE taka sama)
def get_rss_sources() -> List[str]:
    try:
        with open('rss_sources.txt', 'r', encoding='utf-8') as f: sources = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]; log.info(f"Found {len(sources)} RSS sources."); return sources
    except FileNotFoundError: log.error("CRITICAL: rss_sources.txt not found."); return []
async def fetch_feed(client: httpx.AsyncClient, url: str) -> List[Tuple[str, str]]:
    posts: List[Tuple[str, str]] = []; headers = { 'User-Agent': random.choice(USER_AGENTS) }
    try:
        r = await client.get(url, timeout=HTTP_TIMEOUT, follow_redirects=True, headers=headers); r.raise_for_status()
        feed = feedparser.parse(r.text)
        for entry in feed.entries:
            if (t := entry.get("title")) and (l := entry.get("link")): posts.append((t, l))
        log.info(f"Fetched {len(posts)} posts from {url}"); return posts
    except Exception as e: log.error(f"Error fetching RSS feed {url}: {e}"); return []
async def send_telegram_message_async(client: httpx.AsyncClient, title: str, link: str):
    if not TELEGRAM_API_URL: log.error("TG_TOKEN is not set."); return
    message = f"<b>{title}</b>\n\n{link}"; payload = {'chat_id': TG_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}
    try:
        r = await client.post(TELEGRAM_API_URL, json=payload, timeout=HTTP_TIMEOUT); r.raise_for_status()
        log.info(f"Message sent: {title[:80]}…")
    except Exception as e: log.error(f"Telegram error for link {link}: {e}")
async def process_feeds_async() -> str:
    log.info("Starting hybrid processing (RSS + Scraping).")
    if not TG_TOKEN or not TG_CHAT_ID: return "Missing TG_TOKEN/TG_CHAT_ID."
    rss_sources = get_rss_sources(); web_sources = get_web_sources()
    if not rss_sources and not web_sources: return "No RSS or Web sources found."
    state, generation = load_state(); sent_links_dict = state.get("sent_links", {})
    async with httpx.AsyncClient() as client:
        rss_tasks = [fetch_feed(client, url) for url in rss_sources]; scrape_tasks = [scrape_webpage(client, url) for url in web_sources]
        results = await asyncio.gather(*(rss_tasks + scrape_tasks))
    all_posts = [post for post_list in results for post in post_list]
    new_posts, now_utc = [], datetime.now(timezone.utc)
    for title, link in all_posts:
        canonical = canonicalize_url(link)
        if canonical not in sent_links_dict: new_posts.append((title, link)); sent_links_dict[canonical] = now_utc.isoformat()
    if new_posts:
        log.info(f"Found {len(new_posts)} new posts. Sending.")
        async with httpx.AsyncClient() as client: await asyncio.gather(*[send_telegram_message_async(client, t, l) for t, l in new_posts])
    thirty_days_ago = now_utc - timedelta(days=30)
    cleaned_sent_links = {link: ts for link, ts in sent_links_dict.items() if datetime.fromisoformat(ts) > thirty_days_ago}
    log.info(f"Memory cleanup: before={len(sent_links_dict)}, after={len(cleaned_sent_links)}")
    state["sent_links"] = cleaned_sent_links
    try: save_state_atomic(state, generation)
    except RuntimeError as e: log.critical(f"State save failure: {e}"); return f"Critical: {e}"
    return f"Done. Sent {len(new_posts)} posts." if new_posts else "Done. No new posts."
@app.get("/")
def index(): return "Travel-Bot vHYBRID — działa!", 200 # Nowa wersja, żeby wiedzieć, że to scraper i czytnik RSS
@app.get("/healthz")
def healthz(): return jsonify({"status": "ok"}), 200
@app.post("/tg/webhook")
def telegram_webhook():
    update = request.get_json(silent=True) or {}; msg = update.get("message", {}); text = msg.get("text")
    if text == "/start":
        def _send():
            async def _run():
                async with httpx.AsyncClient() as client: await send_telegram_message_async(client, title="Cześć!", link="Bot działa — poluję na okazje i sam po sobie sprzątam.")
            asyncio.run(_run())
        _send()
    return "OK", 200
@app.post("/tasks/rss")
def handle_rss_task():
    log.info("Received request on /tasks/rss endpoint.")
    try:
        result = asyncio.run(process_feeds_async()); code = 200 if "Critical" not in result else 500
        return result, code
    except Exception as e: log.exception("Unhandled error in /tasks/rss"); return f"Server error: {e}", 500
if __name__ == "__main__": port = int(os.environ.get("PORT", 8080)); app.run(host="0.0.0.0", port=port, debug=True)
