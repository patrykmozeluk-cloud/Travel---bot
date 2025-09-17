
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
   - Tokeny nadaje tylko [@BotFather](https://t.me/BotFather).  
   - Otwórz czat z BotFather → wpisz `/newbot` → wybierz nazwę i login (np. `TravelBot123_bot`).  
   - BotFather poda Ci token w formacie:  
     ```
     1234567890:ABCdefGhIJKlmNoPQRstuVWxyz
     ```
   - Ten token zapisz w zmiennej środowiskowej `TG_TOKEN`.

2. **ID czatu (`TG_CHAT_ID`)**  
   Masz dwie opcje:  
   - **Szybka metoda**: dodaj do grupy/kanału bota narzędziowego [@userinfobot](https://t.me/userinfobot).  
     Odpowie Ci od razu z `chat_id`.  
   - **Oficjalna metoda**: dodaj swojego bota, wyślij wiadomość w grupie/kanału i otwórz:  
     ```
     https://api.telegram.org/bot<TG_TOKEN>/getUpdates
     ```
     W odpowiedzi JSON znajdziesz `"chat":{"id": ... }` → to Twój `TG_CHAT_ID`.  
   - Dla grup ma format np. `-1001234567890`.

---

### Uruchomienie lokalne
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export TG_TOKEN="123:ABC..."
export TG_CHAT_ID="-1001234567890"
export BUCKET_NAME="twoja-wlasna-nazwa-bucketa"
export SENT_LINKS_FILE="sent_links.json"

python app.py
curl -X POST http://localhost:8080/tasks/rss

---

Deploy na Cloud Run

Gen1 (First generation)

RAM: 1 GiB

Timeout: 600 s

Concurrency: 80


Zmienne środowiskowe:

TG_TOKEN, TG_CHAT_ID, BUCKET_NAME, SENT_LINKS_FILE

opcjonalnie: HTTP_TIMEOUT=20.0, SEND_TIMEOUT_S=15, MAX_POSTS_PER_RUN=15

---

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

30-day cleanup — old entries removed from sent_links.json

Stable timeouts, retries to Telegram, async fetching


### 🔑 How to get Telegram token & chat_id

1. **Bot token (`TG_TOKEN`)**  
   - Tokens are only issued by [@BotFather](https://t.me/BotFather).  
   - Open BotFather → send `/newbot` → choose a name and unique username (e.g. `TravelBot123_bot`).  
   - BotFather will return a token like:  
     ```
     1234567890:ABCdefGhIJKlmNoPQRstuVWxyz
     ```
   - Save this value in the environment variable `TG_TOKEN`.

2. **Chat ID (`TG_CHAT_ID`)**  
   You have two options:  
   - **Quick method**: add a helper bot like [@userinfobot](https://t.me/userinfobot) to your group/channel.  
     It will immediately reply with the `chat_id`.  
   - **Official method**: add your bot, send a message in the group/channel and open:  
     ```
     https://api.telegram.org/bot<TG_TOKEN>/getUpdates
     ```
     In the JSON response, look for `"chat":{"id": ... }` → that’s your `TG_CHAT_ID`.  
   - For groups it usually looks like `-1001234567890`.

---

Local run

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export TG_TOKEN="123:ABC..."
export TG_CHAT_ID="-1001234567890"
export BUCKET_NAME="your-own-bucket-name"
export SENT_LINKS_FILE="sent_links.json"

python app.py
curl -X POST http://localhost:8080/tasks/rss

---

Deploy on Cloud Run

Gen1 (First generation)

RAM: 1 GiB

Timeout: 600 s

Concurrency: 80


Env variables:

TG_TOKEN, TG_CHAT_ID, BUCKET_NAME, SENT_LINKS_FILE

optional: HTTP_TIMEOUT=20.0, SEND_TIMEOUT_S=15, MAX_POSTS_PER_RUN=15

---

Cloud Scheduler

Cron: */15 * * * *

Target: POST https://<your-service>.run.app/tasks/rss

Headers: Content-Type: application/json

Retry: max 5 attempts, min backoff 30s, max backoff 10m, deadline 60s

---

⬆️ Back to top | 🇵🇱 Instrukcja PL ↑

---

⚠️ Typowe błędy w logach / Known issues

🇵🇱 Typowe błędy w logach

send failed for <URL>
↳ najczęściej timeout podczas wysyłki do Telegrama.
Normalne: link spróbuje się wysłać ponownie w następnym cyklu.
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

🇬🇧 Common errors in logs

send failed for <URL>
↳ usually a timeout when sending to Telegram.
Normal: the link will be retried in the next cycle.
You can increase SEND_TIMEOUT_S to 15 s.

503 + high latency in Cloud Run logs
↳ cold start or slow response from source website.
Scheduler will retry automatically.

Telegram returned ok=false
↳ usually missing permissions for the bot in the group/channel.
Check if the bot can post messages.

No thumbnails for some links
↳ site doesn’t expose og:image/twitter:image or image is too large.
Bot falls back to sending plain link with preview.

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

---

📜 License

MIT

---
