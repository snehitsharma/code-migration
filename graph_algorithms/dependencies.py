from __future__ import annotations
from schemas import DependencyGraph, Symbol


def build_dependency_graph(symbols: dict[str, Symbol]) -> DependencyGraph:
    """Edges symbol -> callee, restricted to known symbols, sorted and de-duplicated."""
    edges = {
        sid: sorted({c for c in sym.calls if c in symbols})
        for sid, sym in sorted(symbols.items())
    }
    return DependencyGraph(edges=edges)


def unresolved_calls(symbols: dict[str, Symbol]) -> dict[str, list[str]]:
    """Calls that point outside the indexed symbols (stdlib, third party, parser misses)."""
    out = {
        sid: sorted({c for c in sym.calls if c not in symbols})
        for sid, sym in sorted(symbols.items())
    }
    return {sid: calls for sid, calls in out.items() if calls}
