"""Small transactional SQLite store; the containing directory must be persistent."""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import Mapping


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            self.db.close()
            raise RuntimeError("Die Datenbank ist neuer als diese Bot-Version.")
        with self.db:
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS voice_stats (
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    total_seconds REAL NOT NULL DEFAULT 0 CHECK(total_seconds >= 0),
                    last_awarded_level INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            self.db.execute("PRAGMA user_version=1")

    def ensure_users(self, guild_id: int, user_ids: set[int]) -> None:
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO voice_stats (guild_id, user_id) VALUES (?, ?)",
                [(str(guild_id), str(user_id)) for user_id in user_ids],
            )

    def add_seconds(self, guild_id: int, deltas: Mapping[int, float]) -> None:
        rows = []
        for user_id, seconds in deltas.items():
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError("Zeitdifferenzen müssen endlich und nicht negativ sein.")
            if seconds > 0:
                rows.append((str(guild_id), str(user_id), seconds))
        if not rows:
            return
        with self.db:
            self.db.executemany("""
                INSERT INTO voice_stats (guild_id, user_id, total_seconds) VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE
                SET total_seconds = total_seconds + excluded.total_seconds
            """, rows)

    def total(self, guild_id: int, user_id: int) -> float:
        row = self.db.execute(
            "SELECT total_seconds FROM voice_stats WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id)),
        ).fetchone()
        return float(row[0]) if row else 0.0

    def totals(self, guild_id: int) -> dict[int, float]:
        return {int(row[0]): float(row[1]) for row in self.db.execute(
            "SELECT user_id, total_seconds FROM voice_stats WHERE guild_id=?", (str(guild_id),)
        )}

    def user_ids(self, guild_id: int) -> set[int]:
        return {int(row[0]) for row in self.db.execute(
            "SELECT user_id FROM voice_stats WHERE guild_id=?", (str(guild_id),)
        )}

    def last_awarded(self, guild_id: int, user_id: int) -> int:
        row = self.db.execute(
            "SELECT last_awarded_level FROM voice_stats WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id)),
        ).fetchone()
        return int(row[0]) if row else 0

    def mark_awarded(self, guild_id: int, user_id: int, level: int) -> None:
        self.ensure_users(guild_id, {user_id})
        with self.db:
            self.db.execute("""
                UPDATE voice_stats SET last_awarded_level=MAX(last_awarded_level, ?)
                WHERE guild_id=? AND user_id=?
            """, (level, str(guild_id), str(user_id)))

    def close(self) -> None:
        self.db.close()
