import os
import logging
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

# --- KONFIG ---
TG_TOKEN = os.environ.get('TG_TOKEN')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID')
BUCKET_NAME = os.environ.get('BUCKET_NAME', 'travel-bot-storage-patrykmozeluk-cloud')
SENT_LINKS_FILE = os.environ.get('SENT_LINKS_FILE', 'sent_links.json')
HTTP_TIMEOUT = float(os.environ.get('HTTP_TIMEOUT', "20.0"))
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage" if TG_TOKEN else None
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
]
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)
app = Flask(__name__)
storage_client = storage.Client()
_bucket = storage_client.bucket(BUCKET_NAME)
_blob = _bucket.blob(SENT_LINKS_FILE)
DROP_PARAMS = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","fbclid","gclid","igshid","mc_cid","mc_eid"}

def canonicalize_url(url: str) -> str:
    try: p = urlparse(url.strip()); scheme = p.scheme.lower() or "https"; netloc = p.netloc.lower(); path = p.path or "/"; q = sorted([(k, v) for k, v in parse_qsl(p.query) if k.lower() not in DROP_PARAMS]); return urlunparse((scheme, netloc, path, p.params, urlencode(q), ""))
    except Exception: return url.strip()

def _default_state() -> Dict[str, Any]: return {"sent_links": {}}
def load_state() -> Tuple[Dict[str, Any], int | None]:
    try:
        if not _blob.exists(): return _default_state(), None
        _blob.reload(); data = _blob.download_as_bytes(); generation = _blob.generation; state_data = orjson.loads(data)
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

def get_sources(filename: str) -> List[str]:
    try:
        with open(filename, 'r', encoding='utf-8') as f: return [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
    except FileNotFoundError: return []

def scrape_webpage(client: httpx.Client, url: str) -> List[Tuple[str, str]]:
    posts = []; headers = {'User-Agent': random.choice(USER_AGENTS)}
    try:
        r = client.get(url, timeout=HTTP_TIMEOUT, follow_redirects=True, headers=headers); r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        if "travel-dealz.com" in url:
            for article in soup.select('article.article-item'):
                if (lt := article.select_one('h2.article-title a')) and (l := lt.get('href')): posts.append((lt.get_text(strip=True), l))
        elif "secretflying.com" in url:
            for article in soup.select('article.post-item'):
                if (lt := article.select_one('.post-title a')) and (l := lt.get('href')): posts.append((lt.get_text(strip=True), l))
        elif "wakacyjnipiraci.pl" in url:
            for article in soup.select('article.post-list__item'):
                if (lt := article.select_one('a.post-list__link')) and (l := lt.get('href')):
                    if tt := article.select_one('h2.post-list__title'): posts.append((tt.get_text(strip=True), l))
        log.info(f"Scraped {len(posts)} posts from {url}"); return posts
    except Exception as e: log.error(f"Error scraping {url}: {e}"); return []

def fetch_feed(client: httpx.Client, url: str) -> List[Tuple[str, str]]:
    posts = []; headers = {'User-Agent': random.choice(USER_AGENTS)}
    try:
        r = client.get(url, timeout=HTTP_TIMEOUT, follow_redirects=True, headers=headers); r.raise_for_status()
        feed = feedparser.parse(r.text)
        for entry in feed.entries:
            if (t := entry.get("title")) and (l := entry.get("link")): posts.append((t, l))
        log.info(f"Fetched {len(posts)} posts from {url}"); return posts
    except Exception as e: log.error(f"Error fetching RSS {url}: {e}"); return []

def send_telegram_message(client: httpx.Client, title: str, link: str):
    message = f"<b>{title}</b>\n\n{link}"; payload = {'chat_id': TG_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}
    try:
        r = client.post(TELEGRAM_API_URL, json=payload, timeout=HTTP_TIMEOUT); r.raise_for_status()
        log.info(f"Message sent: {title[:80]}…")
    except Exception as e: log.error(f"Telegram error for {link}: {e}")

@app.route('/tasks/rss', methods=['POST'])
def handle_rss_task():
    log.info("Starting SYNC processing (RSS + Scraping).")
    rss_sources = get_sources('rss_sources.txt'); web_sources = get_sources('web_sources.txt')
    state, generation = load_state(); sent_links_dict = state.get("sent_links", {})
    all_posts = []
    
    with httpx.Client() as client:
        for url in rss_sources: all_posts.extend(fetch_feed(client, url))
        for url in web_sources: all_posts.extend(scrape_webpage(client, url))

    new_posts, now_utc = [], datetime.now(timezone.utc)
    for title, link in all_posts:
        canonical = canonicalize_url(link)
        if canonical not in sent_links_dict: new_posts.append((title, link)); sent_links_dict[canonical] = now_utc.isoformat()

    if new_posts:
        log.info(f"Found {len(new_posts)} new posts. Sending.")
        with httpx.Client() as client:
            for title, link in new_posts: send_telegram_message(client, title, link); time.sleep(1)

    thirty_days_ago = now_utc - timedelta(days=30)
    cleaned_sent_links = {link: ts for link, ts in sent_links_dict.items() if datetime.fromisoformat(ts) > thirty_days_ago}
    state["sent_links"] = cleaned_sent_links
    save_state_atomic(state, generation)
    
    return f"Done. Sent {len(new_posts)} posts.", 200

@app.route('/')
def index(): return "Travel-Bot vSTABLE — działa!", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
