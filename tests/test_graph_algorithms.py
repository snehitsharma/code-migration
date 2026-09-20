import random

import pytest

from graph_algorithms.chunking import build_chunks, chunk_dependencies, ready_chunks
from graph_algorithms.dependencies import build_dependency_graph, unresolved_calls
from graph_algorithms.scc import (
    contract_cycles, find_sccs, group_dependencies, topological_order,
)
from schemas import Symbol


def make(calls_by_name: dict[str, list[str]], lines: int = 10) -> dict[str, Symbol]:
    return {
        n: Symbol(id=n, file=f"{n}.py", name=n, kind="function", signature=f"def {n}()",
                  start_line=1, end_line=lines, calls=c)
        for n, c in calls_by_name.items()
    }


def pipeline(symbols, max_lines=100):
    graph = build_dependency_graph(symbols)
    groups = contract_cycles(graph)
    deps = group_dependencies(graph, groups)
    order = topological_order(deps)
    chunks = build_chunks(groups, order, symbols, max_lines)
    return graph, groups, order, chunks


def test_unresolved_calls_are_dropped_from_graph_but_reported():
    syms = make({"a": ["b", "print"], "b": []})
    assert build_dependency_graph(syms).edges == {"a": ["b"], "b": []}
    assert unresolved_calls(syms) == {"a": ["print"]}


def test_linear_chain_orders_callees_first():
    _, _, order, _ = pipeline(make({"a": ["b"], "b": ["c"], "c": []}))
    assert order == ["group::c", "group::b", "group::a"]


def test_branching_and_disconnected_use_stable_tie_break():
    syms = make({"a": ["b", "c"], "b": [], "c": [], "z": []})
    _, _, order, _ = pipeline(syms)
    assert order == ["group::b", "group::c", "group::a", "group::z"]


def test_cycle_contracted_into_one_group():
    syms = make({"a": ["b"], "b": ["c"], "c": ["a"], "d": ["a"]})
    _, groups, order, _ = pipeline(syms)
    cyc = groups["group::a"]
    assert cyc.symbol_ids == ["a", "b", "c"] and cyc.is_cycle
    assert order == ["group::a", "group::d"]


def test_self_recursion_is_a_cycle():
    _, groups, _, _ = pipeline(make({"a": ["a"], "b": []}))
    assert groups["group::a"].is_cycle and not groups["group::b"].is_cycle


def test_input_ordering_does_not_change_results():
    base = {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": [], "e": ["f"], "f": ["e"]}
    ref = pipeline(make(base))
    for seed in range(5):
        items = list(base.items())
        random.Random(seed).shuffle(items)
        got = pipeline(make(dict(items)))
        assert got[2] == ref[2]
        assert {k: v.model_dump() for k, v in got[3].items()} == \
               {k: v.model_dump() for k, v in ref[3].items()}


def test_every_symbol_in_exactly_one_chunk():
    syms = make({"a": ["b"], "b": ["c"], "c": [], "d": [], "e": ["f"], "f": ["e"]})
    _, _, _, chunks = pipeline(syms, max_lines=25)
    all_syms = [s for c in chunks.values() for s in c.symbol_ids]
    assert sorted(all_syms) == sorted(syms)


def test_chunks_respect_line_budget_and_oversized_group_stays_atomic():
    syms = make({"a": ["b"], "b": ["a"], "c": [], "d": []}, lines=10)
    _, _, _, chunks = pipeline(syms, max_lines=15)
    sizes = [len(c.symbol_ids) for c in chunks.values()]
    assert 2 in sizes  # the 20-line cycle exceeds budget but is not split
    for c in chunks.values():
        if len(c.symbol_ids) > 1:
            assert set(c.symbol_ids) == {"a", "b"}


def test_chunk_dependencies_are_acyclic_and_drive_dispatch():
    syms = make({"a": ["b"], "b": ["c"], "c": []})
    graph, _, _, chunks = pipeline(syms, max_lines=10)  # one symbol per chunk
    deps = chunk_dependencies(chunks, graph.edges)
    assert ready_chunks(deps, set()) == ["chunk-001"]
    assert ready_chunks(deps, {"chunk-001"}) == ["chunk-002"]
    assert ready_chunks(deps, {"chunk-001"}, excluded={"chunk-002"}) == []


def test_topological_order_rejects_uncontracted_cycle():
    with pytest.raises(ValueError):
        topological_order({"x": {"y"}, "y": {"x"}})


def test_find_sccs_handles_deep_chain_without_recursion_error():
    n = 3000
    graph = build_dependency_graph(
        make({f"f{i:05d}": ([f"f{i + 1:05d}"] if i < n - 1 else []) for i in range(n)})
    )
    assert len(find_sccs(graph)) == n
