FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api.py cache.py guesser.py local_store.py seed_local_db.py seed_local_db_wide.py import_grupos_csv.py ./
COPY scripts/import_brasilio_grupos.sh ./scripts/
RUN chmod +x ./scripts/import_brasilio_grupos.sh

# Diretório do cache SQLite (montado como volume em runtime).
RUN mkdir -p /app/data

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:5000/health > /dev/null || exit 1

CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "2", "--timeout", "120", "api:app"]
