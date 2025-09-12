# Etap 1: Instalowanie narzędzi
FROM python:3.11-slim as builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Etap 2: Budowanie finalnego, lekkiego obrazu
FROM python:3.11-slim

WORKDIR /app

COPY --from=builder /root/.local /root/.local
COPY . .

ENV PATH=/root/.local/bin:$PATH

# Uruchomienie aplikacji przy użyciu serwera Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "app:app"]
