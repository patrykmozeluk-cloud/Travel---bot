# -*- coding: utf-8 -*-
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
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, unquote, urljoin
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
TELEGRAM_SECRET = env("TELEGRAM_SECRET") # opcjonalny sekret do weryfikacji nagłówka
ENABLE_WEBHOOK = env("ENABLE_WEBHOOK", "1") in {"1", "true", "True", "yes", "YES"}
DISABLE_DEDUP = env("DISABLE_DEDUP", "0") in {"1", "true", "True", "yes", "YES"}
DEBUG_FEEDS = env("DEBUG_FEEDS", "0") in {"1", "true", "True", "yes", "YES"}

# OGRANICZ liczbę pracy per wywołanie, aby zmieścić się w budżecie czasu
MAX_POSTS_PER_RUN = int(env("MAX_POSTS_PER_RUN", "15"))  # było: 0 (bez limitu)
CONCURRENCY = int(env("CONCURRENCY", "8"))               # semafor na fetch/scrape/send
FETCH_TIMEOUT_S = float(env("FETCH_TIMEOUT_S", "10"))    # timebox pojedynczego fetch/scrape
SEND_TIMEOUT_S  = float(env("SEND_TIMEOUT_S", "8"))      # timebox pojedynczego send
JOB_TIMEOUT_S   = float(env("JOB_TIMEOUT_S", "55"))      # timebox całego joba /tasks/rss

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

def make_async_client() -> httpx.AsyncClient:
    limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
    return httpx.AsyncClient(
        headers=BASE_HEADERS,
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
        http2=True,
        limits=limits,
        cookies=httpx.Cookies()
    )

# --- CONCURRENCY CONTROL ---
SEM = None  # zainicjalizujemy w runtime wg CONCURRENCY

async def _bounded(coro):
    async with SEM:
        return await coro

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
        if img_tag and img_tag.get("src"):
            src = img_tag["src"].strip()
            if src:
                return urljoin(url, src)  # obsługa ścieżek względnych
    except Exception:
        pass
    return None

# --- RETRY helper (Telegram / inne POST-y) ---
async def _post_with_retry(client: httpx.AsyncClient, url: str, payload: Dict[str, Any], attempts: int = 3):
    for i in range(attempts):
        r = await client.post(url, json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code in (429, 500, 502, 503, 504):
            backoff = 2 ** i
            log.warning(f"POST {url} -> {r.status_code}, retry in {backoff}s")
            await asyncio.sleep(backoff)
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r  # formalnie unreachable

# --- TELEGRAM SENDER ---
async def send_telegram_message_async(title: str, link: str, client: httpx.AsyncClient, chat_id: str | None = None) -> bool:
    chat = chat_id or TG_CHAT_ID
    if not TG_TOKEN or not chat:
        log.error("Brak TG_TOKEN/TG_CHAT_ID.")
        return False

    safe_title = html.escape(title, quote=False)
    caption = f"<b>{safe_title}</b>\n\n{link}"

    try:
        img = await find_og_image(client, link)

        if img:
            method, payload = "sendPhoto", {"photo": img, "caption": caption}
        else:
            method, payload = "sendMessage", {"text": caption, "disable_web_page_preview": False}

        payload.update({"chat_id": chat, "parse_mode": "HTML"})
        tg_url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"

        r = await asyncio.wait_for(_post_with_retry(client, tg_url, payload, attempts=3), timeout=SEND_TIMEOUT_S)
        body_text = r.text

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
    try:
        r = await client.get(url, headers=build_headers(url))
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
    try:
        r = await client.get(url, headers=build_headers(url))
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
                href = link_tag.get('href')
                if href:
                    href = urljoin(url, href)  # ważne dla hrefów względnych
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
    global SEM
    SEM = asyncio.Semaphore(CONCURRENCY)

    t_start = time.perf_counter()
    log.info("Starting hybrid processing.")
    if not TG_TOKEN or not TG_CHAT_ID:
        return "Missing TG_TOKEN/TG_CHAT_ID."

    sources = get_rss_sources() + get_web_sources()
    if not sources:
        return "No sources found."

    random.shuffle(sources)
    state, generation = load_state()
    sent_links_dict: Dict[str, str] = state.get("sent_links", {})

    all_posts: List[Tuple[str, str]] = []
    async with make_async_client() as client:
        # 1) Faza fetch/scrape (równoległa, ale ograniczona semaforem i timeboxem każdego zadania)
        tasks = [
            _bounded(fetch_feed(client, url)) if any(k in url for k in ("rss","feed",".xml"))
            else _bounded(scrape_webpage(client, url))
            for url in sources
        ]
        results = await asyncio.gather(
            *[asyncio.wait_for(t, timeout=FETCH_TIMEOUT_S) for t in tasks],
            return_exceptions=True
        )
        for res in results:
            if isinstance(res, Exception):
                log.warning(f"source task failed: {res}")
                continue
            all_posts.extend(res)

        # ewentualny limit
        if MAX_POSTS_PER_RUN > 0:
            all_posts = all_posts[:MAX_POSTS_PER_RUN]

        # 2) Deduplikacja / przygotowanie kandydatów
        candidates: List[Tuple[str, str]] = []
        for title, link in all_posts:
            canonical = canonicalize_url(link)
            if DISABLE_DEDUP or canonical not in sent_links_dict:
                candidates.append((title, link))
                sent_links_dict[canonical] = "processing"  # tymczasowy stan

        unique_candidates = list({canonicalize_url(l): (t, l) for t, l in candidates}.values())
        log.info(f"Summary: seen={len(all_posts)}, new_candidates={len(unique_candidates)}")

        # 3) Wysyłka do TG (równoległa, ale z limitem + timebox + retry)
        sent_count = 0
        now_UTC = datetime.now(timezone.utc).isoformat()
        if unique_candidates:
            send_tasks = [
                _bounded(send_telegram_message_async(t, l, client))
                for t, l in unique_candidates
            ]
            send_results = await asyncio.gather(
                *[asyncio.wait_for(t, timeout=SEND_TIMEOUT_S) for t in send_tasks],
                return_exceptions=True
            )
            for (t, l), ok in zip(unique_candidates, send_results):
                if isinstance(ok, Exception):
                    log.error(f"send failed for {l}: {ok}")
                    continue
                if ok:
                    sent_links_dict[canonicalize_url(l)] = now_UTC
                    sent_count += 1

    # 4) Czyszczenie starych i porządkowanie stanu
    horizon = datetime.now(timezone.utc) - timedelta(days=30)
    cleaned_sent_links = {
        link: ts for link, ts in sent_links_dict.items()
        if ts != "processing" and datetime.fromisoformat(ts.replace("Z", "+00:00")) > horizon
    }
    state["sent_links"] = cleaned_sent_links

    try:
        save_state_atomic(state, generation)
    except RuntimeError as e:
        log.critical(f"State save failure: {e}")
        return f"Critical: {e}"

    took = time.perf_counter() - t_start
    result_msg = f"Done. Sent {sent_count} of {len(unique_candidates)} new posts. took_s={took:.3f}"
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
    # opcjonalne: prosty sekret w nagłówku, jeśli TELEGRAM_SECRET ustawione
    if TELEGRAM_SECRET:
        hdr = request.headers.get("X-Task-Secret", "")
        if hdr != TELEGRAM_SECRET:
            log.warning("Unauthorized: bad X-Task-Secret")
            return "unauthorized", 401

    log.info("Received /tasks/rss", extra={"event": "job_start", "ua": request.headers.get("User-Agent")})
    try:
        result = asyncio.run(asyncio.wait_for(process_feeds_async(), timeout=JOB_TIMEOUT_S))
        code = 200 if "Critical" not in result else 500
        return result, code
    except asyncio.TimeoutError:
        log.error(f"Timeout: job exceeded {JOB_TIMEOUT_S}s")
        return f"Timeout: exceeded {JOB_TIMEOUT_S}s", 504
    except Exception as e:
        log.exception("Unhandled error in /tasks/rss")
        return f"Server error: {e}", 500

if __name__ == "__main__":
    port = int(env("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
