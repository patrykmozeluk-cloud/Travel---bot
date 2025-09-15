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
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL")  # np. https://twoja-usluga-abc-uc.a.run.app
TELEGRAM_SECRET = env("TELEGRAM_SECRET")
ENABLE_WEBHOOK = env("ENABLE_WEBHOOK", "1") in {"1", "true", "True", "yes", "YES"}
DISABLE_DEDUP = env("DISABLE_DEDUP", "0") in {"1", "true", "True", "yes", "YES"}
DEBUG_FEEDS = env("DEBUG_FEEDS", "0") in {"1", "true", "True", "yes", "YES"}
MAX_POSTS_PER_RUN = int(env("MAX_POSTS_PER_RUN", "0"))  # 0 = bez limitu

# Proxy per-domain
PROXY_ALL = env("HTTP_PROXY") or env("HTTPS_PROXY")  # np. https://user:pass@host:port
PROXY_DOMAINS = {
    d.strip().lower() for d in (env("PROXY_DOMAINS") or "").split(",") if d and d.strip()
}

def needs_proxy(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return any(host.endswith(d) for d in PROXY_DOMAINS)
    except Exception:
        return False

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
        # migracja starego formatu (lista -> dict)
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
    "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
]
BASE_HEADERS = {
    "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, text/html;q=0.7,*/*;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
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

def domain_root(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"

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

def make_async_client_with_proxy() -> httpx.AsyncClient:
    limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
    proxies = {"http://": PROXY_ALL, "https://": PROXY_ALL}
    return httpx.AsyncClient(
        headers=BASE_HEADERS,
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
        http2=True,
        limits=limits,
        cookies=httpx.Cookies(),
        proxies=proxies,
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

# --- TELEGRAM helpers ---
def telegram_api_url(method: str) -> str:
    token = env("TG_TOKEN")
    if not token:
        return ""
    return f"https://api.telegram.org/bot{token}/{method}"

async def telegram_call(method: str, payload: Dict[str, Any] | None = None) -> tuple[bool, Dict[str, Any] | str]:
    url = telegram_api_url(method)
    if not url:
        log.error("TG_TOKEN is not set.")
        return False, "missing_token"
    async with make_async_client() as client:
        try:
            if payload is None:
                r = await client.get(url, timeout=HTTP_TIMEOUT)
            else:
                r = await client.post(url, json=payload, timeout=HTTP_TIMEOUT)
            status = r.status_code
            text = r.text
            ok = False
            body: Dict[str, Any] | str
            try:
                body = r.json()
                ok = bool(body.get("ok"))
            except Exception:
                body = text
            log.info(f"TG {method} -> {status} | ok={ok} | body_sample={str(body)[:300]}")
            r.raise_for_status()
            return True, body
        except Exception as e:
            log.error(f"TG {method} error: {e}")
            return False, str(e)

# --- IMG helper ---
async def find_og_image(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        r = await client.get(url, headers=build_headers(url))
        r.raise_for_status()
        s = BeautifulSoup(r.text, "html.parser")
        for sel in ('meta[property="og:image"]','meta[name="twitter:image"]'):
            tag = s.select_one(sel)
            if tag and tag.get("content"):
                return tag["content"].strip()
    except Exception:
        pass
    return None

# --- SEND TG (foto jeśli jest, inaczej tekst; bezpieczny tytuł) ---
async def send_telegram_message_async(title: str, link: str, chat_id: str | None = None) -> bool:
    chat = chat_id or env("TG_CHAT_ID")
    if not chat:
        log.error("TG_CHAT_ID is not set.")
        return False
    import html
    safe_title = html.escape(title, quote=False)
    caption = f"<b>{safe_title}</b>\n\n{link}"

    # własny klient do pobrania ewentualnego obrazka (z rotacją UA/cookies)
    async with make_async_client() as client:
        try:
            img = await find_og_image(client, link)
            if img:
                ok, _ = await telegram_call("sendPhoto", {
                    "chat_id": chat, "photo": img, "caption": caption, "parse_mode": "HTML"
                })
            else:
                ok, _ = await telegram_call("sendMessage", {
                    "chat_id": chat, "text": caption, "parse_mode": "HTML", "disable_web_page_preview": True
                })
            if ok:
                log.info(f"Message sent: {title[:80]}… (with_photo={bool(img)})")
            return ok
        except Exception as e:
            log.error(f"Telegram error for link {link}: {e}")
            return False

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
                feed = feedparser.parse(r.content)
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

    # Fallback: alternatywny link RSS na homepage
    try:
        html_resp = await client.get(base, headers=headers)
        if html_resp.status_code == 200:
            soup = BeautifulSoup(html_resp.text, "html.parser")
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
            for article in soup.select('article.article-item, article.article'):
                link_tag = article.select_one('h2.article-title a, h2 a')
                if link_tag and (href := link_tag.get('href')):
                    posts.append((link_tag.get_text(strip=True), href))

        elif "secretflying.com" in url:
            for article in soup.select('article.post-item, article'):
                link_tag = article.select_one('.post-title a, h2 a')
                if link_tag and (href := link_tag.get('href')):
                    title = link_tag.get_text(strip=True)
                    posts.append((title, href))

        elif "flynous.com" in url:
            for a in soup.select("h2.entry-title a, h3.entry-title a, article h2 a, article .entry-title a"):
                href = a.get("href")
                title = a.get_text(strip=True)
                if href and title:
                    posts.append((title, href))

        elif "wakacyjnipiraci.pl" in url:
            for article in soup.select('article.post-list__item, article'):
                link_tag = article.select_one('a.post-list__link, h2 a')
                if link_tag and (href := link_tag.get('href')):
                    title_tag = article.select_one('h2.post-list__title, h2')
                    if title_tag:
                        posts.append((title_tag.get_text(strip=True), href))

        elif "loter.pl" in url:
            for a in soup.select("article a, h2 a, .post-title a"):
                href = a.get("href")
                title = a.get_text(strip=True)
                if href and title and href.startswith("http"):
                    posts.append((title, href))

        log.info(f"Scraped {len(posts)} posts from {url}")
        return posts

    except httpx.HTTPStatusError as he:
        code = he.response.status_code if he.response else None
        log.error(f"Scrape HTTP {code} {url}: {he}")
        return []
    except Exception as e:
        log.error(f"Error scraping webpage {url}: {e}")
        return []

# --- MAIN FLOW (zapis dopiero po UDANEJ wysyłce) ---
async def process_feeds_async() -> str:
    log.info("Starting hybrid processing (RSS + Scraping).")
    if not env("TG_TOKEN") or not env("TG_CHAT_ID"):
        return "Missing TG_TOKEN/TG_CHAT_ID."

    rss_sources = get_rss_sources()
    web_sources = get_web_sources()
    if not rss_sources and not web_sources:
        return "No RSS or Web sources found."

    # rozmyj fingerprint bota
    random.shuffle(rss_sources)
    random.shuffle(web_sources)

    state, generation = load_state()
    sent_links_dict: Dict[str, str] = state.get("sent_links", {})

    async with make_async_client() as client_plain:
        client_proxy = make_async_client_with_proxy() if PROXY_ALL else None

        async def pick_client(u: str) -> httpx.AsyncClient:
            if client_proxy and needs_proxy(u):
                return client_proxy
            return client_plain

        tasks = []
        delay = 0.0

        for url in rss_sources:
            delay += random.uniform(0.0, 0.3)  # jitter 0–300 ms
            async def job(u=url, d=delay):
                await asyncio.sleep(d)
                c = await pick_client(u)
                return await fetch_feed(c, u)
            tasks.append(job())

        for url in web_sources:
            delay += random.uniform(0.0, 0.3)
            async def job(u=url, d=delay):
                await asyncio.sleep(d)
                c = await pick_client(u)
                return await scrape_webpage(c, u)
            tasks.append(job())

        results = await asyncio.gather(*tasks, return_exceptions=False)

        if client_proxy:
            await client_proxy.aclose()

    all_posts = [post for post_list in results for post in post_list]
    if MAX_POSTS_PER_RUN > 0 and len(all_posts) > MAX_POSTS_PER_RUN:
        all_posts = all_posts[:MAX_POSTS_PER_RUN]
    dbg(f"ALL_POSTS total={len(all_posts)}")

    candidates: List[Tuple[str, str]] = []
    now_utc = datetime.now(timezone.utc)

    dup_count = 0
    dup_samples: List[str] = []

    for idx, (title, link) in enumerate(all_posts):
        canonical = canonicalize_url(link)
        if DISABLE_DEDUP or canonical not in sent_links_dict:
            candidates.append((title, link))
            dbg(f"NEW [{idx}] {canonical}  title='{title[:90]}'")
        else:
            dup_count += 1
            if len(dup_samples) < 5:
                dup_samples.append(canonical)
            dbg(f"DUP  [{idx}] {canonical}")

    log.info(
        f"Summary(before send): seen={len(all_posts)}, new_candidates={len(candidates)}, duplicates={dup_count}"
        + (f", dup_samples={dup_samples}" if DEBUG_FEEDS and dup_samples else "")
    )

    sent_count = 0
    if candidates:
        log.info(f"Found {len(candidates)} new posts. Sending.")
        # wysyłamy z obrazkiem/escape title (wewnątrz ma własny klient dla og:image)
        results = await asyncio.gather(*[
            send_telegram_message_async(t, l) for t, l in candidates
        ])

        # zapisz TYLKO te, które poszły
        for (t, l), ok in zip(candidates, results):
            if ok:
                sent_links_dict[canonicalize_url(l)] = now_utc.isoformat()
                sent_count += 1

        if DEBUG_FEEDS and sent_count:
            sample_titles = [t[:90] for (t, _), ok in zip(candidates, results) if ok][:5]
            log.info(f"Sent sample titles: {sample_titles}")
    else:
        log.info("Found 0 new posts. Nothing to send.")

    # cleanup >30 dni
    thirty_days_ago = now_utc - timedelta(days=30)
    cleaned_sent_links = {}
    for link, ts in sent_links_dict.items():
        try:
            when = datetime.fromisoformat(ts)
        except Exception:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if when > thirty_days_ago:
            cleaned_sent_links[link] = ts

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

def _is_start_command(update: Dict[str, Any]) -> bool:
    msg = update.get("message", {})
    text = (msg.get("text") or "").strip()
    if not text:
        return False
    if text.lower().startswith("/start"):
        return True
    for ent in msg.get("entities", []):
        if ent.get("type") == "bot_command" and (text[ent["offset"]: ent["offset"]+ent["length"]]).lower().startswith("/start"):
            return True
    return False

@app.post("/tg/webhook")
def telegram_webhook():
    if TELEGRAM_SECRET:
        secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret_header != TELEGRAM_SECRET:
            log.warning("Webhook secret mismatch.")
            return "Forbidden", 403

    update = request.get_json(silent=True) or {}
    if _is_start_command(update):
        chat = update.get("message", {}).get("chat", {})
        chat_id = str(chat.get("id")) if chat else env("TG_CHAT_ID")
        async def _run():
            await send_telegram_message_async(
                title="Cześć! 👋",
                link="Bot działa — poluję na okazje i sam po sobie sprzątam.",
                chat_id=chat_id,
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

# --- DIAGNOSTYKA / POMOC ---
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
            "TG_CHAT_ID": env("TG_CHAT_ID"),
            "BUCKET_NAME": env("BUCKET_NAME"),
            "SENT_LINKS_FILE": env("SENT_LINKS_FILE"),
            "HTTP_TIMEOUT": env("HTTP_TIMEOUT"),
            "DEBUG_FEEDS": env("DEBUG_FEEDS"),
            "DISABLE_DEDUP": env("DISABLE_DEDUP"),
            "PUBLIC_BASE_URL": env("PUBLIC_BASE_URL"),
            "ENABLE_WEBHOOK": env("ENABLE_WEBHOOK"),
        }
    }), 200

@app.get("/debug/telegram/getMe")
def debug_tg_getme():
    ok, body = asyncio.run(telegram_call("getMe"))
    return (jsonify(body), 200) if ok else (str(body), 500)

@app.post("/debug/telegram/ping")
def debug_tg_ping():
    try:
        title = request.json.get("title", "Self-test from Cloud Run ✅")
        link = request.json.get("link", "Ping.")
    except Exception:
        title, link = "Self-test from Cloud Run ✅", "Ping."
    ok = asyncio.run(send_telegram_message_async(title, link))
    return (jsonify({"ok": True}), 200) if ok else ("TG send failed", 500)

@app.get("/debug/sources")
def debug_sources():
    return jsonify({
        "rss": get_rss_sources(),
        "web": get_web_sources(),
    }), 200

@app.post("/tg/set_webhook")
def set_webhook():
    if not ENABLE_WEBHOOK:
        return "webhook disabled", 200
    base = PUBLIC_BASE_URL
    if not base:
        return "Set PUBLIC_BASE_URL first.", 400
    url = f"{base.rstrip('/')}/tg/webhook"
    params = {"url": url}
    if TELEGRAM_SECRET:
        params["secret_token"] = TELEGRAM_SECRET
    ok, body = asyncio.run(telegram_call("setWebhook", params))
    return (jsonify(body), 200) if ok else (str(body), 500)

if __name__ == "__main__":
    port = int(env("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=True)
