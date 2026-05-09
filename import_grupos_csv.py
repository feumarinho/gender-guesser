"""Importa `grupos.csv` para o banco local do gender-guesser.

Formato esperado do CSV:
name,classification,frequency_female,frequency_male,frequency_total,ratio,names
"""

from __future__ import annotations

import argparse
import csv
import gzip
import logging
import os
import time

from dotenv import load_dotenv

from local_store import NameStatsStore

load_dotenv()

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('gender-guesser-import')


def _open_csv_text(path: str):
    """Abre CSV ou CSV gzipado (.csv.gz)."""
    if path.endswith('.gz'):
        return gzip.open(path, 'rt', encoding='utf-8', newline='')
    return open(path, newline='', encoding='utf-8')


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Importador de grupos.csv')
    parser.add_argument(
        '--csv-path',
        required=True,
        help='Caminho para grupos.csv ou grupos.csv.gz (ex.: dump Brasil.IO)',
    )
    parser.add_argument(
        '--local-db-path',
        default=os.getenv('LOCAL_DB_PATH', '/app/data/gender_data.db'),
        help='Caminho para gender_data.db',
    )
    parser.add_argument(
        '--include-aliases',
        action='store_true',
        help='Importa aliases da coluna names.',
    )
    parser.add_argument(
        '--min-total',
        type=int,
        default=1,
        help='Ignora nomes com frequency_total menor que este valor.',
    )
    return parser.parse_args()


def _normalize_name(value: str) -> str:
    clean = ' '.join((value or '').split()).strip()
    if not clean:
        return ''
    return clean.split(' ')[0].lower()


def _classification_from_counts(m_abs: int, f_abs: int) -> str:
    total = max(1, m_abs + f_abs)
    m_pct = m_abs / total
    f_pct = f_abs / total
    if m_pct >= 0.9:
        return 'masculino'
    if f_pct >= 0.9:
        return 'feminino'
    if m_pct >= 0.6:
        return 'provavelmente_masculino'
    if f_pct >= 0.6:
        return 'provavelmente_feminino'
    return 'ambos'


def _payload_from_row(row: dict) -> dict:
    m_abs = int(float(row.get('frequency_male') or 0))
    f_abs = int(float(row.get('frequency_female') or 0))
    total = max(1, m_abs + f_abs)
    return {
        'classification': _classification_from_counts(m_abs, f_abs),
        'absolute': {'M': m_abs, 'F': f_abs},
        'confidence': max(m_abs / total, f_abs / total),
    }


def _extract_aliases(names_blob: str) -> list[str]:
    # Formato esperado: |ALINE|ALYNE|...
    raw_items = [item.strip() for item in (names_blob or '').split('|')]
    aliases = []
    seen = set()
    for item in raw_items:
        if not item:
            continue
        normalized = _normalize_name(item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        aliases.append(normalized)
    return aliases


def main() -> int:
    args = _parse_args()
    store = NameStatsStore(args.local_db_path)

    csv_path = args.csv_path
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f'CSV não encontrado: {csv_path}')

    start = time.time()
    rows = 0
    inserted_primary = 0
    inserted_alias = 0
    skipped_low_total = 0
    skipped_invalid = 0

    with _open_csv_text(csv_path) as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            rows += 1
            try:
                total = int(float(row.get('frequency_total') or 0))
            except Exception:  # noqa: BLE001
                skipped_invalid += 1
                continue

            if total < args.min_total:
                skipped_low_total += 1
                continue

            base_name = _normalize_name(row.get('name') or '')
            if not base_name:
                skipped_invalid += 1
                continue

            payload = _payload_from_row(row)
            store.upsert_from_payload(base_name, None, payload)
            inserted_primary += 1

            if args.include_aliases:
                aliases = _extract_aliases(row.get('names') or '')
                for alias in aliases:
                    if alias == base_name:
                        continue
                    store.upsert_from_payload(alias, None, payload)
                    inserted_alias += 1

            if rows % 1000 == 0:
                logger.info(
                    'Progresso %d linhas (primary=%d alias=%d invalid=%d low_total=%d)',
                    rows,
                    inserted_primary,
                    inserted_alias,
                    skipped_invalid,
                    skipped_low_total,
                )

    elapsed = time.time() - start
    logger.info(
        'Import concluído em %.1fs: rows=%d primary=%d alias=%d invalid=%d low_total=%d total_db=%d',
        elapsed,
        rows,
        inserted_primary,
        inserted_alias,
        skipped_invalid,
        skipped_low_total,
        store.stats()['total_entries'],
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

