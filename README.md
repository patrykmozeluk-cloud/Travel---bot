README.md.

# ✈️ Travel-Bot

> 🇵🇱 **TL;DR (skrót)**  
> Bot pobiera oferty z RSS i stron, filtruje duplikaty i wysyła **świeże linki + miniatury** na Telegram.  
> Działa na **Google Cloud Run (Gen1)**, odpalany co **15 minut** przez **Cloud Scheduler**.  
> Token i ID czatu trzymasz w zmiennych środowiskowych.  
> Stare wpisy są czyszczone po **30 dniach**.

> 🇬🇧 **TL;DR (short)**  
> The bot fetches travel deals from RSS & websites, removes duplicates, and sends **fresh links + thumbnails** to Telegram.  
> Runs on **Google Cloud Run (Gen1)**, triggered every **15 minutes** by **Cloud Scheduler**.  
> Token and chat ID are stored in environment variables.  
> Old entries are cleaned after **30 days**.

---

[🇵🇱 Instrukcja PL ↓](#-instrukcja-polski) |
[🇬🇧 English guide ↓](#-instructions-english)

---

# 🇵🇱 Instrukcja (Polski)

### Funkcje
- Pobiera dane z **RSS** i prosty **scraping** stron podróżniczych  
- Wysyła do Telegrama:
  - ze zdjęciem (`sendPhoto`), jeśli znajdzie `og:image/twitter:image`  
  - zwykły link (`sendMessage`) z podglądem, jeśli brak miniatury  
- **Dedup**: ten sam link nie zostanie wysłany dwa razy  
- **Czyszczenie co 30 dni** — starsze wpisy znikają z pliku `sent_links.json`  
- Stabilne limity czasu, retry do Telegrama, asynchroniczne pobieranie  

### Uruchomienie lokalne
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export TG_TOKEN="123:ABC..."
export TG_CHAT_ID="-1001234567890"
export BUCKET_NAME="travel-bot-storage-patrykmozeluk-cloud"
export SENT_LINKS_FILE="sent_links.json"

python app.py
curl -X POST http://localhost:8080/tasks/rss



# ✈️ Travel-Bot

Automatyczny bot do śledzenia źródeł (RSS i strony WWW) i wysyłania **świeżych ofert** na Telegram.  
Runs on **Google Cloud Run (Gen1)**, triggered every **15 minutes** by **Cloud Scheduler**.

---

## 🇵🇱 Instrukcja (Polski)

### Funkcje
- Pobiera dane z **RSS** i prosty **scraping** stron podróżniczych  
- Wysyła do Telegrama:
  - ze zdjęciem (`sendPhoto`), jeśli znajdzie `og:image/twitter:image`  
  - zwykły link (`sendMessage`) z podglądem, jeśli brak miniatury  
- **Dedup**: ten sam link nie zostanie wysłany dwa razy  
- **Czyszczenie co 30 dni** — starsze wpisy znikają z pliku `sent_links.json`  
- Stabilne limity czasu, retry do Telegrama, asynchroniczne pobieranie  

### Uruchomienie lokalne
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export TG_TOKEN="123:ABC..."
export TG_CHAT_ID="-1001234567890"
export BUCKET_NAME="travel-bot-storage-patrykmozeluk-cloud"
export SENT_LINKS_FILE="sent_links.json"

python app.py
curl -X POST http://localhost:8080/tasks/rss

Deploy na Cloud Run

Gen1 (First generation)

RAM: 1 GiB

Timeout: 600 s

Concurrency: 80

Zmienne środowiskowe:

TG_TOKEN, TG_CHAT_ID, BUCKET_NAME, SENT_LINKS_FILE

opcjonalnie: HTTP_TIMEOUT=20.0, SEND_TIMEOUT_S=15, MAX_POSTS_PER_RUN=15



Cloud Scheduler

Cron: */15 * * * *

Target: POST https://<your-service>.run.app/tasks/rss

Headers: Content-Type: application/json

Retry: max 5 prób, min backoff 30s, max backoff 10m, deadline 60s



---

🇬🇧 Instructions (English)

Features

Fetches from RSS + simple web scraping

Sends to Telegram:

with photo (sendPhoto) if og:image/twitter:image found

with link preview (sendMessage) if no thumbnail


Deduplication: no duplicates across runs

30-day cleanup of old entries in sent_links.json

Stable timeouts, retries to Telegram, async fetching


Local run

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export TG_TOKEN="123:ABC..."
export TG_CHAT_ID="-1001234567890"
export BUCKET_NAME="travel-bot-storage-patrykmozeluk-cloud"
export SENT_LINKS_FILE="sent_links.json"

python app.py
curl -X POST http://localhost:8080/tasks/rss

Deploy on Cloud Run

Gen1 (First generation)

RAM: 1 GiB

Timeout: 600 s

Concurrency: 80

Env variables:

TG_TOKEN, TG_CHAT_ID, BUCKET_NAME, SENT_LINKS_FILE

optional: HTTP_TIMEOUT=20.0, SEND_TIMEOUT_S=15, MAX_POSTS_PER_RUN=15



Cloud Scheduler

Cron: */15 * * * *

Target: POST https://<your-service>.run.app/tasks/rss

Headers: Content-Type: application/json

Retry: max 5 attempts, min backoff 30s, max backoff 10m, deadline 60s



---

📂 Repo layout

.github/workflows/cron.yml   # optional GitHub Actions scheduler
Dockerfile
app.py
requirements.txt
rss_sources.txt
web_sources.txt


---

🔒 Security

Keep TG_TOKEN in environment variables/secrets

Optional: protect /tasks/rss with header X-Task-Secret

License:

MIT 
