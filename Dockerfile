# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps (cacerts, locales optional)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App files
COPY app.py .
COPY rss_sources.txt .

# Gunicorn config via CMD
# - 1 worker (Cloud Run skaluje instancje), threads=8, timeout 120s
# - gthread dobrze znosi I/O i async w Flask (uruchomi się sekwencyjnie, ale równolegle w wątkach)
ENV PORT=8080
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 120 --worker-tmp-dir /dev/shm app:app
