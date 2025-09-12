import os
import json
import requests
from flask import Flask, request
from fastfeedparser import parse
from google.cloud import storage

# --- Konfiguracja ---
# Pobieranie "sekretów" ze środowiska, które ustawi nam Google Cloud
TG_TOKEN = os.environ.get('TG_TOKEN')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID')

# WAŻNE: Tę nazwę będziemy musieli później stworzyć w Google Cloud.
# Musi być unikalna na całym świecie. Proponuję taką, jest duża szansa, że będzie wolna.
BUCKET_NAME = 'travel-bot-storage-patrykmozeluk-cloud'
SENT_LINKS_FILE = 'sent_links.json'

# Inicjalizacja klienta Google Cloud Storage ("pamięci" bota)
storage_client = storage.Client()
app = Flask(__name__)

# --- Funkcje do obsługi "pamięci" bota ---

def get_sent_links():
    """Pobiera zbiór wysłanych linków z pliku w Google Cloud Storage."""
    try:
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(SENT_LINKS_FILE)
        if blob.exists():
            links_json = blob.download_as_text()
            return set(json.loads(links_json))
        return set()
    except Exception as e:
        print(f"Błąd przy pobieraniu pliku z GCS: {e}. Zaczynam z pustą listą.")
        return set()

def save_sent_links(links_set):
    """Zapisuje zbiór wysłanych linków do pliku w Google Cloud Storage."""
    try:
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(SENT_LINKS_FILE)
        blob.upload_from_string(json.dumps(list(links_set)), content_type='application/json')
        print(f"Zapisano {len(links_set)} linków w pamięci.")
    except Exception as e:
        print(f"Krytyczny błąd przy zapisywaniu pliku do GCS: {e}")

def get_rss_sources():
    """Odczytuje listę kanałów RSS z pliku rss_sources.txt."""
    try:
        with open('rss_sources.txt', 'r') as f:
            # Zwraca listę, ignorując puste linie i te zaczynające się od '#'
            sources = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        return sources
    except FileNotFoundError:
        print("OSTRZEŻENIE: Nie znaleziono pliku rss_sources.txt!")
        return []

# --- Główna logika ---

def send_telegram_message(text):
    """Wysyła wiadomość na zdefiniowany kanał Telegram."""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {'chat_id': TG_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Błąd wysyłania wiadomości Telegram: {e}")
        return None

def process_rss_feeds():
    """Główna funkcja: pobiera RSS, filtruje i wysyła nowe wiadomości."""
    print("Rozpoczynam przetwarzanie kanałów RSS...")
    sent_links = get_sent_links()
    initial_links_count = len(sent_links)

    rss_sources = get_rss_sources()
    if not rss_sources:
        return "Brak zdefiniowanych źródeł RSS w rss_sources.txt. Kończę pracę."

    new_links_found = 0
    for source_url in rss_sources:
        try:
            feed = parse(source_url)
            for entry in feed.entries:
                link = entry.link
                if link and link not in sent_links:
                    title = entry.title
                    message = f"<b>{title}</b>\n\n{link}"

                    print(f"Nowy link: {link}")
                    send_telegram_message(message)

                    sent_links.add(link)
                    new_links_found += 1
        except Exception as e:
            print(f"Błąd podczas przetwarzania {source_url}: {e}")

    if new_links_found > 0:
        save_sent_links(sent_links)
        return f"Zakończono. Znaleziono i wysłano {new_links_found} nowych linków."
    else:
        return "Zakończono. Nie znaleziono nic nowego."

# --- Definicje "adresów" URL naszej aplikacji ---

@app.route('/')
def index():
    return "Bot Travel-Bot żyje!", 200

@app.route('/tg/webhook', methods=['POST'])
def telegram_webhook():
    """Odbiera komendy od użytkowników z Telegrama."""
    update = request.get_json()
    if update and "message" in update:
        text = update["message"].get("text")
        if text == "/start":
            send_telegram_message("Cześć! Jestem Twoim botem podróżniczym. Działam i szukam dla Ciebie okazji.")
    return "OK", 200

@app.route('/tasks/rss', methods=['POST'])
def handle_rss_task():
    """Uruchamia zadanie sprawdzania RSS (dla automatu)."""
    result = process_rss_feeds()
    print(result)
    return result, 200

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

