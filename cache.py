"""SQLite-backed cache for gender guesses.

A consulta ao IBGE pelo `gender-guesser-br` envolve chamadas HTTP via
`DadosAbertosBrasil`, o que é caro. Cacheamos o resultado por (nome, uf)
com TTL em dias para evitar repetir a chamada.
"""

import json
import os
import sqlite3
import threading
import time
from typing import Optional


class GuessCache:
    def __init__(self, db_path: str, ttl_days: int = 30):
        self._db_path = db_path
        self._ttl_seconds = ttl_days * 24 * 3600
        self._lock = threading.Lock()
        self._ensure_dir()
        self._init_schema()

    def _ensure_dir(self) -> None:
        directory = os.path.dirname(self._db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10, check_same_thread=False)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS guesses (
                    name TEXT NOT NULL,
                    uf   TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    cached_at INTEGER NOT NULL,
                    PRIMARY KEY (name, uf)
                )
                """
            )
            conn.commit()

    def get(self, name: str, uf: str) -> Optional[dict]:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                'SELECT payload, cached_at FROM guesses WHERE name = ? AND uf = ?',
                (name, uf),
            )
            row = cur.fetchone()
        if not row:
            return None
        payload_str, cached_at = row
        if time.time() - cached_at > self._ttl_seconds:
            return None
        try:
            return json.loads(payload_str)
        except json.JSONDecodeError:
            return None

    def set(self, name: str, uf: str, payload: dict) -> None:
        payload_str = json.dumps(payload, ensure_ascii=False)
        cached_at = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO guesses (name, uf, payload, cached_at) VALUES (?, ?, ?, ?)',
                (name, uf, payload_str, cached_at),
            )
            conn.commit()

    def stats(self) -> dict:
        with self._lock, self._connect() as conn:
            cur = conn.execute('SELECT COUNT(*) FROM guesses')
            total = cur.fetchone()[0]
        return {'total_entries': total, 'ttl_seconds': self._ttl_seconds}
