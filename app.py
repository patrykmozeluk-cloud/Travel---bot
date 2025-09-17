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
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL")
TELEGRAM_SECRET = env("TELEGRAM_SECRET")
ENABLE_WEBHOOK = env("ENABLE_WEBHOOK", "1") in {"1","true","True","yes","YES"}
DISABLE_DEDUP = env("DISABLE_DEDUP", "0") in {"1","true","True","yes","YES"}
DEBUG_FEEDS = env("DEBUG_FEEDS", "0") in {"1","true","True","yes","YES"}
MAX_POSTS_PER_RUN = int(env("MAX_POSTS_PER_RUN", "0"))  # 0 = bez limitu

# „uliczne” domyślne
MAX_PER_DOMAIN = int(env("MAX_PER_DOMAIN", "5"))
PER_HOST_CONCURRENCY = int(env("PER_HOST_CONCURRENCY", "2"))
JITTER_MIN_MS = int(env("JITTER_MIN_MS", "120"))
JITTER_MAX_MS = int(env("JITTER_MAX_MS", "400"))

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
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
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

# --- „street” fingerprint: sticky UA/AL per host ---
STICKY_IDENTITY: Dict[str, Dict[str, str]] = {
    "travel-dealz.com": {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
        "al": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "referer": "https://www.google.com/"
    },
    "secretflying.com": {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
        "al": "en-US,en;q=0.9",
        "referer": "https://secretflying.com/"
    },
    "theflightdeal.com": {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
        "al": "en-US,en;q=0.9",
        "referer": "https://www.google.com/"
    },
    # PIRACI (hotfix)
    "wakacyjnipiraci.pl": {
        "ua": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Mobile Safari/537.36",
        "al": "pl-PL,pl;q=0.9,en-US;q=0.8",
        "referer": "https://wakacyjnipiraci.pl/",
        "rss_no_brotli": "1",
        "rss_accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7"
    },
    # NOWE HOSTY (ten sam hotfix tylko dla ich feedów)
    "travelfree.info": {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
        "al": "en-US,en;q=0.9",
        "referer": "https://travelfree.info/",
        "rss_no_brotli": "1",
        "rss_accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7"
    },
    "twomonkeystravelgroup.com": {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
        "al": "en-US,en;q=0.9",
        "referer": "https://twomonkeystravelgroup.com/",
        "rss_no_brotli": "1",
        "rss_accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7"
    },
    "thebarefootnomad.com": {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
        "al": "en-US,en;q=0.9",
        "referer": "https://www.thebarefootnomad.com/",
        "rss_no_brotli": "1",
        "rss_accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7"
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

    # rozpoznaj feed po ścieżce/zapytaniu
    pathq = (p.path or "") + ("?" + p.query if p.query else "")
    is_rss = any(tok in pathq.lower() for tok in ("/feed", "rss", ".xml", "?feed"))

    ident = STICKY_IDENTITY.get(host)
    if ident:
        h["User-Agent"] = ident["ua"]
        h["Accept-Language"] = ident["al"]
        h["Referer"] = ident["referer"]
        # per-host fix tylko dla feedów na hostach z rss_no_brotli
        if is_rss and ident.get("rss_no_brotli") == "1":
            h["Accept-Encoding"] = "gzip, deflate"  # bez 'br'
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
        cookies=httpx.Cookies()
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

# --- helpers: host semaphores + jitter ---
_host_semaphores: Dict[str, asyncio.Semaphore] = {}

def _sem_for(url: str) -> asyncio.Semaphore:
    host = urlparse(url).netloc.lower()
    if host not in _host_semaphores:
        _host_semaphores[host] = asyncio.Semaphore(PER_HOST_CONCURRENCY)
    return _host_semaphores[host]

async def _jitter():
    await asyncio.sleep(random.uniform(JITTER_MIN_MS/1000.0, JITTER_MAX_MS/1000.0))

# --- IMG helper ---
async def find_og_image(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        async with _sem_for(url):
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
    try:
        async with _sem_for(url):
            await _jitter()
            r = await client.get(url, headers=build_headers(url))
        # sniffer: HTML zamiast XML (diagnostyka bez zmiany logiki)
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
            return posts[:MAX_PER_DOMAIN]
        log.info(f"HTTP {r.status_code} for RSS: {url}")
    except Exception as e:
        log.info(f"Error fetching RSS {url}: {e}")
    return []

def _selectors_for(host: str) -> List[str]:
    # precyzyjne
    precise: Dict[str, List[str]] = {
        "travel-dealz.com": ['article.article-item h2 a', 'article.article h2 a'],
        "secretflying.com": ['article.post-item .post-title a', 'article h2 a'],
        "wakacyjnipiraci.pl": ['article.post-list__item a.post-list__link'],
        "theflightdeal.com": ['article h2 a', '.entry-title a'],
    }
    # fallbacki ogólne
    fallback = ['article h2 a', 'article h3 a', 'h2 a', 'h3 a', 'main a']
    sels = precise.get(host, []) + fallback
    # unikatowe, w kolejności
    seen, out = set(), []
    for s in sels:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out

async def _scrape_once(client: httpx.AsyncClient, url: str, variant_note: str) -> List[Tuple[str, str]]:
    posts: List[Tuple[str, str]] = []
    async with _sem_for(url):
        await _jitter()
        r = await client.get(url, headers=build_headers(url))
    r.raise_for_status()
    soup = BeautifulSoup(r.text, 'html.parser')

    host = urlparse(url).netloc.lower().replace("www.", "")
    for sel in _selectors_for(host):
        for link_tag in soup.select(sel):
            href = link_tag.get('href')
            if not href or not href.strip().startswith("http"):
                continue
            title = (link_tag.get_text(strip=True) or "").strip()
            if not title:
                parent = link_tag.find_parent(['h2', 'h3', 'article'])
                if parent:
                    title = parent.get_text(strip=True) or "Brak tytułu"
            posts.append((title, href))
        if posts:
            dbg(f"{host} matched selector '{sel}' ({variant_note}), count={len(posts)}")
            break
    return posts

def _mobile_variant(url: str) -> str:
    p = urlparse(url)
    netloc = p.netloc
    if not netloc.startswith("m."):
        netloc = "m." + netloc.replace("www.", "")
    return urlunparse((p.scheme, netloc, p.path or "/", p.params, p.query, p.fragment))

def _amp_variant(url: str) -> str:
    p = urlparse(url)
    path = p.path
    if not path.endswith("/amp/"):
        if path.endswith("/"):
            path = path + "amp/"
        else:
            path = path + "/amp/"
    return urlunparse((p.scheme, p.netloc, path, p.params, p.query, p.fragment))

async def scrape_webpage(client: httpx.AsyncClient, url: str) -> List[Tuple[str, str]]:
    host = urlparse(url).netloc.lower().replace("www.", "")
    variants = [(url, "desktop")]
    # dopiero dla „trudnych” hostów próbujemy mobile/amp
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

# --- MAIN FLOW ---
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
        tasks = []
        for url in sources:
            is_rss = any(tok in url for tok in ["rss", "feed", ".xml"])
            if is_rss and url in web:
                pass
            if is_rss:
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
            sent_links_dict[canonical] = "processing"

    # per-domain cap również na kandydatów
    per_host_count: Dict[str, int] = {}
    unique_candidates: List[Tuple[str, str]] = []
    for t, l in candidates:
        key = canonicalize_url(l)
        host = urlparse(l).netloc.lower().replace("www.", "")
        if per_host_count.get(host, 0) >= MAX_PER_DOMAIN:
            continue
        if key in {canonicalize_url(x[1]) for x in unique_candidates}:
            continue
        unique_candidates.append((t, l))
        per_host_count[host] = per_host_count.get(host, 0) + 1

    log.info(f"Summary: seen={len(all_posts)}, new_candidates={len(unique_candidates)}")

    sent_count = 0
    now_utc_iso = datetime.now(timezone.utc).isoformat()
    if unique_candidates:
        results = await asyncio.gather(*[send_telegram_message_async(t, l) for t, l in unique_candidates])
        for (t, l), ok in zip(unique_candidates, results):
            if ok:
                sent_links_dict[canonicalize_url(l)] = now_utc_iso
                sent_count += 1

    # cleanup: 30 dni
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
    
