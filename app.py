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

# --- GŁÓWNA KONFIGURACJA ---
TG_TOKEN = os.environ.get('TG_TOKEN')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID')

BUCKET_NAME = 'travel-bot-storage-patrykmozeluk-cloud'  # <-- sprawdź nazwę bucketa
SENT_LINKS_FILE = 'sent_links.json'

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
HTTP_TIMEOUT = 15.0  # sekundy

# --- INICJALIZACJA ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
app = Flask(__name__)
storage_client = storage.Client()

# --- MODUŁ 1: KANONIZACJA URL ---
DROP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "igshid", "mc_cid", "mc_eid"
}

def canonicalize_url(url: str) -> str:
    """Normalizuje URL, usuwając parametry śledzące i sortując pozostałe."""
    try:
        p = urlparse(url.strip())
        scheme = p.scheme.lower() or "https"
        netloc = p.netloc.lower()
        path = p.path or "/"
        query_params = sorted([(k, v) for k, v in parse_qsl(p.query) if k.lower() not in DROP_PARAMS])
        return urlunparse((scheme, netloc, path, p.params, urlencode(query_params), ""))
    except Exception:
        return url.strip()

# --- MODUŁ 2: STAN W GCS ---
_bucket = storage_client.bucket(BUCKET_NAME)
_blob = _bucket.blob(SENT_LINKS_FILE)

def _default_state() -> Dict[str, Any]:
    return {"sent_links": []}

def load_state() -> Tuple[Dict[str, Any], int | None]:
    """Odczytuje stan i jego generację (wersję) z GCS."""
    try:
        if not _blob.exists():
            logging.info("Plik stanu nie istnieje, tworzę domyślny.")
            return _default_state(), None

        _blob.reload()
        data = _blob.download_as_bytes()
        generation = _blob.generation
        logging.info(f"Odczytano stan z GCS (generacja: {generation}).")
        return orjson.loads(data), generation
    except Exception as e:
        logging.error(f"Błąd przy pobieraniu stanu z GCS: {e}. Zaczynam z pustym stanem.")
        return _default_state(), None

def save_state_atomic(state: Dict[str, Any], expected_generation: int | None, retries: int = 5):
    """Zapisuje stan atomowo z mechanizmem ponawiania, aby uniknąć nadpisania."""
    payload = orjson.dumps(state)
    for attempt in range(retries):
        try:
            if expected_generation is None:
                _blob.upload_from_string(payload, if_generation_match=0, content_type="application/json")
            else:
                _blob.upload_from_string(payload, if_generation_match=expected_generation, content_type="application/json")
            logging.info("Pomyślnie zapisano stan w GCS.")
            return
        except Exception as e:
            if "PreconditionFailed" in str(e) or "412" in str(e):
                logging.warning(f"Konflikt zapisu (próba {attempt + 1}/{retries}). Ponawiam po odświeżeniu generacji.")
                time.sleep(0.5)
                _, expected_generation = load_state()
                continue
            logging.error(f"Nieoczekiwany błąd podczas zapisu stanu: {e}")
            raise
    raise RuntimeError("Krytyczny błąd: Nie udało się zapisać stanu atomowo po kilku próbach.")

# --- MODUŁ 3: LOGIKA BOTA ---
def get_rss_sources() -> List[str]:
    """Odczytuje listę kanałów RSS z pliku rss_sources.txt."""
    try:
        with open('rss_sources.txt', 'r') as f:
            sources = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        logging.info(f"Znaleziono {len(sources)} źródeł RSS w pliku.")
        return sources
    except FileNotFoundError:
        logging.error("KRYTYCZNY BŁĄD: Nie znaleziono pliku rss_sources.txt!")
        return []

async def fetch_feed(client: httpx.AsyncClient, url: str) -> List[Tuple[str, str]]:
    """Asynchronicznie pobiera i parsuje jeden kanał RSS, zwracając pary (tytuł, link)."""
    posts = []
    try:
        response = await client.get(url, timeout=HTTP_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        feed = feedparser.parse(response.text)
        for entry in feed.entries:
            if entry.get("link") and entry.get("title"):
                posts.append((entry.title, entry.link))
        logging.info(f"Pobrano {len(posts)} postów z {url}")
        return posts
    except Exception as e:
        logging.error(f"Błąd podczas przetwarzania kanału {url}: {e}")
        return []

async def send_telegram_message(client: httpx.AsyncClient, title: str, link: str):
    """Asynchronicznie wysyła jedną wiadomość (tytuł + link) do Telegrama."""
    message = f"<b>{title}</b>\n\n{link}"
    payload = {'chat_id': TG_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}
    try:
        response = await client.post(TELEGRAM_API_URL, json=payload, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        logging.info(f"Wiadomość wysłana: {title[:50]}...")
    except Exception as e:
        logging.error(f"Błąd wysyłania wiadomości Telegram dla linku {link}: {e}")

async def process_feeds_async():
    """Główna funkcja: pobiera RSS, filtruje i wysyła nowe wiadomości."""
    logging.info("Rozpoczynam asynchroniczne przetwarzanie kanałów RSS.")

    rss_sources = get_rss_sources()
    if not rss_sources:
        return "Brak źródeł RSS do przetworzenia."

    state, generation = load_state()
    sent_links_cache = set(state.get("sent_links", []))

    all_fetched_posts = []
    async with httpx.AsyncClient() as client:
        fetch_tasks = [fetch_feed(client, url) for url in rss_sources]
        results = await asyncio.gather(*fetch_tasks)
        for post_list in results:
            all_fetched_posts.extend(post_list)

    new_posts_to_send = []
    for title, link in all_fetched_posts:
        canonical_link = canonicalize_url(link)
        if canonical_link not in sent_links_cache:
            new_posts_to_send.append((title, link))
            sent_links_cache.add(canonical_link)

    if not new_posts_to_send:
        logging.info("Zakończono. Nie znaleziono nic nowego.")
        return "Zakończono. Nie znaleziono nic nowego."

    logging.info(f"Znaleziono {len(new_posts_to_send)} nowych postów. Rozpoczynam wysyłkę.")

    async with httpx.AsyncClient() as client:
        send_tasks = [send_telegram_message(client, title, link) for title, link in new_posts_to_send]
        await asyncio.gather(*send_tasks)

    state["sent_links"] = list(sent_links_cache)
    try:
        save_state_atomic(state, generation)
    except RuntimeError as e:
        logging.critical(f"KRYTYCZNY BŁĄD ZAPISU STANU: {e}. To spowoduje duplikaty przy następnym uruchomieniu!")
        return f"Błąd krytyczny: {e}"

    result_message = f"Zakończono. Wysłano {len(new_posts_to_send)} nowych postów."
    logging.info(result_message)
    return result_message

# --- MODUŁ 4: ROUTES ---
@app.route('/')
def index():
    return "Bot Travel-Bot żyje i jest w nowej, szybszej wersji!", 200

# >>> DODANY ENDPOINT HEALTHCHECK <<<
@app.route('/healthz')
def healthz():
    return jsonify({"status": "ok"}), 200
# ----------------------------------

@app.route('/tg/webhook', methods=['POST'])
async def telegram_webhook():
    """Odbiera komendy od użytkowników z Telegrama."""
    update = request.get_json()
    if update and "message" in update:
        text = update["message"].get("text")
        if text == "/start":
            async with httpx.AsyncClient() as client:
                await send_telegram_message(client, title="Cześć!", link="Jestem Twoim botem podróżniczym. Działam i szukam dla Ciebie okazji.")
    return "OK", 200

@app.route('/tasks/rss', methods=['POST'])
async def handle_rss_task():
    """Uruchamia zadanie sprawdzania RSS (dla Cloud Scheduler)."""
    result = await process_feeds_async()
    return result, 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(debug=True, host="0.0.0.0", port=port)
