from __future__ import annotations
from typing import Optional
from kb.index import SymbolIndex
from schemas import DependencyGraph, Symbol


class KnowledgeBase:
    """Read-only lookups over a SymbolIndex. Agents and nodes use this, never the index."""

    def __init__(self, index: SymbolIndex) -> None:
        self._q = index.conn.execute

    def symbols(self) -> dict[str, Symbol]:
        rows = self._q(
            "SELECT id, file, name, kind, signature, start_line, end_line FROM symbols ORDER BY id"
        ).fetchall()
        calls: dict[str, list[str]] = {}
        for caller, callee in self._q("SELECT caller, callee FROM calls ORDER BY caller, callee"):
            calls.setdefault(caller, []).append(callee)
        return {
            r[0]: Symbol(id=r[0], file=r[1], name=r[2], kind=r[3], signature=r[4],
                         start_line=r[5], end_line=r[6], calls=calls.get(r[0], []))
            for r in rows
        }

    def get_symbol(self, symbol_id: str) -> Optional[Symbol]:
        return self.symbols().get(symbol_id)

    def dependencies(self, symbol_id: str) -> list[str]:
        """Resolved callees."""
        return [r[0] for r in self._q(
            "SELECT callee FROM calls WHERE caller=? AND resolved=1 ORDER BY callee", (symbol_id,))]

    def dependents(self, symbol_id: str) -> list[str]:
        return [r[0] for r in self._q(
            "SELECT caller FROM calls WHERE callee=? AND resolved=1 ORDER BY caller", (symbol_id,))]

    def unresolved(self, symbol_id: str) -> list[str]:
        return [r[0] for r in self._q(
            "SELECT callee FROM calls WHERE caller=? AND resolved=0 ORDER BY callee", (symbol_id,))]

    def summary(self, symbol_id: str) -> Optional[str]:
        row = self._q("SELECT summary FROM summaries WHERE symbol_id=?", (symbol_id,)).fetchone()
        return row[0] if row else None

    def dependency_graph(self) -> DependencyGraph:
        edges: dict[str, list[str]] = {r[0]: [] for r in self._q("SELECT id FROM symbols ORDER BY id")}
        for caller, callee in self._q(
                "SELECT caller, callee FROM calls WHERE resolved=1 ORDER BY caller, callee"):
            edges[caller].append(callee)
        return DependencyGraph(edges=edges)
