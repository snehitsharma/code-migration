from __future__ import annotations
import sqlite3
from schemas import Symbol

SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    id TEXT PRIMARY KEY, file TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
    signature TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
    caller TEXT NOT NULL, callee TEXT NOT NULL, resolved INTEGER NOT NULL,
    PRIMARY KEY (caller, callee)
);
CREATE TABLE IF NOT EXISTS summaries (symbol_id TEXT PRIMARY KEY, summary TEXT NOT NULL);
"""


class SymbolIndex:
    """Writable SQLite index (Phase 0). Re-indexing replaces symbols and calls in one
    transaction, so indexing the same repo twice yields an identical snapshot."""

    def __init__(self, path: str = ":memory:") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def write(self, symbols: dict[str, Symbol]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM symbols")
            self.conn.execute("DELETE FROM calls")
            self.conn.executemany(
                "INSERT INTO symbols VALUES (?,?,?,?,?,?,?)",
                [(s.id, s.file, s.name, s.kind, s.signature, s.start_line, s.end_line)
                 for _, s in sorted(symbols.items())],
            )
            self.conn.executemany(
                "INSERT INTO calls VALUES (?,?,?)",
                [(s.id, c, int(c in symbols)) for _, s in sorted(symbols.items()) for c in s.calls],
            )
            # summaries survive re-indexing only for symbols that still exist
            self.conn.execute("DELETE FROM summaries WHERE symbol_id NOT IN (SELECT id FROM symbols)")

    def set_summary(self, symbol_id: str, summary: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO summaries VALUES (?,?)", (symbol_id, summary))

    def snapshot(self) -> dict:
        q = self.conn.execute
        return {
            "symbols": q("SELECT * FROM symbols ORDER BY id").fetchall(),
            "calls": q("SELECT * FROM calls ORDER BY caller, callee").fetchall(),
        }
