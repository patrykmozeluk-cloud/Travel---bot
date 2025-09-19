import os
import logging
import asyncio
import time
import random
from typing import Dict, Any, Tuple, List
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, unquote

import httpx
import feedparser
import orjson
from flask import Flask, request, jsonify
from google.cloud import storage
from bs4 import BeautifulSoup
import html as html_mod

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

# ---------- APP / GCS ----------
app = Flask(__name__)
storage_client = storage.Client()

# ---------- ENV ----------
def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name, default)
    return v if v is None else v.strip()

TG_TOKEN = env("TG_TOKEN")
TG_CHAT_ID = env("TG_CHAT_ID")
BUCKET_NAME = env("BUCKET_NAME", "travel-bot-storage-patrykmozeluk-cloud")
SENT_LINKS_FILE = env("SENT_LINKS_FILE", "sent_links.json")
HTTP_TIMEOUT = float(env("HTTP_TIMEOUT", "20.0"))
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL")
TELEGRAM_SECRET = env("TELEGRAM_SECRET")
ENABLE_WEBHOOK = env("ENABLE_WEBHOOK", "1") in {"1","true","True","yes","YES"}
DISABLE_DEDUP = env("DISABLE_DEDUP", "0") in {"1","true","True","yes","YES"}
DEBUG_FEEDS = env("DEBUG_FEEDS", "0") in {"1","true","True","yes","YES"}
MAX_POSTS_PER_RUN = int(env("MAX_POSTS_PER_RUN", "0"))  # 0 = brak limitu

# --- Telegram rate-limit (bez env, stałe, minimalna ingerencja) ---
TG_RATE_INTERVAL = 1.1  # sekundy między wiadomościami do tego samego czatu
_tg_last_send_ts: float | None = None
_tg_send_lock = asyncio.Lock()

async def _tg_throttle():
    global _tg_last_send_ts
    async with _tg_send_lock:
        now = time.monotonic()
        if _tg_last_send_ts is not None:
            wait = _tg_last_send_ts + TG_RATE_INTERVAL - now
            if wait > 0:
                await asyncio.sleep(wait)
        _tg_last_send_ts = time.monotonic()
        
# TTL
DELETE_AFTER_HOURS = int(env("DELETE_AFTER_HOURS", "24"))
DEDUP_TTL_HOURS = int(env("DEDUP_TTL_HOURS", "24"))

# limity
MAX_PER_DOMAIN = int(env("MAX_PER_DOMAIN", "5"))
PER_HOST_CONCURRENCY = int(env("PER_HOST_CONCURRENCY", "2"))
JITTER_MIN_MS = int(env("JITTER_MIN_MS", "120"))
JITTER_MAX_MS = int(env("JITTER_MAX_MS", "400"))

# wrażliwe domeny: tytuł bez dodatków
TITLE_ONLY_DOMAINS = {"secretflying.com", "wakacyjnipiraci.pl"}

def dbg(msg: str):
    if DEBUG_FEEDS:
        log.info(f"DEBUG {msg}")

# ---------- GCS STATE ----------
_bucket = storage_client.bucket(BUCKET_NAME)
_blob = _bucket.blob(SENT_LINKS_FILE)

DROP_PARAMS = {
    "utm_source","utm_medium","utm_campaign","utm_term","utm_content",
    "fbclid","gclid","igshid","mc_cid","mc_eid","ref","ref_src","src"
}

def _strip_default_port(netloc: str, scheme: str) -> str:
    if scheme == "http" and netloc.endswith(":80"):
        return netloc[:-3]
    if scheme == "https" and netloc.endswith(":443"):
        return netloc[:-4]
    return netloc

def canonicalize_url(url: str) -> str:
    try:
        u = unquote(url.strip())
        p = urlparse(u)
        scheme = (p.scheme or "https").lower()
        netloc = p.netloc.lower()
        for pref in ("www.", "m.", "amp."):
            if netloc.startswith(pref):
                netloc = netloc[len(pref):]
        netloc = _strip_default_port(netloc, scheme)
        path = p.path or "/"
        while "//" in path:
            path = path.replace("//", "/")
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in DROP_PARAMS]
        q.sort()
        return urlunparse((scheme, netloc, path, p.params, urlencode(q, doseq=True), ""))
    except Exception:
        return url.strip()

def _default_state() -> Dict[str, Any]:
    return {"sent_links": {}, "delete_queue": []}

def _ensure_state_shapes(state: Dict[str, Any]) -> None:
    if "sent_links" not in state or not isinstance(state["sent_links"], dict):
        state["sent_links"] = {}
    if "delete_queue" not in state or not isinstance(state["delete_queue"], list):
        state["delete_queue"] = []

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
                link: datetime.now(timezone.utc).isoformat() for link in state_data["sent_links"]
            }
        _ensure_state_shapes(state_data)
        return state_data, generation
    except Exception as e:
        log.warning(f"load_state fallback: {e}")
        return _default_state(), None

def save_state_atomic(state: Dict[str, Any], expected_generation: int | None, retries: int = 10):
    payload = orjson.dumps(state)
    for _ in range(retries):
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

# ---------- HEADERS / CLIENT ----------
STICKY_IDENTITY: Dict[str, Dict[str, str]] = {
    "travel-dealz.com": {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
        "al": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "referer": "https://www.google.com/",
    },
    "secretflying.com": {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
        "al": "en-US,en;q=0.9",
        "referer": "https://secretflying.com/",
    },
    "theflightdeal.com": {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
        "al": "en-US,en;q=0.9",
        "referer": "https://www.google.com/",
    },
    "wakacyjnipiraci.pl": {
        "ua": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Mobile Safari/537.36",
        "al": "pl-PL,pl;q=0.9,en-US;q=0.8",
        "referer": "https://wakacyjnipiraci.pl/",
        "rss_no_brotli": "1",
        "rss_accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7",
    },
    "travelfree.info": {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
        "al": "en-US,en;q=0.9",
        "referer": "https://travelfree.info/",
        "rss_no_brotli": "1",
        "rss_accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7",
    },
    "twomonkeystravelgroup.com": {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
        "al": "en-US,en;q=0.9",
        "referer": "https://twomonkeystravelgroup.com/",
        "rss_no_brotli": "1",
        "rss_accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7",
    },
    "thebarefootnomad.com": {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
        "al": "en-US,en;q=0.9",
        "referer": "https://www.thebarefootnomad.com/",
        "rss_no_brotli": "1",
        "rss_accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7",
    },
}

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

def build_headers(url: str) -> Dict[str, str]:
    p = urlparse(url)
    host = p.netloc.lower().replace("www.", "")
    h = dict(BASE_HEADERS)
    pathq = (p.path or "") + ("?" + p.query if p.query else "")
    is_rss = any(tok in pathq.lower() for tok in ("/feed", "rss", ".xml", "?feed"))
    ident = STICKY_IDENTITY.get(host)
    if ident:
        h["User-Agent"] = ident["ua"]
        h["Accept-Language"] = ident["al"]
        h["Referer"] = ident["referer"]
        if is_rss and ident.get("rss_no_brotli") == "1":
            h["Accept-Encoding"] = "gzip, deflate"
            h["Accept"] = ident.get("rss_accept", h.get("Accept", "application/xml,*/*;q=0.7"))
    else:
        h["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"
        h["Accept-Language"] = "en-US,en;q=0.9"
        h["Referer"] = f"{p.scheme}://{p.netloc}/"
    return h

def domain_root(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"

def make_async_client() -> httpx.AsyncClient:
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=40)
    return httpx.AsyncClient(
        headers=BASE_HEADERS,
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
        http2=True,
        limits=limits,
        cookies=httpx.Cookies(),
    )

# ---------- SOURCES ----------
def get_web_sources() -> List[str]:
    try:
        with open("web_sources.txt", "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
    except FileNotFoundError:
        return []

def get_rss_sources() -> List[str]:
    try:
        with open("rss_sources.txt", "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
    except FileNotFoundError:
        return []

# ---------- CONCURRENCY ----------
_host_semaphores: Dict[str, asyncio.Semaphore] = {}

def _sem_for(url: str) -> asyncio.Semaphore:
    host = urlparse(url).netloc.lower()
    if host not in _host_semaphores:
        _host_semaphores[host] = asyncio.Semaphore(PER_HOST_CONCURRENCY)
    return _host_semaphores[host]

async def _jitter():
    await asyncio.sleep(random.uniform(JITTER_MIN_MS/1000.0, JITTER_MAX_MS/1000.0))

# ---------- TELEGRAM ----------
async def send_telegram_message_async(title: str, link: str, chat_id: str | None = None) -> bool:
    chat = chat_id or TG_CHAT_ID
    if not TG_TOKEN or not chat:
        log.error("Brak TG_TOKEN/TG_CHAT_ID.")
        return False

    safe_title = html_mod.escape(title or "", quote=False)
    text = f"<b>{safe_title}</b>\n\n{link}"
    payload = {
        "chat_id": str(chat),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        async with make_async_client() as client:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

            # dławiemy tempo, żeby nie wpadać w 429
            await _tg_throttle()

            r = await client.post(url, json=payload, timeout=HTTP_TIMEOUT)

            # proste retry przy 429 Too Many Requests
            for attempt in range(3):
                if r.status_code != 429:
                    break
                try:
                    retry_after = float(r.headers.get("Retry-After", "1"))
                except Exception:
                    retry_after = 1.0
                log.info(f"Telegram 429: retry in {retry_after}s (attempt {attempt+1}/3)")
                await asyncio.sleep(retry_after + 0.2)
                await _tg_throttle()
                r = await client.post(url, json=payload, timeout=HTTP_TIMEOUT)

            r.raise_for_status()
            body = r.json()
            if not body.get("ok"):
                log.error(f"Telegram ok=false: {body}")
                return False

            mid = body.get("result", {}).get("message_id")
            if mid:
                delete_at = datetime.now(timezone.utc) + timedelta(hours=DELETE_AFTER_HOURS)
                remember_for_deletion(str(chat), int(mid), delete_at)

            log.info(f"Message sent: {title[:80]}… (preview_by_TG=True)")
            return True

    except Exception as e:
        log.error(f"Telegram send error for {link}: {e}")
        return False

def remember_for_deletion(chat_id: str, message_id: int, delete_at: datetime) -> None:
    state, gen = load_state()
    _ensure_state_shapes(state)
    state["delete_queue"].append({
        "chat_id": str(chat_id),
        "message_id": int(message_id),
        "delete_at": delete_at.isoformat(),
    })
    save_state_atomic(state, gen)

async def sweep_delete_queue(now: datetime | None = None) -> int:
    if not TG_TOKEN or not TG_CHAT_ID:
        return 0
    state, gen = load_state()
    _ensure_state_shapes(state)
    dq = state.get("delete_queue", [])
    if not dq:
        return 0

    now = now or datetime.now(timezone.utc)
    keep: List[Dict[str, Any]] = []
    deleted = 0

    async with make_async_client() as client:
        for item in dq:
            try:
                delete_at = datetime.fromisoformat(item["delete_at"])
                if delete_at.tzinfo is None:
                    delete_at = delete_at.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            if delete_at > now:
                keep.append(item)
                continue

            try:
                url = f"https://api.telegram.org/bot{TG_TOKEN}/deleteMessage"
                payload = {"chat_id": item["chat_id"], "message_id": item["message_id"]}
                r = await client.post(url, json=payload, timeout=HTTP_TIMEOUT)
                if r.status_code == 200:
                    deleted += 1
                else:
                    dbg(f"deleteMessage status={r.status_code} body={r.text[:200]}")
            except Exception as e:
                dbg(f"deleteMessage error: {e}")

    state["delete_queue"] = keep
    save_state_atomic(state, gen)
    return deleted

# ---------- FETCH & SCRAPE ----------
async def fetch_feed(client: httpx.AsyncClient, url: str) -> List[Tuple[str, str]]:
    posts: List[Tuple[str, str]] = []
    try:
        async with _sem_for(url):
            await _jitter()
            r = await client.get(url, headers=build_headers(url))
        if r.status_code == 200 and r.content[:64].lstrip().lower().startswith(b"<html"):
            log.info(f"RSS looks like HTML (not XML): {url} [Content-Type={r.headers.get('Content-Type')}]")
        if r.status_code == 200:
            feed = feedparser.parse(r.content)
            if getattr(feed, "bozo", 0):
                log.info(f"feedparser bozo for {url}: {getattr(feed, 'bozo_exception', '')}")
            for entry in feed.entries:
                t = entry.get("title")
                l = entry.get("link")
                if t and l:
                    posts.append((t, l))
            log.info(f"Fetched {len(posts)} posts from RSS: {url}")
            host = urlparse(url).netloc.lower().replace("www.", "")
            if not posts and "marocmama.com" in host:
                dbg("marocmama RSS empty — attempting homepage fallback")
                posts = await marocmama_fallback(client, domain_root(url))
            return posts[:MAX_PER_DOMAIN]
        log.info(f"HTTP {r.status_code} for RSS: {url}")
    except Exception as e:
        log.info(f"Error fetching RSS {url}: {e}")
        try:
            host = urlparse(url).netloc.lower()
            if "marocmama.com" in host:
                return (await marocmama_fallback(client, domain_root(url)))[:MAX_PER_DOMAIN]
        except Exception:
            pass
    return []

def _selectors_for(host: str) -> List[str]:
    precise: Dict[str, List[str]] = {
        "travel-dealz.com": ['article.article-item h2 a', 'article.article h2 a'],
        "secretflying.com": ['article.post-item .post-title a', 'article h2 a'],
        "wakacyjnipiraci.pl": ['article.post-list__item a.post-list__link'],
        "theflightdeal.com": ['article h2 a', '.entry-title a'],
        "marocmama.com": ['article h2 a', 'h2 a', 'h3 a'],
    }
    fallback = ['article h2 a', 'article h3 a', 'h2 a', 'h3 a', 'main a']
    sels = precise.get(host, []) + fallback
    out, seen = [], set()
    for s in sels:
        if s not in seen:
            out.append(s); seen.add(s)
    return out

async def _scrape_once(client: httpx.AsyncClient, url: str, variant_note: str) -> List[Tuple[str, str]]:
    posts: List[Tuple[str, str]] = []
    async with _sem_for(url):
        await _jitter()
        r = await client.get(url, headers=build_headers(url))
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    host = urlparse(url).netloc.lower().replace("www.", "")
    for sel in _selectors_for(host):
        for link_tag in soup.select(sel):
            href = (link_tag.get("href") or "").strip()
            if not href.startswith("http"):
                continue
            title = (link_tag.get_text(strip=True) or "").strip()
            if not title:
                parent = link_tag.find_parent(["h2", "h3", "article"])
                if parent:
                    title = parent.get_text(strip=True) or "Brak tytułu"
            posts.append((title, href))
        if posts:
            dbg(f"{host} matched selector '{sel}' ({variant_note}), count={len(posts)}")
            break
    return posts

async def _try_variants_scrape(client: httpx.AsyncClient, url: str) -> List[Tuple[str, str]]:
    def _mobile_variant(u: str) -> str:
        p = urlparse(u)
        netloc = p.netloc
        if not netloc.startswith("m."):
            netloc = "m." + netloc.replace("www.", "")
        return urlunparse((p.scheme, netloc, p.path or "/", p.params, p.query, p.fragment))

    def _amp_variant(u: str) -> str:
        p = urlparse(u)
        path = p.path
        if not path.endswith("/amp/"):
            path = (path + "/" if not path.endswith("/") else path) + "amp/"
        return urlunparse((p.scheme, p.netloc, path, p.params, p.query, p.fragment))

    host = urlparse(url).netloc.lower().replace("www.", "")
    variants = [(url, "desktop")]
    if host in {"travel-dealz.com", "secretflying.com", "theflightdeal.com"}:
        variants += [(_mobile_variant(url), "mobile"), (_amp_variant(url), "amp")]

    for vurl, note in variants:
        try:
            posts = await _scrape_once(client, vurl, note)
            if posts:
                log.info(f"Scraped {len(posts)} posts from Web ({note}): {url}")
                return posts[:MAX_PER_DOMAIN]
        except Exception as e:
            dbg(f"scrape variant {note} failed for {url}: {e}")
            continue
    log.info(f"Scraped 0 posts from Web: {url}")
    return []

async def marocmama_fallback(client: httpx.AsyncClient, root_url: str) -> List[Tuple[str, str]]:
    try:
        async with _sem_for(root_url):
            await _jitter()
            r = await client.get(root_url, headers=build_headers(root_url))
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        posts: List[Tuple[str, str]] = []
        for sel in _selectors_for("marocmama.com"):
            for a in soup.select(sel):
                href = (a.get("href") or "").strip()
                if not href.startswith("http"):
                    continue
                title = (a.get_text(strip=True) or "").strip() or "Nowy wpis"
                posts.append((title, href))
            if posts:
                break
        if posts:
            log.info(f"marocmama fallback returned {len(posts)} items")
        return posts[:MAX_PER_DOMAIN]
    except Exception as e:
        log.info(f"marocmama fallback failed: {e}")
        return []

async def scrape_webpage(client: httpx.AsyncClient, url: str) -> List[Tuple[str, str]]:
    return await _try_variants_scrape(client, url)

# ---------- MAIN FLOW ----------
def _prioritize_sources(urls: List[str]) -> List[str]:
    priority_hosts = ["travel-dealz.com", "secretflying.com", "theflightdeal.com", "loter"]
    def score(u: str) -> int:
        host = urlparse(u).netloc.lower()
        for i, h in enumerate(priority_hosts):
            if h in host:
                return i
        return len(priority_hosts)
    return sorted(urls, key=score)

async def process_feeds_async() -> str:
    log.info("Starting hybrid processing.")
    if not TG_TOKEN or not TG_CHAT_ID:
        return "Missing TG_TOKEN/TG_CHAT_ID."

    rss = get_rss_sources()
    web = get_web_sources()
    sources = _prioritize_sources(rss + web)
    if not sources:
        return "No sources found."

    state, generation = load_state()
    sent_links_dict: Dict[str, str] = state.get("sent_links", {})

    all_posts: List[Tuple[str, str]] = []
    async with make_async_client() as client:
        tasks: List[asyncio.Future] = []
        for url in sources:
            url_lc = url.lower()
            is_rss = (
                url_lc.endswith("/feed/") or
                "rss" in url_lc or
                "feed" in url_lc or
                url_lc.endswith(".xml")
            )
            if is_rss:
                tasks.append(fetch_feed(client, url))
            else:
                tasks.append(scrape_webpage(client, url))

        results = await asyncio.gather(*tasks, return_exceptions=True)

    for res in results:
        if isinstance(res, Exception):
            log.info(f"Task error: {res}")
            continue
        if res:
            all_posts.extend(res)

    # kanonizacja + dedup + limit per domenę
    per_host_count: Dict[str, int] = {}
    unique: List[Tuple[str, str]] = []
    seen = set()
    for title, link in all_posts:
        canon = canonicalize_url(link)
        if not canon:
            continue
        if not DISABLE_DEDUP and canon in sent_links_dict:
            continue
        host = urlparse(canon).netloc.lower().replace("www.", "")
        if per_host_count.get(host, 0) >= MAX_PER_DOMAIN:
            continue
        if canon in seen:
            continue
        seen.add(canon)
        per_host_count[host] = per_host_count.get(host, 0) + 1
        unique.append((title or "Nowy wpis", canon))

    if MAX_POSTS_PER_RUN > 0:
        unique = unique[:MAX_POSTS_PER_RUN]

    sent = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for title, canon_link in unique:
        if await send_telegram_message_async(title, canon_link, TG_CHAT_ID):
            sent_links_dict[canon_link] = now_iso
            sent += 1

    state["sent_links"] = sent_links_dict
    save_state_atomic(state, generation)

    return f"Processed sources={len(sources)}, posts_in={len(all_posts)}, sent={sent}, kept_in_state={len(sent_links_dict)}"

# ---------- ROUTES ----------
@app.get("/")
def root():
    return "ok", 200

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/run")
@app.post("/run")
def run_now():
    result = asyncio.run(process_feeds_async())
    return jsonify({"status": "done", "result": result})
@app.get("/tasks/rss")
@app.post("/tasks/rss")
def tasks_rss():
    # dokładnie ta sama ścieżka wykonania co /run
    result = asyncio.run(process_feeds_async())
    return jsonify({"status": "done", "result": result})
@app.post("/telegram/webhook")
def telegram_webhook():
    if not ENABLE_WEBHOOK:
        return jsonify({"ok": False, "error": "webhook disabled"}), 403
    if TELEGRAM_SECRET and request.args.get("secret") != TELEGRAM_SECRET:
        return jsonify({"ok": False, "error": "forbidden"}), 403
    result = asyncio.run(process_feeds_async())
    return jsonify({"ok": True, "result": result})
