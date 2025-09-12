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

# --- LOGGING ---
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- CONFIG (ENV) ---
TG_TOKEN = os.environ.get('TG_TOKEN')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID')
BUCKET_NAME = os.environ.get('BUCKET_NAME', 'travel-bot-storage-patrykmozeluk-cloud')
SENT_LINKS_FILE = os.environ.get('SENT_LINKS_FILE', 'sent_links.json')
HTTP_TIMEOUT = float(os.environ.get('HTTP_TIMEOUT', "15.0"))
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage" if TG_TOKEN else None

# Fail fast on critical env
missing_env = [k for k, v in {"TG_TOKEN": TG_TOKEN, "TG_CHAT_ID": TG_CHAT_ID}.items() if not v]
if missing_env:
    logger.error(f"Missing required env vars: {missing_env}. Cloud Run will start but endpoints will 500.")
# (Nie wyłączamy procesu – pozwalamy sprawdzić / i /healthz – ale /tasks/rss zwróci 500 z jasnym komunikatem)

# --- APP / CLIENTS ---
app = Flask(__name__)
storage_client = storage.Client()

# --- URL CANONICALIZATION ---
DROP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "igshid", "mc_cid", "mc_eid"
}

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

# --- STATE IN GCS ---
_bucket = storage_client.bucket(BUCKET_NAME)
_blob = _bucket.blob(SENT_LINKS_FILE)

def _default_state() -> Dict[str, Any]:
    return {"sent_links": []}

def load_state() -> Tuple[Dict[str, Any], int | None]:
    try:
        if not _blob.exists():
            logger.info("State file not found in GCS; starting with default.")
            return _default_state(), None
        _blob.reload()
        data = _blob.download_as_bytes()
        generation = _blob.generation
        logger.info(f"State loaded from GCS (generation: {generation}).")
        return orjson.loads(data), generation
    except Exception as e:
        logger.error(f"GCS read error: {e} — starting with default state.")
        return _default_state(), None

def save_state_atomic(state: Dict[str, Any], expected_generation: int | None, retries: int = 5):
    payload = orjson.dumps(state)
    for attempt in range(retries):
        try:
            if expected_generation is None:
                _blob.upload_from_string(payload, if_generation_match=0, content_type="application/json")
            else:
                _blob.upload_from_string(payload, if_generation_match=expected_generation, content_type="application/json")
            logger.info("State saved to GCS.")
            return
        except Exception as e:
            # PreconditionFailed when generation moved
            if "PreconditionFailed" in str(e) or "412" in str(e):
                logger.warning(f"GCS write conflict (attempt {attempt+1}/{retries}); reloading generation.")
                time.sleep(0.5)
                _, expected_generation = load_state()
                continue
            logger.error(f"GCS unexpected write error: {e}")
            raise
    raise RuntimeError("Failed to save state atomically after retries.")

# --- RSS ---
def get_rss_sources() -> List[str]:
    try:
        with open('rss_sources.txt', 'r', encoding='utf-8') as f:
            sources = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
        logger.info(f"RSS sources: {len(sources)}")
        return sources
    except FileNotFoundError:
        logger.error("rss_sources.txt not found in container.")
        return []

async def fetch_feed(client: httpx.AsyncClient, url: str) -> List[Tuple[str, str]]:
    posts: List[Tuple[str, str]] = []
    try:
        r = await client.get(url, timeout=HTTP_TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        feed = feedparser.parse(r.text)
        for entry in feed.entries:
            link = entry.get("link")
            title = entry.get("title")
            if link and title:
                posts.append((title, link))
        logger.info(f"Fetched {len(posts)} posts from {url}")
        return posts
    except Exception as e:
        logger.error(f"RSS error [{url}]: {e}")
        return []

async def send_telegram_message(client: httpx.AsyncClient, title: str, link: str):
    if not TELEGRAM_API_URL:
        logger.error("TG_TOKEN not set; skipping Telegram send.")
        return
    message = f"<b>{title}</b>\n\n{link}"
    payload = {'chat_id': TG_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}
    try:
        r = await client.post(TELEGRAM_API_URL, json=payload, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        logger.info(f"Sent: {title[:80]}…")
    except Exception as e:
        logger.error(f"Telegram send error for {link}: {e}")

async def process_feeds_async() -> str:
    if not TG_TOKEN or not TG_CHAT_ID:
        return "Missing TG_TOKEN/TG_CHAT_ID env."

    rss_sources = get_rss_sources()
    if not rss_sources:
        return "No RSS sources."

    state, generation = load_state()
    sent_links_cache = set(state.get("sent_links", []))

    # Pobieramy z ograniczoną równoległością (np. 6 naraz)
    sem = asyncio.Semaphore(int(os.environ.get("CONCURRENCY", "6")))
    async with httpx.AsyncClient() as client:
        async def _task(u: str):
            async with sem:
                return await fetch_feed(client, u)
        results = await asyncio.gather(*[_task(u) for u in rss_sources])

    all_posts: List[Tuple[str, str]] = []
    for post_list in results:
        all_posts.extend(post_list)

    new_posts: List[Tuple[str, str]] = []
    for title, link in all_posts:
        canonical_link = canonicalize_url(link)
        if canonical_link not in sent_links_cache:
            new_posts.append((title, link))
            sent_links_cache.add(canonical_link)

    if not new_posts:
        logger.info("No new posts.")
        return "No new posts."

    logger.info(f"New posts to send: {len(new_posts)}")

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*[send_telegram_message(client, t, l) for t, l in new_posts])

    # Uwaga: lista może rosnąć bez końca – produkcyjnie warto trymować do X ostatnich
    state["sent_links"] = list(sent_links_cache)
    try:
        save_state_atomic(state, generation)
    except RuntimeError as e:
        logger.critical(f"State save failure: {e} — duplicates likely next run.")
        return f"Critical: {e}"

    return f"Done. Sent {len(new_posts)} new posts."

# --- ROUTES ---
@app.get("/")
def index():
    return "Travel-Bot alive (Flask) — v2", 200

@app.get("/healthz")
def healthz():
    # Opcjonalny szybki test na GCS (nie krytykuj jeśli brak uprawnień – wypisz info)
    info = {"bucket": BUCKET_NAME, "sent_file": SENT_LINKS_FILE}
    try:
        exists = _blob.exists()
        info["state_exists"] = bool(exists)
        return jsonify({"status": "ok", "gcs": info}), 200
    except Exception as e:
        return jsonify({"status": "ok", "gcs_error": str(e), "gcs": info}), 200

@app.post("/tg/webhook")
async def telegram_webhook():
    update = request.get_json(silent=True) or {}
    msg = update.get("message", {})
    text = msg.get("text")
    if text == "/start":
        async with httpx.AsyncClient() as client:
            await send_telegram_message(client, title="Cześć!", link="Bot działa — poluję na okazje.")
    return "OK", 200

@app.post("/tasks/rss")
async def handle_rss_task():
    # Cloud Scheduler target
    try:
        result = await process_feeds_async()
        code = 200 if result and not result.lower().startswith("missing") else 500
        return result, code
    except Exception as e:
        logger.exception("Unhandled error in /tasks/rss")
        return f"Server error: {e}", 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    # Local dev only
    app.run(host="0.0.0.0", port=port, debug=True)
