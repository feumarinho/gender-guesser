"""Wrapper sobre `gender-guesser-br`.

Normaliza a saída do pacote para payload estável e separa a consulta online
do IBGE da lógica de montagem de resposta local.
"""

import logging
import os
import time
from typing import Optional

logger = logging.getLogger('gender-guesser')
IBGE_RETRY_ATTEMPTS = max(1, int(os.getenv('IBGE_RETRY_ATTEMPTS', '3')))
IBGE_RETRY_BACKOFF_MS = max(50, int(os.getenv('IBGE_RETRY_BACKOFF_MS', '300')))
IBGE_VERIFY_SSL = os.getenv('IBGE_VERIFY_SSL', 'true').strip().lower() != 'false'


def _safe_extract(genero) -> dict:
    """Extrai os dicts `f` e `m` do objeto Genero de forma defensiva."""
    f = getattr(genero, 'f', None) or {}
    m = getattr(genero, 'm', None) or {}
    return {
        'f': {
            'absoluto': int(f.get('absoluto') or 0),
            'percentual': float(f.get('percentual') or 0.0),
        },
        'm': {
            'absoluto': int(m.get('absoluto') or 0),
            'percentual': float(m.get('percentual') or 0.0),
        },
    }


def _classification_to_biological_sex(classification: str) -> Optional[str]:
    if classification in ('masculino', 'provavelmente_masculino'):
        return 'M'
    if classification in ('feminino', 'provavelmente_feminino'):
        return 'F'
    return None


def _display_name(biological_sex: Optional[str]) -> Optional[str]:
    if biological_sex == 'M':
        return 'Masculino'
    if biological_sex == 'F':
        return 'Feminino'
    return None


def _from_values(
    *,
    name: str,
    uf: Optional[str],
    classification: str,
    m_abs: int,
    f_abs: int,
    m_pct: float,
    f_pct: float,
) -> dict:
    biological_sex = _classification_to_biological_sex(classification)
    confidence = max(m_pct, f_pct)
    return {
        'name': name,
        'uf': uf,
        'classification': classification,
        'biologicalSex': biological_sex,
        'displayName': _display_name(biological_sex),
        'confidence': round(confidence, 4),
        'absolute': {'M': m_abs, 'F': f_abs},
    }


def from_local_row(
    *,
    name: str,
    uf: Optional[str],
    classification: str,
    m_abs: int,
    f_abs: int,
    m_pct: float,
    f_pct: float,
) -> dict:
    """Monta payload normalizado a partir dos valores do banco local."""
    return _from_values(
        name=name,
        uf=uf,
        classification=classification,
        m_abs=m_abs,
        f_abs=f_abs,
        m_pct=m_pct,
        f_pct=f_pct,
    )


def guess_online(name: str, uf: Optional[str] = None) -> dict:
    """Consulta o IBGE via `gender-guesser-br` com retry/backoff.

    A resposta inclui:
      - `name`, `uf`
      - `classification`: string original do pacote (`masculino`, `ambos`, ...)
      - `biologicalSex`: 'M' | 'F' | None (canônico)
      - `displayName`: 'Masculino' | 'Feminino' | None (pt-BR)
      - `confidence`: max(percentual M, percentual F) — float em [0,1]
      - `absolute`: { 'M': int, 'F': int }
      - `error` e `error_kind`: opcionais quando IBGE falhar
    """
    from gender_guesser_br import Genero  # import preguiçoso

    base = {
        'name': name,
        'uf': uf,
        'classification': 'desconhecido',
        'biologicalSex': None,
        'displayName': None,
        'confidence': 0.0,
        'absolute': {'M': 0, 'F': 0},
    }

    last_error = None
    genero = None
    classification = 'desconhecido'
    for attempt in range(1, IBGE_RETRY_ATTEMPTS + 1):
        try:
            kwargs = {'nome': name}
            if uf:
                kwargs['uf'] = uf
            # Compatibilidade: versões atuais do gender-guesser-br podem não
            # aceitar `verificar_certificado` no construtor.
            try:
                if not IBGE_VERIFY_SSL:
                    kwargs['verificar_certificado'] = False
                genero = Genero(**kwargs)
            except TypeError as type_err:
                if "unexpected keyword argument 'verificar_certificado'" not in str(
                    type_err
                ):
                    raise
                kwargs.pop('verificar_certificado', None)
                genero = Genero(**kwargs)
            classification = genero()
            last_error = None
            break
        except Exception as err:  # noqa: BLE001 — defensivo contra falhas externas
            last_error = err
            logger.warning(
                "Tentativa %d/%d falhou no IBGE para nome='%s' uf='%s': %s",
                attempt,
                IBGE_RETRY_ATTEMPTS,
                name,
                uf or '-',
                err,
            )
            if attempt < IBGE_RETRY_ATTEMPTS:
                backoff = (IBGE_RETRY_BACKOFF_MS * (attempt ** 2)) / 1000.0
                time.sleep(backoff)

    if last_error is not None or genero is None:
        message = str(last_error).lower() if last_error else 'unknown'
        error_kind = 'network_error'
        if 'connection refused' in message or 'newconnectionerror' in message:
            error_kind = 'connection_refused'
        elif 'timed out' in message or 'read timeout' in message:
            error_kind = 'timeout'
        elif 'ssl' in message or 'certificate' in message:
            error_kind = 'ssl_error'

        logger.warning(
            "Falha ao consultar IBGE para nome='%s' uf='%s' kind=%s: %s",
            name,
            uf or '-',
            error_kind,
            last_error,
        )
        base['error'] = 'ibge_unavailable'
        base['error_kind'] = error_kind
        return base

    extracted = _safe_extract(genero)
    return _from_values(
        name=name,
        uf=uf,
        classification=classification,
        m_abs=extracted['m']['absoluto'],
        f_abs=extracted['f']['absoluto'],
        m_pct=extracted['m']['percentual'],
        f_pct=extracted['f']['percentual'],
    )
