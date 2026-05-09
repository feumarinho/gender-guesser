#!/usr/bin/env sh
# Baixa grupos.csv.gz do Brasil.IO e importa para o SQLite local (evita depender do IBGE em muitos nomes).
# Uso no container:
#   docker compose exec gender-guesser /app/scripts/import_brasilio_grupos.sh
# Opções extras são repassadas ao import_grupos_csv.py, ex.:
#   .../import_brasilio_grupos.sh --include-aliases --min-total 5

set -eu

URL="${BRASILIO_GRUPOS_URL:-https://data.brasil.io/dataset/genero-nomes/grupos.csv.gz}"
DEST="${BRASILIO_GRUPOS_GZ:-/app/data/grupos.csv.gz}"

mkdir -p "$(dirname "$DEST")"

echo "[brasil.io] Download: $URL -> $DEST"
curl -fsSL -o "$DEST" "$URL"

echo "[brasil.io] Importando para o banco local..."
exec python /app/import_grupos_csv.py --csv-path "$DEST" "$@"
