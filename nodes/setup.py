from __future__ import annotations
from pathlib import Path
from typing import Any
from config import Config
from graph_algorithms.dependencies import build_dependency_graph
from graph_algorithms.scc import contract_cycles, group_dependencies, topological_order
from kb.index import SymbolIndex
from parser.base import LanguageParser
from state import GraphState


def parse_index(
    state: GraphState, *, parser: LanguageParser, index: SymbolIndex, config: Config
) -> dict[str, Any]:
    """Phase 0: parse the source repo and persist symbols/edges. Parser gaps are kept as
    warnings so the final report can mention them."""
    result = parser.parse_repo(Path(state["repo_path"]), config.exclude)
    index.write(result.symbols)
    return {"symbols": result.symbols, "warnings": result.warnings}


def derive_graph(state: GraphState) -> dict[str, Any]:
    """Phase 1: dependency graph, SCC contraction, dependency-safe order. No LLM."""
    graph = build_dependency_graph(state["symbols"])
    groups = contract_cycles(graph)
    order = topological_order(group_dependencies(graph, groups))
    return {"dependency_graph": graph, "scc_groups": groups, "topo_order": order}
