"""Small SQLite cache used for incremental, per-symbol refreshes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterable, Mapping

from .models import utc_now_iso


@dataclass(frozen=True)
class CacheRecord:
    namespace: str
    key: str
    payload: Mapping[str, Any]
    fetched_at: str
    source_as_of: str | None
    schema_version: int
    fresh: bool


class SQLiteCache:
    """Thread-safe JSON cache with independent records for every ticker.

    A failed refresh never deletes an older record.  The caller can therefore
    surface stale data explicitly while unaffected symbols continue updating.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def get(
        self,
        namespace: str,
        key: str,
        *,
        max_age_seconds: float | None = None,
    ) -> CacheRecord | None:
        records = self.get_many(
            namespace, [key], max_age_seconds=max_age_seconds
        )
        return records.get(key)

    def get_many(
        self,
        namespace: str,
        keys: Iterable[str],
        *,
        max_age_seconds: float | None = None,
    ) -> dict[str, CacheRecord]:
        normalized = list(dict.fromkeys(str(key) for key in keys))
        if not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        query = (
            "SELECT cache_key, payload_json, fetched_at, source_as_of, schema_version "
            f"FROM cache_entries WHERE namespace = ? AND cache_key IN ({placeholders})"
        )
        with self._lock, self._connection() as connection:
            rows = connection.execute(query, [namespace, *normalized]).fetchall()
        output: dict[str, CacheRecord] = {}
        now = datetime.now(timezone.utc)
        for row in rows:
            try:
                payload = json.loads(row[1])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            fetched = _parse_timestamp(row[2])
            fresh = True
            if max_age_seconds is not None:
                fresh = fetched is not None and (
                    now - fetched
                ).total_seconds() <= max_age_seconds
            output[str(row[0])] = CacheRecord(
                namespace=namespace,
                key=str(row[0]),
                payload=payload,
                fetched_at=str(row[2]),
                source_as_of=str(row[3]) if row[3] is not None else None,
                schema_version=int(row[4]),
                fresh=fresh,
            )
        return output

    def put(
        self,
        namespace: str,
        key: str,
        payload: Mapping[str, Any],
        *,
        source_as_of: str | None = None,
        fetched_at: str | None = None,
        schema_version: int = 1,
    ) -> None:
        self.put_many(
            namespace,
            {key: payload},
            source_as_of={key: source_as_of},
            fetched_at=fetched_at,
            schema_version=schema_version,
        )

    def put_many(
        self,
        namespace: str,
        payloads: Mapping[str, Mapping[str, Any]],
        *,
        source_as_of: Mapping[str, str | None] | None = None,
        fetched_at: str | None = None,
        schema_version: int = 1,
    ) -> None:
        if not payloads:
            return
        timestamp = fetched_at or utc_now_iso()
        rows = [
            (
                namespace,
                str(key),
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                timestamp,
                (source_as_of or {}).get(key),
                schema_version,
            )
            for key, payload in payloads.items()
        ]
        statement = """
            INSERT INTO cache_entries
                (namespace, cache_key, payload_json, fetched_at, source_as_of, schema_version)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(namespace, cache_key) DO UPDATE SET
                payload_json=excluded.payload_json,
                fetched_at=excluded.fetched_at,
                source_as_of=excluded.source_as_of,
                schema_version=excluded.schema_version
        """
        with self._lock, self._connection() as connection:
            connection.executemany(statement, rows)
            connection.commit()

    def delete_namespace(self, namespace: str) -> int:
        """Delete one narrow cache namespace; primarily useful in tests."""

        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM cache_entries WHERE namespace = ?", (namespace,)
            )
            connection.commit()
            return int(cursor.rowcount)

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_entries (
                    namespace TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    source_as_of TEXT,
                    schema_version INTEGER NOT NULL,
                    PRIMARY KEY(namespace, cache_key)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_fetched_at "
                "ON cache_entries(namespace, fetched_at)"
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
