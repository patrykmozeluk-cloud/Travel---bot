# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopiujemy WSZYSTKIE pliki, których potrzebuje Twój hybrydowy kod
COPY app.py .
COPY rss_sources.txt .
COPY web_sources.txt .

ENV PORT=8080
# Klasyczna, pancerna komenda startowa dla synchronicznego bota
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 120 --worker-tmp-dir /dev/shm app:app
