from __future__ import annotations
import heapq
from schemas import DependencyGraph, SCCGroup


def find_sccs(graph: DependencyGraph) -> list[list[str]]:
    """Tarjan's algorithm (iterative). Members and components are sorted for determinism."""
    edges = graph.edges
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0

    for root in sorted(edges):
        if root in index:
            continue
        work = [(root, iter(sorted(edges.get(root, []))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(edges.get(nxt, [])))))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                result.append(sorted(comp))
    return sorted(result)


def contract_cycles(graph: DependencyGraph) -> dict[str, SCCGroup]:
    """One SCCGroup per component. Group id is stable: 'group::<smallest symbol id>'."""
    groups = {}
    for comp in find_sccs(graph):
        self_loop = len(comp) == 1 and comp[0] in graph.edges.get(comp[0], [])
        gid = f"group::{comp[0]}"
        groups[gid] = SCCGroup(id=gid, symbol_ids=comp, is_cycle=len(comp) > 1 or self_loop)
    return groups


def group_dependencies(
    graph: DependencyGraph, groups: dict[str, SCCGroup]
) -> dict[str, set[str]]:
    """group_id -> group_ids it depends on (callees), excluding itself."""
    owner = {sid: gid for gid, g in groups.items() for sid in g.symbol_ids}
    deps: dict[str, set[str]] = {gid: set() for gid in groups}
    for sid, callees in graph.edges.items():
        for callee in callees:
            if owner[sid] != owner[callee]:
                deps[owner[sid]].add(owner[callee])
    return deps


def topological_order(deps: dict[str, set[str]]) -> list[str]:
    """Dependencies first (Kahn's algorithm); ties broken by smallest id."""
    remaining = {gid: set(d) for gid, d in deps.items()}
    dependents: dict[str, set[str]] = {gid: set() for gid in deps}
    for gid, d in deps.items():
        for dep in d:
            dependents[dep].add(gid)
    ready = [gid for gid, d in remaining.items() if not d]
    heapq.heapify(ready)
    order = []
    while ready:
        gid = heapq.heappop(ready)
        order.append(gid)
        for nxt in dependents[gid]:
            remaining[nxt].discard(gid)
            if not remaining[nxt]:
                heapq.heappush(ready, nxt)
    if len(order) != len(deps):
        raise ValueError("cycle in group dependencies; contract SCCs first")
    return order
