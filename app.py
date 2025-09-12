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

# --- KONFIG ---
TG_TOKEN = os.environ.get('TG_TOKEN')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID')
BUCKET_NAME = os.environ.get('BUCKET_NAME', 'travel-bot-storage-patrykmozeluk-cloud')
SENT_LINKS_FILE = os.environ.get('SENT_LINKS_FILE', 'sent_links.json')
HTTP_TIMEOUT = float(os.environ.get('HTTP_TIMEOUT', "20.0")) # Lekko zwiększony timeout

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage" if TG_TOKEN else None

# --- LOGI ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# --- APP / GCS ---
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
    return {"sent_links": []}

def load_state() -> Tuple[Dict[str, Any], int | None]:
    try:
        if not _blob.exists():
            log.info("State file not found, creating a default one.")
            return _default_state(), None
        _blob.reload()
        data = _blob.download_as_bytes()
        generation = _blob.generation
        log.info(f"State file loaded successfully (generation: {generation}).")
        return orjson.loads(data), generation
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
    try:
        r = await client.get(url, timeout=HTTP_TIMEOUT, follow_redirects=True)
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
    sent = set(state.get("sent_links", []))

    async with httpx.AsyncClient() as client:
        fetch_tasks = [fetch_feed(client, url) for url in rss]
        results = await asyncio.gather(*fetch_tasks)

    all_posts: List[Tuple[str, str]] = []
    for post_list in results: all_posts.extend(post_list)

    new_posts: List[Tuple[str, str]] = []
    for title, link in all_posts:
        canonical = canonicalize_url(link)
        if canonical not in sent:
            new_posts.append((title, link))
            sent.add(canonical)

    if not new_posts:
        log.info("Processing complete. No new posts found.")
        return "No new posts."

    log.info(f"Found {len(new_posts)} new posts. Preparing to send to Telegram.")
    async with httpx.AsyncClient() as client:
        send_tasks = [send_telegram_message_async(client, t, l) for t, l in new_posts]
        await asyncio.gather(*send_tasks)

    state["sent_links"] = list(sent)
    try:
        save_state_atomic(state, generation)
    except RuntimeError as e:
        log.critical(f"Failed to save state after sending messages: {e}")
        return f"Critical error: {e}"

    return f"Processing complete. Sent {len(new_posts)} new posts to Telegram."

# --- ROUTES ---
@app.get("/")
def index():
    # Zmieniony tekst, żebyśmy mieli 100% pewności, że nowa wersja się wdrożyła
    return "Travel-Bot vFINAL — działa!", 200

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
                    await send_telegram_message_async(client, title="Cześć!", link="Bot działa — poluję na okazje.")
            return asyncio.run(_run())
        _send()
    return "OK", 200

@app.post("/tasks/rss")
def handle_rss_task():
    log.info("Received request on /tasks/rss endpoint.")
    try:
        # Synchroniczne uruchomienie głównej logiki asynchronicznej
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
