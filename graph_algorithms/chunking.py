from __future__ import annotations
from pathlib import Path
from schemas import Chunk, SCCGroup, Symbol


def group_lines(group: SCCGroup, symbols: dict[str, Symbol]) -> int:
    return sum(symbols[s].end_line - symbols[s].start_line + 1 for s in group.symbol_ids)


def build_chunks(
    groups: dict[str, SCCGroup],
    topo_order: list[str],
    symbols: dict[str, Symbol],
    max_lines: int,
    strategies: dict[str, str] | None = None,
    target_extension: str | None = None,
) -> dict[str, Chunk]:
    """Pack consecutive groups (in topological order) into chunks of <= max_lines.

    Contiguous segments of a topological order keep the chunk graph acyclic. A group is
    never split: an oversized group (e.g. a big cycle) becomes its own chunk. Chunk
    ids are sequential in dispatch order; `group_id` is the first group in the chunk.
    With `target_extension`, target files mirror source paths with that extension.
    """
    strategies = strategies or {}
    chunks: dict[str, Chunk] = {}
    current: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal current, size
        if not current:
            return
        sym_ids = [s for gid in current for s in groups[gid].symbol_ids]
        cid = f"chunk-{len(chunks) + 1:03d}"
        chunks[cid] = Chunk(
            id=cid,
            group_id=current[0],
            symbol_ids=sym_ids,
            source_files=(files := sorted({symbols[s].file for s in sym_ids})),
            target_files=[Path(f).with_suffix(target_extension).as_posix() for f in files]
            if target_extension else [],
            strategy="\n".join(strategies[g] for g in current if g in strategies),
        )
        current, size = [], 0

    for gid in topo_order:
        lines = group_lines(groups[gid], symbols)
        if current and size + lines > max_lines:
            flush()
        current.append(gid)
        size += lines
    flush()
    return chunks


def chunk_dependencies(
    chunks: dict[str, Chunk], edges: dict[str, list[str]]
) -> dict[str, set[str]]:
    """chunk_id -> chunk_ids that must complete first."""
    owner = {s: cid for cid, c in chunks.items() for s in c.symbol_ids}
    deps: dict[str, set[str]] = {cid: set() for cid in chunks}
    for sid, callees in edges.items():
        for callee in callees:
            if owner[sid] != owner[callee]:
                deps[owner[sid]].add(owner[callee])
    return deps


def ready_chunks(
    deps: dict[str, set[str]], completed: set[str], excluded: set[str] = frozenset()
) -> list[str]:
    """Chunks whose dependencies are all complete, not yet done or excluded (blocked/failed)."""
    return sorted(
        cid for cid, d in deps.items()
        if cid not in completed and cid not in excluded and d <= completed
    )
