# Gender Guesser API

Microsserviço HTTP (Flask + Gunicorn) que infere o sexo biológico a partir de um primeiro nome, com cache SQLite, base local opcional e consulta ao IBGE quando necessário.

## Créditos e base

Este projeto **usa como base a biblioteca** [**gender-guesser-br**](https://github.com/GusFurtado/gender-guesser-br) (Gus Furtado), que por sua vez utiliza dados do Censo via [DadosAbertosBrasil](https://github.com/GusFurtado/DadosAbertosBrasil) / IBGE. A inferência online é feita através do pacote Python `gender-guesser-br` (`Genero`).

Arquitetura deste repositório:

- SQLite local (`gender_data.db`) para respostas rápidas e modo offline
- Fallback online via `gender-guesser-br` quando o modo híbrido precisa do IBGE
- Cache de respostas HTTP em SQLite separado (`cache.db`)

## Licença

Código aberto sob [licença MIT](LICENSE), alinhada ao ecossistema do projeto de referência.

## Requisitos

- Python 3.12+ (local) ou Docker
- Chave de API configurada (`GENDER_GUESSER_API_KEY`) — o endpoint `/guess` exige o header `x-gender-guesser-api-key`

## Rodar com Docker (local)

1. Copie as variáveis de ambiente:

   ```bash
   cp .env.example .env
   ```

   Edite `.env` e defina `GENDER_GUESSER_API_KEY`.

2. Suba o serviço:

   ```bash
   docker compose up --build -d
   ```

3. Teste:

   ```bash
   curl -s "http://127.0.0.1:5060/health"
   curl -s -H "x-gender-guesser-api-key: SUA_CHAVE" "http://127.0.0.1:5060/guess?name=carlos"
   ```

O mapeamento `5060:5000` expõe apenas em localhost; em produção, publique sem `ports` públicos ou atrás de um reverse proxy.

## Imagem no Docker Hub

Existe uma imagem de referência **`feumarinho/gender-gesser`** no [Docker Hub](https://hub.docker.com/r/feumarinho/gender-gesser) para quem prefere não construir a partir deste repositório. Os builds publicados pelo GitHub Actions são **multi-arquitetura** (`linux/amd64` e `linux/arm64`), por exemplo para servidores x86 e Mac Apple Silicon sem `platform:` no compose.

```bash
docker pull feumarinho/gender-gesser:latest
docker run -d --name gender-guesser -p 5060:5000 \
  -e GENDER_GUESSER_API_KEY=sua-chave \
  -v gender-data:/app/data \
  feumarinho/gender-gesser:latest
```

Defina `GENDER_GUESSER_API_KEY` (e outras variáveis, se precisar) como no [`.env.example`](.env.example). Os exemplos deste README que usam `docker compose exec gender-guesser …` funcionam da mesma com a imagem do Hub: use `docker exec gender-guesser …` (ou o nome do contentor que escolheu no `docker run`).

## Rodar sem Docker (desenvolvimento)

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edite .env
python api.py
```

## Endpoints

### `GET /health`

```json
{
  "status": "ok",
  "service": "gender-guesser",
  "cache": { "total_entries": 1234, "ttl_seconds": 2592000 }
}
```

### `GET /guess?name=<nome>&uf=<sigla?>`

Header obrigatório: `x-gender-guesser-api-key: <chave>`.

Resposta (200):

```json
{
  "name": "carlos",
  "uf": null,
  "classification": "masculino",
  "biologicalSex": "M",
  "displayName": "Masculino",
  "confidence": 0.998,
  "absolute": { "M": 1245301, "F": 2310 },
  "source": "cache"
}
```

- `classification`: valor do pacote (`masculino`, `feminino`, `provavelmente_masculino`, `provavelmente_feminino`, `ambos`, `desconhecido`).
- `biologicalSex`: `'M'`, `'F'` ou `null` (ambos / desconhecido).
- `confidence`: maior percentual entre M e F (0 a 1).
- `source`: `cache`, `local`, `ibge_fallback`, `online`, `local_degraded`.
- Em falha do IBGE sem fallback local, retorna `error: "ibge_unavailable"` (503).
- Em modo híbrido, quando um nome não existe localmente e o fallback online é bem-sucedido, o serviço faz **upsert** na base local.

Erros: `400` (parâmetros), `401` (API key), `429` (rate limit).

## Variáveis de ambiente

| Var | Default | Descrição |
| --- | --- | --- |
| `GENDER_GUESSER_API_KEY` | _vazio_ | Chave compartilhada; obrigatória para `/guess`. |
| `GENDER_MODE` | `hybrid` | `offline`, `hybrid` ou `online`. |
| `LOCAL_DB_PATH` | `/app/data/gender_data.db` | Banco local (híbrido/offline). |
| `IBGE_FALLBACK_ENABLED` | `true` | Se `false`, híbrido não consulta IBGE. |
| `LOCAL_CONFIDENCE_THRESHOLD` | `0.7` | Limiar legado de confiança local. |
| `IBGE_RETRY_ATTEMPTS` | `3` | Tentativas no fallback online. |
| `IBGE_RETRY_BACKOFF_MS` | `300` | Backoff base (quadrático) entre tentativas. |
| `IBGE_VERIFY_SSL` | `true` | Verificação TLS nas chamadas IBGE. |
| `CACHE_DB_PATH` | `/app/data/cache.db` | SQLite de cache. |
| `CACHE_TTL_DAYS` | `30` | TTL do cache. |
| `RATE_LIMIT_PER_MIN` | `60` | Janela de 60s; `0` desativa. |
| `LOG_LEVEL` | `INFO` | `DEBUG` para troubleshooting. |
| `PORT` | `5000` | Apenas em `python api.py` (produção: Gunicorn na porta 5000). |

## Cache e dados

- Cache de resposta: `CACHE_DB_PATH` (TTL configurável).
- Base de nomes: `LOCAL_DB_PATH` (seed opcional).

## Base local com Brasil.IO (menos chamadas ao IBGE)

O [Brasil.IO](https://brasil.io/) publica o conjunto **genero-nomes** com um arquivo agregado de grupos de nomes, adequado para encher o SQLite local de uma vez:

- Arquivo: [`grupos.csv.gz`](https://data.brasil.io/dataset/genero-nomes/grupos.csv.gz) (mesmo esquema de colunas que o importador espera: `name`, `frequency_female`, `frequency_male`, `frequency_total`, `names`, etc.).

Com o serviço já em execução via Docker Compose e o volume `/app/data` montado, basta **um comando** para baixar o `.gz` e importar para `gender_data.db`:

```bash
docker compose exec gender-guesser /app/scripts/import_brasilio_grupos.sh
```

Isso grava em `/app/data/grupos.csv.gz` (reutilizável em importações futuras) e atualiza o banco local. Em modo `hybrid`, muitos `/guess` passam a responder com `source: local` sem ir ao IBGE. Para **não consultar o IBGE nunca**, use `GENDER_MODE=offline` ou `IBGE_FALLBACK_ENABLED=false` (nomes que ainda não existirem no SQLite continuarão sem resposta online nesse caso).

Opções extras do importador (repassadas ao script):

```bash
docker compose exec gender-guesser /app/scripts/import_brasilio_grupos.sh --include-aliases --min-total 5
```

- `--include-aliases`: também grava variantes listadas na coluna `names` (base maior, import mais longo).
- `--min-total`: ignora linhas com `frequency_total` abaixo do valor (default `1`).

Variáveis opcionais no `exec` (sobrescrevem URL ou destino do download):

| Variável | Default |
| --- | --- |
| `BRASILIO_GRUPOS_URL` | `https://data.brasil.io/dataset/genero-nomes/grupos.csv.gz` |
| `BRASILIO_GRUPOS_GZ` | `/app/data/grupos.csv.gz` |

Exemplo com URL explícita:

```bash
docker compose exec -e BRASILIO_GRUPOS_URL="https://data.brasil.io/dataset/genero-nomes/grupos.csv.gz" gender-guesser /app/scripts/import_brasilio_grupos.sh
```

**Sem Docker** (ficheiro local ou já descarregado):

```bash
curl -fsSL -o grupos.csv.gz "https://data.brasil.io/dataset/genero-nomes/grupos.csv.gz"
python import_grupos_csv.py --csv-path grupos.csv.gz
```

O importador aceita `.csv` ou `.csv.gz` em `--csv-path`.

## Seed da base local (IBGE via `gender-guesser-br`)

Estes scripts consultam o IBGE através do pacote (útil para recortes por UF/década). Para uma base nacional ampla com um único download, prefira a secção [Base local com Brasil.IO](#base-local-com-brasilio-io-menos-chamadas-ao-ibge) acima.

```bash
python seed_local_db.py --top-n 1500
python seed_local_db.py --top-n 1000 --include-ufs SP,RJ,MG
python seed_local_db.py --top-n 500 --only-br
```

Via Docker:

```bash
docker compose exec gender-guesser python seed_local_db.py --top-n 1000 --include-ufs SP,RJ,MG
```

Seed amplo (checkpoint/resume):

```bash
docker compose exec gender-guesser python seed_local_db_wide.py --top-n-per-scope 200
```

Checkpoint padrão: `/app/data/seed_checkpoint.json` — opções `--include-ufs`, `--decades`, `--force-restart`, etc.

## Contribuir

Issues e pull requests são bem-vindos. Mantenha o escopo focado e documente mudanças de comportamento da API ou de variáveis de ambiente no README.
