import hmac
import logging
import os
import re
import threading
import time
from collections import deque

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from cache import GuessCache
from guesser import from_local_row, guess_online
from local_store import NameStatsStore

load_dotenv()

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('gender-guesser')

API_KEY = (os.getenv('GENDER_GUESSER_API_KEY') or '').strip()
CACHE_DB_PATH = os.getenv('CACHE_DB_PATH', '/app/data/cache.db')
CACHE_TTL_DAYS = int(os.getenv('CACHE_TTL_DAYS', '30'))
RATE_LIMIT_PER_MIN = int(os.getenv('RATE_LIMIT_PER_MIN', '60'))
NAME_MAX_LEN = 100
GENDER_MODE = os.getenv('GENDER_MODE', 'hybrid').strip().lower()
LOCAL_DB_PATH = os.getenv('LOCAL_DB_PATH', '/app/data/gender_data.db')
IBGE_FALLBACK_ENABLED = (
    os.getenv('IBGE_FALLBACK_ENABLED', 'true').strip().lower() != 'false'
)
LOCAL_CONFIDENCE_THRESHOLD = float(
    os.getenv('LOCAL_CONFIDENCE_THRESHOLD', '0.7').strip()
)
CONF_HIGH_RATIO = float(os.getenv('CONF_HIGH_RATIO', '0.95'))
CONF_MEDIUM_RATIO = float(os.getenv('CONF_MEDIUM_RATIO', '0.80'))
MIN_TOTAL_HIGH = int(os.getenv('MIN_TOTAL_HIGH', '30'))
MIN_TOTAL_MEDIUM = int(os.getenv('MIN_TOTAL_MEDIUM', '15'))
MEDIUM_CONFIDENCE_MODE = os.getenv('MEDIUM_CONFIDENCE_MODE', 'verify_ibge').strip().lower()

if not API_KEY:
    logger.warning('GENDER_GUESSER_API_KEY não configurada — endpoints rejeitarão todas as chamadas.')

cache = GuessCache(CACHE_DB_PATH, ttl_days=CACHE_TTL_DAYS)
local_store = NameStatsStore(LOCAL_DB_PATH)
logger.info(
    'Gender Guesser iniciando — mode=%s cache=%s local_db=%s ttl=%dd rate_limit=%d/min',
    GENDER_MODE,
    CACHE_DB_PATH,
    LOCAL_DB_PATH,
    CACHE_TTL_DAYS,
    RATE_LIMIT_PER_MIN,
)

app = Flask(__name__)

_NAME_PATTERN = re.compile(r"^[A-Za-zÀ-ÿ\s'\-]+$")
_UF_PATTERN = re.compile(r'^[A-Za-z]{2}$')

_rate_lock = threading.Lock()
_rate_window: deque = deque()


def _is_valid_api_key(provided: str) -> bool:
    if not API_KEY or not provided:
        return False
    return hmac.compare_digest(provided, API_KEY)


def _check_rate_limit() -> bool:
    """Sliding window simples por chave única de API (60s)."""
    if RATE_LIMIT_PER_MIN <= 0:
        return True
    now = time.time()
    cutoff = now - 60
    with _rate_lock:
        while _rate_window and _rate_window[0] < cutoff:
            _rate_window.popleft()
        if len(_rate_window) >= RATE_LIMIT_PER_MIN:
            return False
        _rate_window.append(now)
    return True


def _normalize_name(raw: str) -> str:
    """Pega só o primeiro nome e normaliza para lowercase sem espaços extras."""
    cleaned = ' '.join(raw.split()).strip()
    if not cleaned:
        return ''
    first = cleaned.split(' ')[0]
    return first.lower()


def _normalize_uf(raw: str) -> str:
    return raw.strip().upper() if raw else ''


def _to_local_payload(name: str, requested_uf: str, row: dict) -> dict:
    return from_local_row(
        name=name,
        uf=requested_uf or None,
        classification=row['classification'],
        m_abs=row['m_abs'],
        f_abs=row['f_abs'],
        m_pct=row['m_pct'],
        f_pct=row['f_pct'],
    )


def _confidence_bucket(payload: dict) -> str:
    absolute = payload.get('absolute') or {}
    total = int(absolute.get('M') or 0) + int(absolute.get('F') or 0)
    ratio = float(payload.get('confidence') or 0.0)

    if ratio >= CONF_HIGH_RATIO and total >= MIN_TOTAL_HIGH:
        return 'high'
    if ratio >= CONF_MEDIUM_RATIO and total >= MIN_TOTAL_MEDIUM:
        return 'medium'
    return 'low'


@app.route('/health')
def health():
    return jsonify(
        {
            'status': 'ok',
            'service': 'gender-guesser',
            'mode': GENDER_MODE,
            'cache': cache.stats(),
            'local_store': local_store.stats(),
            'policy': {
                'high_ratio': CONF_HIGH_RATIO,
                'medium_ratio': CONF_MEDIUM_RATIO,
                'min_total_high': MIN_TOTAL_HIGH,
                'min_total_medium': MIN_TOTAL_MEDIUM,
                'medium_mode': MEDIUM_CONFIDENCE_MODE,
            },
        }
    )


@app.route('/guess')
def guess_endpoint():
    client_ip = request.remote_addr

    provided_key = request.headers.get('x-gender-guesser-api-key', '')
    if not _is_valid_api_key(provided_key):
        logger.warning('[REQUEST] Rejeitado: API key inválida from %s', client_ip)
        return jsonify({'error': 'Não autorizado'}), 401

    if not _check_rate_limit():
        logger.warning('[REQUEST] Rate limit excedido from %s', client_ip)
        return jsonify({'error': 'Rate limit excedido'}), 429

    raw_name = (request.args.get('name') or '').strip()
    raw_uf = (request.args.get('uf') or '').strip()

    if not raw_name:
        return jsonify({'error': "Parâmetro 'name' é obrigatório"}), 400
    if len(raw_name) > NAME_MAX_LEN:
        return jsonify({'error': f"Parâmetro 'name' excede {NAME_MAX_LEN} caracteres"}), 400
    if not _NAME_PATTERN.match(raw_name):
        return jsonify({'error': "Parâmetro 'name' contém caracteres inválidos"}), 400
    if raw_uf and not _UF_PATTERN.match(raw_uf):
        return jsonify({'error': "Parâmetro 'uf' deve ter 2 letras"}), 400

    name = _normalize_name(raw_name)
    uf = _normalize_uf(raw_uf)
    cache_key_uf = uf or 'BR'
    no_cache = (request.args.get('noCache') or '').strip().lower() == 'true'

    if not name:
        return jsonify({'error': "Parâmetro 'name' inválido após normalização"}), 400

    logger.info(
        "[REQUEST] GET /guess?name='%s'%s from %s",
        name,
        f"&uf={uf}" if uf else '',
        client_ip,
    )

    cached = None if no_cache else cache.get(name, cache_key_uf)
    if cached and 'error' not in cached:
        logger.debug("Cache hit name='%s' uf='%s'", name, cache_key_uf)
        return jsonify({**cached, 'source': 'cache'})

    local_row = local_store.get(name, uf or None)
    local_payload = _to_local_payload(name, uf, local_row) if local_row else None
    local_confidence = float(local_payload.get('confidence', 0.0)) if local_payload else 0
    local_bucket = _confidence_bucket(local_payload) if local_payload else None

    # Modo offline: nunca consulta IBGE.
    if GENDER_MODE == 'offline':
        if local_payload:
            if not no_cache:
                cache.set(name, cache_key_uf, local_payload)
            return jsonify({**local_payload, 'source': 'local', 'confidence_bucket': local_bucket})
        return jsonify({'error': 'name_not_found_offline'}), 404

    # Modo híbrido: local-first.
    if GENDER_MODE == 'hybrid' and local_payload:
        # Compat legado: threshold antigo também habilita fast path.
        if local_confidence >= LOCAL_CONFIDENCE_THRESHOLD and local_bucket == 'high':
            if not no_cache:
                cache.set(name, cache_key_uf, local_payload)
            return jsonify({**local_payload, 'source': 'local', 'confidence_bucket': local_bucket})
        if local_bucket == 'high':
            if not no_cache:
                cache.set(name, cache_key_uf, local_payload)
            return jsonify({**local_payload, 'source': 'local', 'confidence_bucket': local_bucket})
        if local_bucket == 'medium' and MEDIUM_CONFIDENCE_MODE == 'local_first':
            if not no_cache:
                cache.set(name, cache_key_uf, local_payload)
            return jsonify({**local_payload, 'source': 'local', 'confidence_bucket': local_bucket})

    if GENDER_MODE == 'hybrid' and not IBGE_FALLBACK_ENABLED:
        if local_payload:
            if not no_cache:
                cache.set(name, cache_key_uf, local_payload)
            return jsonify({**local_payload, 'source': 'local', 'confidence_bucket': local_bucket})
        return jsonify({'error': 'ibge_fallback_disabled'}), 503

    # Modo online ou fallback híbrido.
    payload = guess_online(name, uf or None)
    if 'error' not in payload:
        local_store.upsert_from_payload(name, uf or None, payload)
        if not no_cache:
            cache.set(name, cache_key_uf, payload)
        logger.info(
            "Resultado name='%s' uf='%s' classification='%s' confidence=%.3f",
            name,
            cache_key_uf,
            payload.get('classification'),
            payload.get('confidence', 0.0),
        )
        source = 'online' if GENDER_MODE == 'online' else 'ibge_fallback'
        return jsonify({**payload, 'source': source, 'confidence_bucket': _confidence_bucket(payload)})

    # Falha online: no híbrido, devolve local degradado se existir.
    if GENDER_MODE == 'hybrid' and local_payload:
        degraded = {
            **local_payload,
            'source': 'local_degraded',
            'degraded': True,
            'fallback_error_kind': payload.get('error_kind'),
            'confidence_bucket': local_bucket,
        }
        if not no_cache:
            cache.set(name, cache_key_uf, degraded)
        return jsonify(degraded)

    # Não cachear erro de IBGE para evitar "congelar" indisponibilidade.
    return jsonify({**payload, 'source': 'online_error'}), 503


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    logger.info('Servidor iniciando em 0.0.0.0:%d', port)
    app.run(host='0.0.0.0', port=port)
