# app.py
import os, logging, asyncio, time, random, html as html_mod
from typing import Dict, Any, Tuple, List
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, unquote, urljoin

import httpx, feedparser, orjson
from flask import Flask, request, jsonify
from google.cloud import storage
from bs4 import BeautifulSoup

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

# ---------- APP / GCS ----------
app = Flask(__name__)
storage_client = storage.Client()

# ---------- ENV ----------
def env(n: str, d: str | None = None) -> str | None:
    v = os.environ.get(n, d)
    return v if v is None else v.strip()

TG_TOKEN             = env("TG_TOKEN")
TG_CHAT_ID           = env("TG_CHAT_ID")
BUCKET_NAME          = env("BUCKET_NAME", "travel-bot-storage-patrykmozeluk-cloud")
SENT_LINKS_FILE      = env("SENT_LINKS_FILE", "sent_links.json")
HTTP_TIMEOUT         = float(env("HTTP_TIMEOUT", "20.0"))
ENABLE_WEBHOOK       = env("ENABLE_WEBHOOK", "1") in {"1","true","True","yes","YES"}
TELEGRAM_SECRET      = env("TELEGRAM_SECRET")
DEBUG_FEEDS          = env("DEBUG_FEEDS", "0") in {"1","true","True","yes","YES"}

# świeżość / dedup / limity
USE_EVENT_TIME       = env("USE_EVENT_TIME", "1") in {"1","true","True","yes","YES"}
EVENT_WINDOW_HOURS   = int(env("EVENT_WINDOW_HOURS", "48"))
BACKFILL_WARMUP_HOURS= int(env("BACKFILL_WARMUP_HOURS", "6"))
DEDUP_TTL_HOURS      = int(env("DEDUP_TTL_HOURS", "336"))     # 14 dni
DELETE_AFTER_HOURS   = int(env("DELETE_AFTER_HOURS", "48"))   # auto-delete TG
DISABLE_DEDUP        = env("DISABLE_DEDUP", "0") in {"1","true","True","yes","YES"}
MAX_PER_DOMAIN       = int(env("MAX_PER_DOMAIN", "8"))
MAX_POSTS_PER_RUN    = int(env("MAX_POSTS_PER_RUN", "0"))     # 0 = brak globalnego limitu
PER_HOST_CONCURRENCY = int(env("PER_HOST_CONCURRENCY", "2"))
JITTER_MIN_MS        = int(env("JITTER_MIN_MS", "120"))
JITTER_MAX_MS        = int(env("JITTER_MAX_MS", "400"))

# ---------- UTILS ----------
def dbg(msg: str):
    if DEBUG_FEEDS:
        log.info(f"DEBUG {msg}")

DROP_PARAMS = {
    "utm_source","utm_medium","utm_campaign","utm_term","utm_content",
    "fbclid","gclid","igshid","mc_cid","mc_eid","ref","ref_src","src"
}

def _strip_default_port(netloc: str, scheme: str) -> str:
    if scheme == "http" and netloc.endswith(":80"): return netloc[:-3]
    if scheme == "https" and netloc.endswith(":443"): return netloc[:-4]
    return netloc

def canonicalize_url(url: str) -> str:
    try:
        u = unquote(url.strip()); p = urlparse(u)
        scheme = (p.scheme or "https").lower()
        netloc = p.netloc.lower()
        for pref in ("www.","m.","amp."):
            if netloc.startswith(pref): netloc = netloc[len(pref):]
        netloc = _strip_default_port(netloc, scheme)
        path = (p.path or "/").replace("//","/")
        if path != "/" and path.endswith("/"): path = path[:-1]
        q = [(k,v) for k,v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in DROP_PARAMS]
        q.sort()
        return urlunparse((scheme, netloc, path, p.params, urlencode(q, doseq=True), ""))
    except Exception:
        return url.strip()

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

# ---------- STATE (GCS) ----------
_bucket = storage_client.bucket(BUCKET_NAME)
_blob   = _bucket.blob(SENT_LINKS_FILE)

def _default_state() -> Dict[str, Any]:
    return {"sent_links": {}, "delete_queue": []}

def _ensure_state_shapes(state: Dict[str, Any]) -> None:
    if "sent_links" not in state or not isinstance(state["sent_links"], dict): state["sent_links"] = {}
    if "delete_queue" not in state or not isinstance(state["delete_queue"], list): state["delete_queue"] = []

def load_state() -> Tuple[Dict[str, Any], int | None]:
    try:
        if not _blob.exists():
            return _default_state(), None
        _blob.reload()
        data, gen = _blob.download_as_bytes(), _blob.generation
        state = orjson.loads(data)
        if isinstance(state.get("sent_links"), list):
            state["sent_links"] = {link: _now_utc().isoformat() for link in state["sent_links"]}
        _ensure_state_shapes(state)
        return state, gen
    except Exception as e:
        log.warning(f"load_state fallback: {e}")
        return _default_state(), None

def save_state_atomic(state: Dict[str, Any], expected_generation: int | None, retries: int = 8):
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
                time.sleep(0.3); _, expected_generation = load_state(); continue
            raise
    raise RuntimeError("Atomic save failed.")

def prune_sent_links(state: Dict[str, Any], now: datetime) -> int:
    ttl = timedelta(hours=DEDUP_TTL_HOURS)
    sent = state.get("sent_links", {})
    if not isinstance(sent, dict) or not sent: return 0
    keep, removed = {}, 0
    for link, iso in sent.items():
        try:
            ts = datetime.fromisoformat(iso)
            if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if now - ts <= ttl: keep[link] = iso
        else: removed += 1
    state["sent_links"] = keep
    return removed

# ---------- HTTP CLIENT / HEADERS ----------
BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

# HOTFIXY (przywrócone, w tym Tanie-Loty)
STICKY_IDENTITY: Dict[str, Dict[str, str]] = {
    "tanie-loty.com.pl": {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
        "al": "pl-PL,pl;q=0.9,en-US,en;q=0.8",
        "referer": "https://www.tanie-loty.com.pl/",
        "rss_no_brotli": "1",
        "rss_accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7",
    },
    "wakacyjnipiraci.pl": {
        "ua": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Mobile Safari/537.36",
        "al": "pl-PL,pl;q=0.9,en-US,en;q=0.8",
        "referer": "https://wakacyjnipiraci.pl/",
        "rss_no_brotli": "1",
        "rss_accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7",
    },
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
        h["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"
        )
        h["Accept-Language"] = "en-US,en;q=0.9"
        h["Referer"] = f"{p.scheme}://{p.netloc}/"

    # Wymuszenie „RSS-owego” Accept + wyłączenie brotli dla feedów bez profilu
    if is_rss and not ident:
        h["Accept"] = "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7"
        h["Accept-Encoding"] = "gzip, deflate"

    return h


def make_async_client() -> httpx.AsyncClient:
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=40)
    return httpx.AsyncClient(headers=BASE_HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True, http2=True, limits=limits, cookies=httpx.Cookies())

# ---------- SOURCES ----------
def _read_lines(path: str) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
    except FileNotFoundError:
        return []
def get_rss_sources() -> List[str]: return _read_lines("rss_sources.txt")
def get_web_sources() -> List[str]: return _read_lines("web_sources.txt")

# ---------- CONCURRENCY ----------
_host_semaphores: Dict[str, asyncio.Semaphore] = {}
def _sem_for(url: str) -> asyncio.Semaphore:
    host = urlparse(url).netloc.lower()
    if host not in _host_semaphores:
        _host_semaphores[host] = asyncio.Semaphore(PER_HOST_CONCURRENCY)
    return _host_semaphores[host]
async def _jitter(): await asyncio.sleep(random.uniform(JITTER_MIN_MS/1000.0, JITTER_MAX_MS/1000.0))

# ---------- TELEGRAM ----------
TG_RATE_INTERVAL = 1.1
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

def remember_for_deletion(chat_id: str, message_id: int, delete_at: datetime) -> None:
    state, gen = load_state(); _ensure_state_shapes(state)
    state["delete_queue"].append({"chat_id": str(chat_id), "message_id": int(message_id), "delete_at": delete_at.isoformat()})
    save_state_atomic(state, gen)

async def send_telegram_message_async(title: str, link: str, chat_id: str | None = None) -> bool:
    chat = chat_id or TG_CHAT_ID
    if not TG_TOKEN or not chat:
        log.error("Brak TG_TOKEN/TG_CHAT_ID.")
        return False
    payload = {
        "chat_id": str(chat),
        "text": f"<b>{html_mod.escape(title or '', quote=False)}</b>\n\n{link}",
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        async with make_async_client() as client:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            await _tg_throttle()
            r = await client.post(url, json=payload, timeout=HTTP_TIMEOUT)
            for _ in range(3):
                if r.status_code != 429: break
                ra = float(r.headers.get("Retry-After","1"))
                log.info(f"Telegram 429: retry in {ra}s")
                await asyncio.sleep(ra + 0.2)
                await _tg_throttle()
                r = await client.post(url, json=payload, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            body = r.json()
            if not body.get("ok"):
                log.error(f"Telegram ok=false: {body}")
                return False
            mid = body.get("result",{}).get("message_id")
            if mid:
                remember_for_deletion(str(chat), int(mid), _now_utc()+timedelta(hours=DELETE_AFTER_HOURS))
            log.info(f"Message sent: {title[:80]}…")
            return True
    except Exception as e:
        log.error(f"Telegram send error for {link}: {e}")
        return False

async def sweep_delete_queue(now: datetime | None = None) -> int:
    if not TG_TOKEN or not TG_CHAT_ID: return 0
    state, gen = load_state(); _ensure_state_shapes(state); dq = state.get("delete_queue", [])
    if not dq: return 0
    now = now or _now_utc()
    keep: List[Dict[str, Any]] = []; deleted = 0
    async with make_async_client() as client:
        for item in dq:
            try:
                delete_at = datetime.fromisoformat(item["delete_at"])
                if delete_at.tzinfo is None: delete_at = delete_at.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if delete_at > now:
                keep.append(item); continue
            try:
                r = await client.post(
                    f"https://api.telegram.org/bot{TG_TOKEN}/deleteMessage",
                    json={"chat_id": item["chat_id"], "message_id": item["message_id"]},
                    timeout=HTTP_TIMEOUT
                )
                if r.status_code == 200: deleted += 1
                else: dbg(f"deleteMessage status={r.status_code} body={r.text[:200]}")
            except Exception as e:
                dbg(f"deleteMessage error: {e}")
    state["delete_queue"] = keep; save_state_atomic(state, gen); return deleted

# ---------- FETCH & SCRAPE ----------
def _parse_entry_datetime(entry) -> datetime | None:
    from email.utils import parsedate_to_datetime
    for k in ("published_parsed","updated_parsed","created_parsed"):
        ts = entry.get(k)
        if ts:
            try: return datetime(*ts[:6], tzinfo=timezone.utc)
            except Exception: pass
    for k in ("published","updated","created"):
        s = entry.get(k)
        if not s: continue
        try:
            dt = parsedate_to_datetime(str(s))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
        try:
            dt = datetime.fromisoformat(str(s).replace("Z","+00:00"))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return None

async def fetch_feed(client: httpx.AsyncClient, url: str) -> List[Tuple[str, str, datetime | None]]:
    posts: List[Tuple[str, str, datetime | None]] = []
    try:
        async with _sem_for(url):
            await _jitter()
            r = await client.get(url, headers=build_headers(url))
        if r.status_code == 200 and r.content[:64].lstrip().lower().startswith(b"<html"):
            log.info(f"RSS looks like HTML (not XML): {url} [CT={r.headers.get('Content-Type')}]")
        if r.status_code == 200:
            feed = feedparser.parse(r.content)
            if getattr(feed,"bozo",0):
                dbg(f"feedparser bozo for {url}: {getattr(feed,'bozo_exception','')}")
            for e in feed.entries:
                t = e.get("title"); raw = e.get("link") or e.get("id") or e.get("guid")
                if not t or not raw: continue
                try:
                    raw = str(raw)
                    l = raw if raw.startswith("http") else urljoin(f"{urlparse(url).scheme}://{urlparse(url).netloc}/", raw.lstrip("/"))
                except Exception:
                    continue
                posts.append((t, l, _parse_entry_datetime(e)))
            log.info(f"Fetched {len(posts)} posts from RSS: {url}")
            return posts
        log.info(f"HTTP {r.status_code} for RSS: {url}")
    except Exception as e:
        log.info(f"Error fetching RSS {url}: {e}")
    return []

def _selectors_for(host: str) -> List[str]:
    base: Dict[str, List[str]] = {
        "travel-dealz.com": ['article.article-item h2 a','article.article h2 a'],
        "secretflying.com": ['article.post-item .post-title a','article h2 a'],
        "wakacyjnipiraci.pl": ['article.post-list__item a.post-list__link'],
        "theflightdeal.com": ['article h2 a','.entry-title a'],
    }
    return base.get(host, []) + ['article h2 a','article h3 a','h2 a','h3 a','main a']

async def scrape_webpage(client: httpx.AsyncClient, url: str) -> List[Tuple[str, str, datetime | None]]:
    posts: List[Tuple[str, str, datetime | None]] = []
    async with _sem_for(url):
        await _jitter()
        r = await client.get(url, headers=build_headers(url))
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    host = urlparse(url).netloc.lower().replace("www.","")
    for sel in _selectors_for(host):
        for a in soup.select(sel):
            href = (a.get("href") or "").strip()
            if not href.startswith("http"): continue
            title = (a.get_text(strip=True) or "").strip() or "Nowy wpis"
            posts.append((title, href, None))
        if posts:
            dbg(f"{host} matched '{sel}', count={len(posts)}")
            break
    log.info(f"Scraped {len(posts)} posts from Web: {url}")
    return posts

# ---------- MAIN ----------
def _prioritize_sources(urls: List[str]) -> List[str]:
    priority = ["travel-dealz.com","secretflying.com","theflightdeal.com","loter"]
    def score(u: str) -> int:
        host = urlparse(u).netloc.lower()
        for i,h in enumerate(priority):
            if h in host: return i
        return len(priority)
    return sorted(urls, key=score)

async def process_feeds_async() -> str:
    log.info("Run started.")
    if not TG_TOKEN or not TG_CHAT_ID:
        return "Missing TG_TOKEN/TG_CHAT_ID."

    # Sprzątamy kolejkę kasowania na starcie przebiegu.
    swept1 = await sweep_delete_queue()

    rss, web = get_rss_sources(), get_web_sources()
    sources = _prioritize_sources(rss + web)
    if not sources:
        return f"No sources. swept={swept1}"

    state, gen = load_state(); _ensure_state_shapes(state)
    sent_links = state.get("sent_links", {})

    now = _now_utc()
    pruned = prune_sent_links(state, now)
    is_cold = (len(sent_links) == 0)

    items_raw: List[Tuple[str, str, datetime | None]] = []
    async with make_async_client() as client:
        coros = []
        for u in sources:
            is_rss = any(s in u.lower() for s in ("/feed","rss",".xml","?feed"))
            coros.append((fetch_feed if is_rss else scrape_webpage)(client, u))
        results = await asyncio.gather(*coros, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception): dbg(f"task err: {r}"); continue
        if r: items_raw.extend(r)

    # kanonizacja + wstępny dedup (w sesji)
    seen, items = set(), []
    for title, link, dt in items_raw:
        canon = canonicalize_url(link)
        if not canon: continue
        if (not DISABLE_DEDUP) and (canon in sent_links): continue
        if canon in seen: continue
        seen.add(canon); items.append((title or "Nowy wpis", canon, dt))

    # świeżość + sort
    if USE_EVENT_TIME:
        window = timedelta(hours=BACKFILL_WARMUP_HOURS if is_cold and BACKFILL_WARMUP_HOURS>0 else EVENT_WINDOW_HOURS)
        cutoff = now - window
        items = [it for it in items if (it[2] is None or it[2] >= cutoff)]
        items.sort(key=lambda x: (x[2] is None, x[2] or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)

    # limity
    per_host: Dict[str,int] = {}; out: List[Tuple[str,str]] = []
    for t, l, _ in items:
        host = urlparse(l).netloc.lower().replace("www.","")
        if per_host.get(host,0) >= MAX_PER_DOMAIN: continue
        per_host[host] = per_host.get(host,0) + 1
        out.append((t,l))
    if MAX_POSTS_PER_RUN > 0:
        out = out[:MAX_POSTS_PER_RUN]

    # wysyłka
    sent = 0; now_iso = now.isoformat()
    for t, l in out:
        if await send_telegram_message_async(t, l, TG_CHAT_ID):
            sent_links[l] = now_iso; sent += 1

    state["sent_links"] = sent_links
    save_state_atomic(state, gen)

    # drugie sprzątanie na końcu rundy
    swept2 = await sweep_delete_queue()

    return f"sources={len(sources)} items_in={len(items_raw)} candidates={len(items)} sent={sent} pruned={pruned} kept={len(sent_links)} swept={swept1+swept2}"

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
    return jsonify({"status":"done","result":result})

@app.get("/tasks/rss")
@app.post("/tasks/rss")
def tasks_rss():
    result = asyncio.run(process_feeds_async())
    return jsonify({"status":"done","result":result})

@app.post("/telegram/webhook")
def telegram_webhook():
    if not ENABLE_WEBHOOK:
        return jsonify({"ok":False,"error":"webhook disabled"}), 403
    if TELEGRAM_SECRET and request.args.get("secret") != TELEGRAM_SECRET:
        return jsonify({"ok":False,"error":"forbidden"}), 403
    result = asyncio.run(process_feeds_async())
    return jsonify({"ok":True,"result":result})

# Cron do samego sprzątania (opcjonalnie pod Cloud Scheduler)
@app.get("/tasks/sweep")
def tasks_sweep():
    deleted = asyncio.run(sweep_delete_queue())
    return jsonify({"ok":True, "deleted": deleted})
