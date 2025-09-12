import os
import logging
import asyncio
import httpx
import feedparser
import orjson
import time
import random # <-- NOWOŚĆ: Będziemy losować przebrania
from flask import Flask, request, jsonify
from google.cloud import storage
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from typing import Dict, Any, Tuple, List
from datetime import datetime, timedelta, timezone

# --- KONFIG ---
TG_TOKEN = os.environ.get('TG_TOKEN')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID')
BUCKET_NAME = os.environ.get('BUCKET_NAME', 'travel-bot-storage-patrykmozeluk-cloud')
SENT_LINKS_FILE = os.environ.get('SENT_LINKS_FILE', 'sent_links.json')
HTTP_TIMEOUT = float(os.environ.get('HTTP_TIMEOUT', "20.0"))

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage" if TG_TOKEN else None

# <-- NOWOŚĆ: Twoja szafa z przebraniami (User-Agents) ze starego kodu
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0',
    'Mozilla/5.0 (Linux; Android 10; SM-G960F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.101 Mobile Safari/537.36',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1'
]

# --- LOGI / APP / GCS ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)
app = Flask(__name__)
storage_client = storage.Client()
_bucket = storage_client.bucket(BUCKET_NAME)
_blob = _bucket.blob(SENT_LINKS_FILE)

# --- URL CANON ---
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

# --- STAN ---
def _default_state() -> Dict[str, Any]:
    return {"sent_links": {}}

def load_state() -> Tuple[Dict[str, Any], int | None]:
    try:
        if not _blob.exists():
            log.info("State file not found, creating a default one.")
            return _default_state(), None
        _blob.reload()
        data = _blob.download_as_bytes()
        generation = _blob.generation
        log.info(f"State file loaded successfully (generation: {generation}).")
        state_data = orjson.loads(data)
        if isinstance(state_data.get("sent_links"), list):
            log.warning("Old state format detected, converting to new format.")
            state_data["sent_links"] = {link: datetime.now(timezone.utc).isoformat() for link in state_data["sent_links"]}
        return state_data, generation
    except Exception as e:
        log.error(f"Error reading from GCS: {e}. Starting with an empty state.")
        return _default_state(), None

def save_state_atomic(state: Dict[str, Any], expected_generation: int | None, retries: int = 5):
    payload = orjson.dumps(state)
    for attempt in range(retries):
        try:
            if expected_generation is None:
                _blob.upload_from_string(payload, if_generation_match=0, content_type="application/json")
            else:
                _blob.upload_from_string(payload, if_generation_match=expected_generation, content_type="application/json")
            log.info("State file saved successfully.")
            return
        except Exception as e:
            if "PreconditionFailed" in str(e) or "412" in str(e):
                log.warning(f"GCS write conflict (attempt {attempt+1}/{retries}). Reloading state generation and retrying.")
                time.sleep(0.5)
                _, expected_generation = load_state()
                continue
            log.error(f"Critical error writing to GCS: {e}")
            raise
    raise RuntimeError("Atomic save failed after multiple retries.")

# --- RSS / TG ---
def get_rss_sources() -> List[str]:
    try:
        with open('rss_sources.txt', 'r', encoding='utf-8') as f:
            sources = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
        log.info(f"Found {len(sources)} RSS sources in rss_sources.txt.")
        return sources
    except FileNotFoundError:
        log.error("CRITICAL: rss_sources.txt not found in the container.")
        return []

async def fetch_feed(client: httpx.AsyncClient, url: str) -> List[Tuple[str, str]]:
    posts: List[Tuple[str, str]] = []
    # ZMIANA: Losujemy przebranie (User-Agent) z Twojej listy!
    headers = { 'User-Agent': random.choice(USER_AGENTS) }
    try:
        r = await client.get(url, timeout=HTTP_TIMEOUT, follow_redirects=True, headers=headers)
        r.raise_for_status()
        feed = feedparser.parse(r.text)
        for entry in feed.entries:
            t = entry.get("title"); l = entry.get("link")
            if t and l: posts.append((t, l))
        log.info(f"Fetched {len(posts)} posts from {url}")
        return posts
    except Exception as e:
        log.error(f"Error fetching RSS feed {url}: {e}")
        return []

async def send_telegram_message_async(client: httpx.AsyncClient, title: str, link: str):
    if not TELEGRAM_API_URL:
        log.error("TG_TOKEN is not set. Cannot send Telegram message.")
        return
    message = f"<b>{title}</b>\n\n{link}"
    payload = {'chat_id': TG_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}
    try:
        r = await client.post(TELEGRAM_API_URL, json=payload, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        log.info(f"Message sent to Telegram: {title[:80]}…")
    except Exception as e:
        log.error(f"Error sending Telegram message for link {link}: {e}")

async def process_feeds_async() -> str:
    log.info("Starting asynchronous feed processing.")
    if not TG_TOKEN or not TG_CHAT_ID:
        log.error("Missing TG_TOKEN or TG_CHAT_ID environment variables. Aborting.")
        return "Missing TG_TOKEN/TG_CHAT_ID env."

    rss = get_rss_sources()
    if not rss:
        return "No RSS sources found to process."

    state, generation = load_state()
    sent_links_dict = state.get("sent_links", {})
    
    async with httpx.AsyncClient() as client:
        fetch_tasks = [fetch_feed(client, url) for url in rss]
        results = await asyncio.gather(*fetch_tasks)

    all_posts: List[Tuple[str, str]] = []
    for post_list in results: all_posts.extend(post_list)

    new_posts: List[Tuple[str, str]] = []
    now_utc = datetime.now(timezone.utc)
    for title, link in all_posts:
        canonical = canonicalize_url(link)
        if canonical not in sent_links_dict:
            new_posts.append((title, link))
            sent_links_dict[canonical] = now_utc.isoformat()

    if not new_posts:
        log.info("No new posts found.")
    else:
        log.info(f"Found {len(new_posts)} new posts. Preparing to send to Telegram.")
        async with httpx.AsyncClient() as client:
            send_tasks = [send_telegram_message_async(client, t, l) for t, l in new_posts]
            await asyncio.gather(*send_tasks)

    thirty_days_ago = now_utc - timedelta(days=30)
    cleaned_sent_links = {}
    for link, timestamp_str in sent_links_dict.items():
        try:
            timestamp = datetime.fromisoformat(timestamp_str)
            if timestamp > thirty_days_ago:
                cleaned_sent_links[link] = timestamp_str
        except (TypeError, ValueError):
            cleaned_sent_links[link] = now_utc.isoformat()

    log.info(f"Memory cleanup: before={len(sent_links_dict)}, after={len(cleaned_sent_links)}")
    state["sent_links"] = cleaned_sent_links
    
    try:
        save_state_atomic(state, generation)
    except RuntimeError as e:
        log.critical(f"Failed to save state after sending messages: {e}")
        return f"Critical error: {e}"

    if new_posts:
        return f"Processing complete. Sent {len(new_posts)} new posts to Telegram."
    else:
        return "Processing complete. No new posts."

# --- ROUTES & MAIN ---
@app.get("/")
def index():
    # Nowa wersja, żeby wiedzieć, że to mistrz przebrań
    return "Travel-Bot vULTIMATE — działa!", 200

@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200

@app.post("/tg/webhook")
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    msg = update.get("message", {})
    text = msg.get("text")
    if text == "/start":
        def _send():
            async def _run():
                async with httpx.AsyncClient() as client:
                    await send_telegram_message_async(client, title="Cześć!", link="Bot działa — poluję na okazje i sam po sobie sprzątam.")
            return asyncio.run(_run())
        _send()
    return "OK", 200

@app.post("/tasks/rss")
def handle_rss_task():
    log.info("Received request on /tasks/rss endpoint.")
    try:
        result = asyncio.run(process_feeds_async())
        log.info(f"RSS task finished with result: {result}")
        code = 200
        if "Missing" in result or "Critical" in result:
            code = 500
        return result, code
    except Exception as e:
        log.exception("An unhandled exception occurred in /tasks/rss.")
        return f"Internal Server Error: {e}", 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
