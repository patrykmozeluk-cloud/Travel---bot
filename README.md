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

[🇵🇱 Instrukcja PL ↓](#-instrukcja-polski) | [🇬🇧 English guide ↓](#-instructions-english)

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

### 🔑 Jak zdobyć token i chat_id w Telegramie
1. **Token bota (`TG_TOKEN`)**  
   - Otwórz Telegram i znajdź użytkownika [@BotFather](https://t.me/BotFather).  
   - Wpisz `/newbot` i nadaj nazwę + unikalny login (np. `TravelBot123_bot`).  
   - BotFather poda Ci token w formacie:  
     ```
     1234567890:ABCdefGhIJKlmNoPQRstuVWxyz
     ```
   - Ten token wpisz do zmiennej środowiskowej `TG_TOKEN`.

2. **ID czatu (`TG_CHAT_ID`)**  
   - Dodaj swojego bota do grupy/kanalu.  
   - Napisz w tej grupie jakąś wiadomość.  
   - Wejdź w przeglądarkę:  
     ```
     https://api.telegram.org/bot<TG_TOKEN>/getUpdates
     ```
   - W odpowiedzi JSON znajdziesz pole `"chat":{"id": ... }` — to jest Twój `TG_CHAT_ID`.  
   - Dla grup ma format np. `-1001234567890`.

---

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

⬆️ Back to top | 🇬🇧 English guide ↓


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


🔑 How to get Telegram token & chat_id

1. Bot token (TG_TOKEN)

Open Telegram and talk to @BotFather.

Send /newbot → choose a name and unique username (e.g. TravelBot123_bot).

BotFather will give you a token like:

1234567890:ABCdefGhIJKlmNoPQRstuVWxyz

Save this as TG_TOKEN.



2. Chat ID (TG_CHAT_ID)

Add your bot to a group/channel.

Send a test message.

Open in browser:

https://api.telegram.org/bot<TG_TOKEN>/getUpdates

Look for "chat":{"id": ... } — that’s your TG_CHAT_ID.

For groups it usually looks like -1001234567890.

---

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

⬆️ Back to top | 🇵🇱 Instrukcja PL ↑


---

⚠️ Known issues / Typowe błędy w logach

send failed for <URL>:
↳ najczęściej timeout podczas wysyłki do Telegrama.
Normalne, link spróbuje się wysłać ponownie w następnym cyklu.
Można zwiększyć SEND_TIMEOUT_S do 15 s.

503 + długi latency w logach Cloud Run
↳ zimny start lub wolna odpowiedź z serwisu źródłowego.
Scheduler automatycznie ponawia próbę.

Telegram returned ok=false
↳ zwykle problem z uprawnieniami bota w grupie/kanale.
Sprawdź, czy bot ma prawo pisać do tej grupy.

Brak miniaturek przy niektórych linkach
↳ strona nie udostępnia og:image/twitter:image albo obrazek jest za duży.
Bot wtedy wyśle zwykły link z podglądem strony.

---

📂 Repo layout

.github/workflows/cron.yml   # optional GitHub Actions scheduler
Dockerfile
app.py
requirements.txt
rss_sources.txt
web_sources.txt

🔒 Security

Keep TG_TOKEN in environment variables/secrets

Optional: protect /tasks/rss with header X-Task-Secret


📜 License

MIT

---
