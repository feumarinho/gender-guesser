"""Seed da base local `gender_data.db` para modo híbrido/offline.

Estratégia:
1) Coleta ranking nacional (IBGE) por sexo.
2) Opcionalmente coleta ranking por UF.
3) Para cada nome, consulta `Genero(nome, uf?)` e grava no SQLite local.

Uso (no container ou venv com dependências instaladas):
  python seed_local_db.py --top-n 2000
  python seed_local_db.py --top-n 1000 --include-ufs SP,RJ,MG
  python seed_local_db.py --top-n 500 --only-br
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Iterable

from dotenv import load_dotenv

from guesser import guess_online
from local_store import NameStatsStore

load_dotenv()

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('gender-guesser-seed')


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Seed da base local de nomes.')
    parser.add_argument(
        '--top-n',
        type=int,
        default=1500,
        help='Quantidade máxima de nomes por universo (BR/UF).',
    )
    parser.add_argument(
        '--include-ufs',
        type=str,
        default='',
        help='Lista de UFs separadas por vírgula (ex.: SP,RJ,MG).',
    )
    parser.add_argument(
        '--only-br',
        action='store_true',
        help='Se ativo, faz seed apenas nacional (sem UFs).',
    )
    parser.add_argument(
        '--sleep-ms',
        type=int,
        default=20,
        help='Pausa entre consultas para reduzir pressão no endpoint.',
    )
    parser.add_argument(
        '--local-db-path',
        type=str,
        default=os.getenv('LOCAL_DB_PATH', '/app/data/gender_data.db'),
        help='Caminho do SQLite local de nomes.',
    )
    return parser.parse_args()


def _as_list(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        normalized = value.strip().upper()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _extract_top_names_for_scope(scope_uf: str | None, top_n: int) -> list[str]:
    """Busca ranking de nomes com DadosAbertosBrasil para BR ou UF."""
    from DadosAbertosBrasil import ibge

    localidade = None
    if scope_uf:
        try:
            # Aceita sigla de UF diretamente.
            localidades = ibge.localidades(
                nivel='estados',
                formato='pandas',
                verificar_certificado=True,
            )
            row = localidades[localidades['sigla'].str.upper() == scope_uf.upper()]
            if row.empty:
                logger.warning("UF '%s' não encontrada em ibge.localidades", scope_uf)
                return []
            localidade = int(row.iloc[0]['id'])
        except Exception as err:  # noqa: BLE001
            logger.warning("Falha ao resolver localidade da UF '%s': %s", scope_uf, err)
            return []

    names: list[str] = []
    for sexo in ('f', 'm'):
        try:
            ranking = ibge.nomes_ranking(
                sexo=sexo,
                localidade=localidade,
                formato='pandas',
                verificar_certificado=True,
            )
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "Falha ao buscar ranking (sexo=%s, uf=%s): %s",
                sexo,
                scope_uf or 'BR',
                err,
            )
            continue

        if 'nome' not in ranking.columns:
            continue
        names.extend(ranking['nome'].astype(str).head(top_n).tolist())

    # Normaliza e dedup mantendo ordem.
    normalized = [n.strip().split(' ')[0].lower() for n in names if n.strip()]
    dedup: list[str] = []
    seen = set()
    for item in normalized:
        if item in seen:
            continue
        seen.add(item)
        dedup.append(item)
    return dedup[:top_n]


def _seed_scope(
    store: NameStatsStore,
    uf: str | None,
    top_n: int,
    sleep_ms: int,
) -> tuple[int, int]:
    names = _extract_top_names_for_scope(uf, top_n)
    if not names:
        return (0, 0)

    inserted = 0
    failed = 0
    for idx, name in enumerate(names, start=1):
        payload = guess_online(name, uf)
        if 'error' in payload:
            failed += 1
            continue
        store.upsert_from_payload(name, uf, payload)
        inserted += 1

        if idx % 100 == 0:
            logger.info(
                '[%s] progresso %d/%d (ok=%d, fail=%d)',
                uf or 'BR',
                idx,
                len(names),
                inserted,
                failed,
            )
        if sleep_ms > 0:
            time.sleep(sleep_ms / 1000.0)
    return (inserted, failed)


def main() -> int:
    args = _parse_args()
    store = NameStatsStore(args.local_db_path)

    ufs: list[str] = []
    if not args.only_br:
        ufs = _as_list(args.include_ufs.split(',')) if args.include_ufs else []

    logger.info(
        'Iniciando seed local (db=%s, top_n=%d, only_br=%s, ufs=%s)',
        args.local_db_path,
        args.top_n,
        args.only_br,
        ','.join(ufs) if ufs else '-',
    )

    total_ok = 0
    total_fail = 0

    ok_br, fail_br = _seed_scope(
        store=store,
        uf=None,
        top_n=args.top_n,
        sleep_ms=args.sleep_ms,
    )
    total_ok += ok_br
    total_fail += fail_br
    logger.info('[BR] finalizado ok=%d fail=%d', ok_br, fail_br)

    for uf in ufs:
        ok_uf, fail_uf = _seed_scope(
            store=store,
            uf=uf,
            top_n=args.top_n,
            sleep_ms=args.sleep_ms,
        )
        total_ok += ok_uf
        total_fail += fail_uf
        logger.info('[%s] finalizado ok=%d fail=%d', uf, ok_uf, fail_uf)

    logger.info(
        'Seed concluído: total_ok=%d total_fail=%d total_local_entries=%d',
        total_ok,
        total_fail,
        store.stats()['total_entries'],
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

