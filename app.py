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
HTTP_TIMEOUT = float(os.environ.get('HTTP_TIMEOUT', "15.0"))

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
            log.info("State not found → default.")
            return _default_state(), None
        _blob.reload()
        data = _blob.download_as_bytes()
        generation = _blob.generation
        log.info(f"State loaded (gen={generation}).")
        return orjson.loads(data), generation
    except Exception as e:
        log.error(f"GCS read error: {e} → default.")
        return _default_state(), None

def save_state_atomic(state: Dict[str, Any], expected_generation: int | None, retries: int = 5):
    payload = orjson.dumps(state)
    for attempt in range(retries):
        try:
            if expected_generation is None:
                _blob.upload_from_string(payload, if_generation_match=0, content_type="application/json")
            else:
                _blob.upload_from_string(payload, if_generation_match=expected_generation, content_type="application/json")
            log.info("State saved.")
            return
        except Exception as e:
            if "PreconditionFailed" in str(e) or "412" in str(e):
                log.warning(f"GCS conflict (attempt {attempt+1}/{retries}) → reload gen")
                time.sleep(0.5)
                _, expected_generation = load_state()
                continue
            log.error(f"GCS write error: {e}")
            raise
    raise RuntimeError("Atomic save failed after retries.")

# --- RSS / TG (ASYNC FUNKCJE, ALE WYWOŁYWANE SYNCHRONICZNIE PRZEZ asyncio.run) ---
def get_rss_sources() -> List[str]:
    try:
        with open('rss_sources.txt', 'r', encoding='utf-8') as f:
            sources = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
        log.info(f"RSS sources: {len(sources)}")
        return sources
    except FileNotFoundError:
        log.error("rss_sources.txt not found in container.")
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
        log.info(f"Fetched {len(posts)} from {url}")
        return posts
    except Exception as e:
        log.error(f"RSS error {url}: {e}")
        return []

async def send_telegram_message_async(client: httpx.AsyncClient, title: str, link: str):
    if not TELEGRAM_API_URL:
        log.error("TG_TOKEN missing → skip send.")
        return
    message = f"<b>{title}</b>\n\n{link}"
    payload = {'chat_id': TG_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}
    try:
        r = await client.post(TELEGRAM_API_URL, json=payload, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        log.info(f"Sent: {title[:80]}…")
    except Exception as e:
        log.error(f"Telegram send error for {link}: {e}")

async def process_feeds_async() -> str:
    if not TG_TOKEN or not TG_CHAT_ID:
        return "Missing TG_TOKEN/TG_CHAT_ID env."

    rss = get_rss_sources()
    if not rss:
        return "No RSS sources."

    state, generation = load_state()
    sent = set(state.get("sent_links", []))

    sem = asyncio.Semaphore(int(os.environ.get("CONCURRENCY", "6")))
    async with httpx.AsyncClient() as client:
        async def _task(u: str):
            async with sem:
                return await fetch_feed(client, u)
        results = await asyncio.gather(*[_task(u) for u in rss])

    all_posts: List[Tuple[str, str]] = []
    for lst in results: all_posts.extend(lst)

    new_posts: List[Tuple[str, str]] = []
    for title, link in all_posts:
        c = canonicalize_url(link)
        if c not in sent:
            new_posts.append((title, link))
            sent.add(c)

    if not new_posts:
        log.info("No new posts.")
        return "No new posts."

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*[send_telegram_message_async(client, t, l) for t, l in new_posts])

    state["sent_links"] = list(sent)
    try:
        save_state_atomic(state, generation)
    except RuntimeError as e:
        log.critical(f"State save failure: {e}")
        return f"Critical: {e}"

    return f"Done. Sent {len(new_posts)} new posts."

# --- ROUTES (WSZYSTKIE SYNCHRONICZNE) ---
@app.get("/")
def index():
    return "Travel-Bot vSYNC-1 — działa", 200

@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200

@app.post("/tg/webhook")
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    msg = update.get("message", {})
    text = msg.get("text")
    if text == "/start":
        # synchroniczne wywołanie async – jednorazowo
        def _send():
            async def _run():
                async with httpx.AsyncClient() as client:
                    await send_telegram_message_async(client, title="Cześć!", link="Bot działa — poluję na okazje.")
            return asyncio.run(_run())
        _send()
    return "OK", 200

@app.post("/tasks/rss")
def handle_rss_task():
    try:
        result = asyncio.run(process_feeds_async())  # <-- synchronicznie wołamy async
        code = 200 if result and not result.lower().startswith("missing") else 500
        return result, code
    except Exception as e:
        log.exception("Unhandled in /tasks/rss")
        return f"Server error: {e}", 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
