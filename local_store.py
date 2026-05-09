"""Armazenamento local persistente para modo híbrido/offline."""

import os
import sqlite3
import threading
import time
from typing import Optional


class NameStatsStore:
    def __init__(self, db_path: str):
        self._db_path = db_path
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
                CREATE TABLE IF NOT EXISTS name_stats (
                    name_normalized TEXT NOT NULL,
                    uf TEXT NOT NULL DEFAULT 'BR',
                    classification TEXT NOT NULL,
                    m_abs INTEGER NOT NULL,
                    f_abs INTEGER NOT NULL,
                    m_pct REAL NOT NULL,
                    f_pct REAL NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (name_normalized, uf)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_name_stats_name
                ON name_stats (name_normalized)
                """
            )
            conn.commit()

    def get(self, name: str, uf: Optional[str]) -> Optional[dict]:
        """Busca nome por UF; se não encontrar, tenta BR."""
        key_uf = (uf or 'BR').upper()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                SELECT name_normalized, uf, classification, m_abs, f_abs, m_pct, f_pct, updated_at
                FROM name_stats
                WHERE name_normalized = ? AND uf = ?
                """,
                (name, key_uf),
            )
            row = cur.fetchone()
            if not row and key_uf != 'BR':
                cur = conn.execute(
                    """
                    SELECT name_normalized, uf, classification, m_abs, f_abs, m_pct, f_pct, updated_at
                    FROM name_stats
                    WHERE name_normalized = ? AND uf = 'BR'
                    """,
                    (name,),
                )
                row = cur.fetchone()

        if not row:
            return None

        return {
            'name_normalized': row[0],
            'uf': row[1],
            'classification': row[2],
            'm_abs': int(row[3]),
            'f_abs': int(row[4]),
            'm_pct': float(row[5]),
            'f_pct': float(row[6]),
            'updated_at': int(row[7]),
        }

    def upsert_from_payload(self, name: str, uf: Optional[str], payload: dict) -> None:
        absolute = payload.get('absolute') or {}
        m_abs = int(absolute.get('M') or 0)
        f_abs = int(absolute.get('F') or 0)
        total = max(1, m_abs + f_abs)
        m_pct = float(m_abs / total)
        f_pct = float(f_abs / total)
        classification = payload.get('classification') or 'desconhecido'
        key_uf = (uf or 'BR').upper()

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO name_stats
                (name_normalized, uf, classification, m_abs, f_abs, m_pct, f_pct, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    key_uf,
                    classification,
                    m_abs,
                    f_abs,
                    m_pct,
                    f_pct,
                    int(time.time()),
                ),
            )
            conn.commit()

    def stats(self) -> dict:
        with self._lock, self._connect() as conn:
            cur = conn.execute('SELECT COUNT(*) FROM name_stats')
            total = cur.fetchone()[0]
        return {'total_entries': total}

