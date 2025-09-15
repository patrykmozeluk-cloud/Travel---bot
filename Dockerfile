# bazowy Python
FROM python:3.11-slim

# szybkie i przewidywalne logi
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# system deps (ssl, kompilatory do niektórych paczek)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    build-essential \
    libffi-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# najpierw zależności Pythona
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# potem kod i pliki źródeł
COPY app.py rss_sources.txt web_sources.txt ./

# (opcjonalnie) pokaż wersje — pomaga w debug logach uruchomieniowych
# RUN python -V && pip freeze

EXPOSE 8080

# Gunicorn: 1 worker + 8 wątków, timeout > budżet joba (55s), access log do STDOUT
CMD ["gunicorn", "app:app", \
     "-b", "0.0.0.0:8080", \
     "--workers=1", \
     "--threads=8", \
     "--timeout=65", \
     "--graceful-timeout=30", \
     "--access-logfile=-"]
