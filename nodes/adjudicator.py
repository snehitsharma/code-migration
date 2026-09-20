from __future__ import annotations
from typing import Any, Literal, Optional
from pydantic import BaseModel
from config import RetryCaps
from graph_algorithms.scc import group_dependencies
from llm import LLMClient
from nodes.common import AgentOutputError, ArtifactStore, call_structured
from nodes.planner import find_conflict_pairs
from schemas import AdjudicationOutcome, AdjudicationResult
from state import GraphState


class ConflictDecision(BaseModel):
    outcome: Literal["resolved", "inconclusive"]
    chosen_strategy: Optional[str] = None
    rationale: str


PROMPT = """Two neighbouring units of a {source} -> {target} migration were planned with
disagreeing views on whether they change a public interface.
Unit {gid}: {own}
Neighbours it conflicts with:
{others}
Decide one migration approach for {gid} that is safe for its neighbours.
Reply with JSON only: {{"outcome": "resolved"|"inconclusive", "chosen_strategy": "<approach, required if resolved>", "rationale": "..."}}
Use "inconclusive" if the evidence is not enough; do not guess."""


def adjudicator(
    state: GraphState, *, llm: LLMClient, store: ArtifactStore
) -> dict[str, Any]:
    """Phase 3: one decision per conflicting group; may explicitly be inconclusive."""
    strategies = state["strategies"]
    deps = group_dependencies(state["dependency_graph"], state["scc_groups"])
    pairs = find_conflict_pairs(strategies, deps)
    results = dict(state.get("adjudications", {}))
    for gid in state["conflicting_group_ids"]:
        if gid in results:
            continue
        neighbours = sorted({b if a == gid else a for a, b in pairs if gid in (a, b)})
        prompt = PROMPT.format(
            source=state["source_lang"], target=state["target_lang"], gid=gid,
            own=f"{strategies[gid].approach} (touches_public_interface="
                f"{strategies[gid].touches_public_interface})",
            others="\n".join(
                f"- {n}: {strategies[n].approach} (touches_public_interface="
                f"{strategies[n].touches_public_interface})" for n in neighbours),
        )
        d = call_structured(llm, prompt, ConflictDecision, store, f"adjudicator:{gid}")
        results[gid] = AdjudicationResult(
            target_id=gid, outcome=d.outcome, chosen_strategy=d.chosen_strategy,
            rationale=d.rationale)
    return {"adjudications": results}


Route = Literal["retry", "pass", "blocked"]

_ROUTES: dict[AdjudicationOutcome, Route] = {
    "fixed": "retry",             # a fix was applied: run the chunk's checks again
    "false_positive": "pass",     # evidence kept; the chunk is treated as passing
    "real_gap": "blocked",
    "inconclusive": "blocked",    # never promoted to success automatically
    "resolved": "retry",
}


def route_adjudication(outcome: AdjudicationOutcome) -> Route:
    """Phase 4 routing after retries are exhausted."""
    return _ROUTES[outcome]


class ChunkDecision(BaseModel):
    outcome: Literal["fixed", "false_positive", "real_gap", "inconclusive"]
    fix_path: Optional[str] = None
    rationale: str


CHUNK_PROMPT = """You are adjudicating a chunk of a {source} -> {target} migration that
kept failing after {attempts} attempts. Chunk {chunk_id}, files: {files}.
Latest evidence:
{evidence}
Classify: "fixed" (you can say what to change; put guidance in rationale), "false_positive"
(the check is wrong, code is fine), "real_gap" (behaviour genuinely differs), or "inconclusive".
Reply with JSON only: {{"outcome": "...", "fix_path": null, "rationale": "..."}}"""


def chunk_adjudicator(
    state: GraphState, *, llm: LLMClient, store: ArtifactStore, caps: RetryCaps
) -> dict[str, Any]:
    """Phase 4: called once per chunk after agent retries are exhausted. Invalid output is
    recorded as inconclusive, never as success. `fixed` grants exactly one more attempt."""
    c = state["current_chunk_id"]
    chunk = state["chunks"][c]
    b, v = state.get("last_build"), state.get("last_validation")
    i = state.get("integration_results", {}).get(c)
    evidence = (
        (b.stderr if b and not b.success else None)
        or (v.details if v and not v.passed else None)
        or (i.details if i and not i.passed else None)
        or "none recorded"
    )
    prompt = CHUNK_PROMPT.format(
        source=state["source_lang"], target=state["target_lang"], chunk_id=c,
        attempts=state.get("agent_attempts", {}).get(c, 0),
        files=", ".join(chunk.target_files), evidence=evidence)
    try:
        d = call_structured(llm, prompt, ChunkDecision, store, f"chunk_adjudicator:{c}")
        result = AdjudicationResult(target_id=c, outcome=d.outcome, fix_path=d.fix_path,
                                    rationale=d.rationale)
    except AgentOutputError as exc:
        result = AdjudicationResult(target_id=c, outcome="inconclusive", rationale=str(exc))
    update: dict[str, Any] = {
        "adjudications": {**state.get("adjudications", {}), c: result},
        "parity_adjudications": {**state.get("parity_adjudications", {}), c: result.outcome},
    }
    if route_adjudication(result.outcome) == "retry":
        update["agent_attempts"] = {**state.get("agent_attempts", {}), c: caps.agent_attempts - 1}
    return update
