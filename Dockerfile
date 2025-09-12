# bazowy Python
FROM python:3.11-slim

# zmienne środowiskowe
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# instalacja zależności systemowych (certyfikaty SSL itd.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# katalog aplikacji
WORKDIR /app

# kopiuj requirements i zainstaluj
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# kopiuj kod źródłowy
COPY . .

# gunicorn jako server (gthread dla async)
CMD ["gunicorn", "-b", "0.0.0.0:8080", "--workers=1", "--threads=8", "app:app"]
