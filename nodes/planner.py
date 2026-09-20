from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field
from graph_algorithms.chunking import build_chunks, chunk_dependencies, ready_chunks
from graph_algorithms.scc import group_dependencies
from llm import LLMClient
from nodes.common import ArtifactStore, PlanningError, call_structured
from schemas import PlannerStrategy
from state import GraphState


class StrategyDraft(BaseModel):
    description: str = Field(min_length=1)
    approach: str = Field(min_length=1)
    touches_public_interface: bool = False


PROMPT = """You are planning the migration of one unit of a {source} codebase to {target}.
Unit: {group_id} ({kind}); symbols:
{symbols}
Strategies already chosen for the units it depends on:
{dep_strategies}
Reply with JSON only: {{"description": "...", "approach": "...", "touches_public_interface": true|false}}
Set touches_public_interface to true if the migration changes any signature other code relies on."""


def find_conflict_pairs(
    strategies: dict[str, PlannerStrategy], deps: dict[str, set[str]]
) -> list[tuple[str, str]]:
    """A conflict is a caller/callee pair of groups whose strategies disagree on
    `touches_public_interface`: one side changes an interface the other relies on."""
    pairs = {
        tuple(sorted((g, d)))
        for g, ds in deps.items() for d in ds
        if g in strategies and d in strategies
        and strategies[g].touches_public_interface != strategies[d].touches_public_interface
    }
    return sorted(pairs)


def planner(state: GraphState, *, llm: LLMClient, store: ArtifactStore) -> dict[str, Any]:
    """One strategy per group, in dependency order so callees are planned first."""
    graph, groups, symbols = state["dependency_graph"], state["scc_groups"], state["symbols"]
    deps = group_dependencies(graph, groups)
    strategies = dict(state.get("strategies", {}))
    for gid in state["topo_order"]:
        if gid in strategies:
            continue
        group = groups[gid]
        prompt = PROMPT.format(
            source=state["source_lang"], target=state["target_lang"], group_id=gid,
            kind="cycle, migrate atomically" if group.is_cycle else "single function",
            symbols="\n".join(
                f"- {s} [{symbols[s].file}:{symbols[s].start_line}-{symbols[s].end_line}] "
                f"{symbols[s].signature}: {state['summaries'].get(s, 'no summary')}"
                for s in group.symbol_ids),
            dep_strategies="\n".join(
                f"- {d}: {strategies[d].approach}" for d in sorted(deps[gid]) if d in strategies
            ) or "none",
        )
        draft = call_structured(llm, prompt, StrategyDraft, store, f"planner:{gid}")
        strategies[gid] = PlannerStrategy(group_id=gid, **draft.model_dump())
    pairs = find_conflict_pairs(strategies, deps)
    return {
        "strategies": strategies,
        "conflicting_group_ids": sorted({g for pair in pairs for g in pair}),
    }


def finalize_plan(
    state: GraphState, *, max_lines: int, target_extension: str | None = None
) -> dict[str, Any]:
    """Turn approved strategies into immutable chunks. Refuses to plan while any
    conflict lacks a 'resolved' adjudication: inconclusive never becomes approval."""
    adjudications = state.get("adjudications", {})
    unresolved = [
        g for g in state["conflicting_group_ids"]
        if g not in adjudications or adjudications[g].outcome != "resolved"
    ]
    if unresolved:
        raise PlanningError(f"unresolved strategy conflicts: {unresolved}")
    approved = {
        gid: (adjudications[gid].chosen_strategy if gid in adjudications else s.approach)
        for gid, s in state["strategies"].items()
    }
    chunks = build_chunks(
        state["scc_groups"], state["topo_order"], state["symbols"], max_lines, approved,
        target_extension)
    deps = chunk_dependencies(chunks, state["dependency_graph"].edges)
    return {"chunks": chunks, "chunk_queue": ready_chunks(deps, set())}
