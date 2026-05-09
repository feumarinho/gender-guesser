"""Seed amplo da base local com checkpoint/resume.

Coleta nomes em volume maior usando:
- rankings por década (1930..2010) no Brasil
- rankings por década em UFs selecionadas

Persistência de progresso:
- checkpoint JSON salvo periodicamente (default: /app/data/seed_checkpoint.json)
- em nova execução, retoma do último item processado por escopo
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
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
logger = logging.getLogger('gender-guesser-seed-wide')


@dataclass
class ScopeResult:
    processed: int = 0
    ok: int = 0
    failed: int = 0
    skipped_existing: int = 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Seed amplo com checkpoint.')
    parser.add_argument(
        '--top-n-per-scope',
        type=int,
        default=200,
        help='Máximo de nomes por escopo (sexo/decada/localidade).',
    )
    parser.add_argument(
        '--decades',
        type=str,
        default='1930,1940,1950,1960,1970,1980,1990,2000,2010',
        help='Lista de décadas separadas por vírgula.',
    )
    parser.add_argument(
        '--include-ufs',
        type=str,
        default='SP,RJ,MG,BA,RS,PR,PE,CE,PA,SC',
        help='UFs separadas por vírgula para ampliar o seed.',
    )
    parser.add_argument(
        '--only-br',
        action='store_true',
        help='Executa apenas escopo nacional (sem UFs).',
    )
    parser.add_argument(
        '--sleep-ms',
        type=int,
        default=30,
        help='Pausa entre chamadas ao IBGE para reduzir pressão.',
    )
    parser.add_argument(
        '--local-db-path',
        type=str,
        default=os.getenv('LOCAL_DB_PATH', '/app/data/gender_data.db'),
        help='Caminho do SQLite local.',
    )
    parser.add_argument(
        '--checkpoint-path',
        type=str,
        default=os.getenv('SEED_CHECKPOINT_PATH', '/app/data/seed_checkpoint.json'),
        help='Caminho do checkpoint JSON.',
    )
    parser.add_argument(
        '--save-every',
        type=int,
        default=25,
        help='Salva checkpoint a cada N itens processados.',
    )
    parser.add_argument(
        '--force-restart',
        action='store_true',
        help='Ignora checkpoint existente e reinicia do zero.',
    )
    return parser.parse_args()


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(',') if item.strip()]


def _unique_preserve(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _load_checkpoint(path: Path, force_restart: bool) -> dict:
    if force_restart or not path.exists():
        return {'scopes': {}}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:  # noqa: BLE001
        logger.warning('Checkpoint inválido em %s. Reiniciando.', path)
        return {'scopes': {}}


def _save_checkpoint(path: Path, checkpoint: dict) -> None:
    _ensure_parent(path)
    path.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def _resolve_uf_ids() -> dict[str, int]:
    from DadosAbertosBrasil import ibge

    table = ibge.localidades(
        nivel='estados',
        formato='pandas',
        verificar_certificado=True,
    )
    mapping: dict[str, int] = {}
    for _, row in table.iterrows():
        sigla = str(row['sigla']).strip().upper()
        mapping[sigla] = int(row['id'])
    return mapping


def _collect_scope_names(
    *,
    localidade: int | None,
    decades: list[int],
    top_n_per_scope: int,
) -> list[str]:
    from DadosAbertosBrasil import ibge

    names: list[str] = []
    for sexo in ('f', 'm'):
        for decada in decades:
            try:
                ranking = ibge.nomes_ranking(
                    decada=decada,
                    sexo=sexo,
                    localidade=localidade,
                    formato='pandas',
                    verificar_certificado=True,
                )
            except Exception as err:  # noqa: BLE001
                logger.warning(
                    'Falha no ranking (sexo=%s, decada=%s, localidade=%s): %s',
                    sexo,
                    decada,
                    localidade if localidade is not None else 'BR',
                    err,
                )
                continue
            if 'nome' not in ranking.columns:
                continue
            names.extend(ranking['nome'].astype(str).head(top_n_per_scope).tolist())

    first_names = [n.split(' ')[0] for n in names if n and str(n).strip()]
    return _unique_preserve(first_names)


def _scope_key(uf: str | None) -> str:
    return uf or 'BR'


def _seed_scope(
    *,
    store: NameStatsStore,
    names: list[str],
    uf: str | None,
    sleep_ms: int,
    save_every: int,
    checkpoint: dict,
    checkpoint_path: Path,
) -> ScopeResult:
    key = _scope_key(uf)
    scope_state = checkpoint.setdefault('scopes', {}).setdefault(
        key,
        {'next_index': 0, 'ok': 0, 'failed': 0, 'skipped_existing': 0},
    )
    start_index = int(scope_state.get('next_index', 0))
    result = ScopeResult(
        processed=start_index,
        ok=int(scope_state.get('ok', 0)),
        failed=int(scope_state.get('failed', 0)),
        skipped_existing=int(scope_state.get('skipped_existing', 0)),
    )

    if start_index >= len(names):
        logger.info('[%s] já concluído no checkpoint (%d itens).', key, len(names))
        return result

    logger.info(
        '[%s] retomando em %d/%d (ok=%d, fail=%d, skipped=%d)',
        key,
        start_index,
        len(names),
        result.ok,
        result.failed,
        result.skipped_existing,
    )

    for idx in range(start_index, len(names)):
        name = names[idx]
        result.processed = idx + 1

        # Skip opcional: se já existe no store para esse escopo, não consulta IBGE.
        existing = store.get(name, uf)
        if existing:
            result.skipped_existing += 1
        else:
            payload = guess_online(name, uf)
            if 'error' in payload:
                result.failed += 1
            else:
                store.upsert_from_payload(name, uf, payload)
                result.ok += 1

        scope_state['next_index'] = result.processed
        scope_state['ok'] = result.ok
        scope_state['failed'] = result.failed
        scope_state['skipped_existing'] = result.skipped_existing

        if result.processed % save_every == 0:
            _save_checkpoint(checkpoint_path, checkpoint)
            logger.info(
                '[%s] progresso %d/%d (ok=%d, fail=%d, skipped=%d)',
                key,
                result.processed,
                len(names),
                result.ok,
                result.failed,
                result.skipped_existing,
            )

        if sleep_ms > 0:
            time.sleep(sleep_ms / 1000.0)

    _save_checkpoint(checkpoint_path, checkpoint)
    return result


def main() -> int:
    args = _parse_args()
    store = NameStatsStore(args.local_db_path)
    checkpoint_path = Path(args.checkpoint_path)
    checkpoint = _load_checkpoint(checkpoint_path, args.force_restart)

    decades = [int(d) for d in _split_csv(args.decades)]
    ufs = [] if args.only_br else [u.strip().upper() for u in _split_csv(args.include_ufs)]

    logger.info(
        'Seed amplo iniciado (db=%s, checkpoint=%s, top_n_per_scope=%d, decades=%s, ufs=%s)',
        args.local_db_path,
        checkpoint_path,
        args.top_n_per_scope,
        decades,
        ufs if ufs else ['-'],
    )

    uf_ids = _resolve_uf_ids() if ufs else {}

    # Coleta nomes BR
    br_names = _collect_scope_names(
        localidade=None,
        decades=decades,
        top_n_per_scope=args.top_n_per_scope,
    )
    logger.info('[BR] nomes coletados: %d', len(br_names))
    br_result = _seed_scope(
        store=store,
        names=br_names,
        uf=None,
        sleep_ms=args.sleep_ms,
        save_every=max(1, args.save_every),
        checkpoint=checkpoint,
        checkpoint_path=checkpoint_path,
    )
    logger.info(
        '[BR] final: processados=%d ok=%d fail=%d skipped=%d',
        br_result.processed,
        br_result.ok,
        br_result.failed,
        br_result.skipped_existing,
    )

    total_ok = br_result.ok
    total_failed = br_result.failed
    total_skipped = br_result.skipped_existing

    for uf in ufs:
        localidade = uf_ids.get(uf)
        if not localidade:
            logger.warning('[%s] UF ignorada (não encontrada no IBGE).', uf)
            continue

        uf_names = _collect_scope_names(
            localidade=localidade,
            decades=decades,
            top_n_per_scope=args.top_n_per_scope,
        )
        logger.info('[%s] nomes coletados: %d', uf, len(uf_names))
        uf_result = _seed_scope(
            store=store,
            names=uf_names,
            uf=uf,
            sleep_ms=args.sleep_ms,
            save_every=max(1, args.save_every),
            checkpoint=checkpoint,
            checkpoint_path=checkpoint_path,
        )
        logger.info(
            '[%s] final: processados=%d ok=%d fail=%d skipped=%d',
            uf,
            uf_result.processed,
            uf_result.ok,
            uf_result.failed,
            uf_result.skipped_existing,
        )
        total_ok += uf_result.ok
        total_failed += uf_result.failed
        total_skipped += uf_result.skipped_existing

    logger.info(
        'Seed amplo concluído: ok=%d fail=%d skipped=%d total_entries_local=%d',
        total_ok,
        total_failed,
        total_skipped,
        store.stats()['total_entries'],
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

