"""Pure control flow for the single-chunk migration loop (no LLM, no I/O)."""
from __future__ import annotations
from typing import Any, Literal
from config import RetryCaps
from graph_algorithms.chunking import chunk_dependencies, ready_chunks
from nodes.adjudicator import route_adjudication
from state import GraphState

Step = Literal[
    "dispatch", "migrate", "build", "parity", "integration", "adjudicate",
    "complete", "block", "fail", "halt", "finalize",
]


def _agent_failure(state: GraphState, caps: RetryCaps) -> Step:
    c = state["current_chunk_id"]
    if state.get("agent_attempts", {}).get(c, 0) < caps.agent_attempts:
        return "migrate"
    # Retries exhausted: adjudicate once; a second exhaustion is final.
    return "block" if c in state.get("parity_adjudications", {}) else "adjudicate"


def route_after_build(state: GraphState, caps: RetryCaps) -> Step:
    b, c = state["last_build"], state["current_chunk_id"]
    if b.success:
        return "parity"
    if b.error_type == "infra":  # separate counter; retry the build, not the migrator
        exhausted = state.get("infra_attempts", {}).get(c, 0) >= caps.infra_attempts
        return "fail" if exhausted else "build"
    return _agent_failure(state, caps)


def route_after_migrate(state: GraphState, caps: RetryCaps) -> Step:
    b = state.get("last_build")
    if b is not None and not b.success:  # migrator output was rejected
        return route_after_build(state, caps)
    return "build"


def route_after_parity(state: GraphState, caps: RetryCaps) -> Step:
    return "integration" if state["last_validation"].passed else _agent_failure(state, caps)


def route_after_integration(state: GraphState, caps: RetryCaps) -> Step:
    c = state["current_chunk_id"]
    return "complete" if state["integration_results"][c].passed else _agent_failure(state, caps)


def route_after_adjudication(state: GraphState, caps: RetryCaps) -> Step:
    c = state["current_chunk_id"]
    route = route_adjudication(state["parity_adjudications"][c])
    if route == "blocked":
        return "block"
    if route == "pass":  # false positive only makes sense for parity, never a failed build
        b = state.get("last_build")
        return "block" if b is not None and not b.success else "integration"
    return "migrate"


def finish_chunk(
    state: GraphState, status: Literal["completed", "blocked", "failed"]
) -> dict[str, Any]:
    key = {"completed": "completed_chunk_ids", "blocked": "blocked_chunk_ids",
           "failed": "failed_chunk_ids"}[status]
    return {key: [*state.get(key, []), state["current_chunk_id"]], "current_chunk_id": None}


def dispatch(state: GraphState, caps: RetryCaps) -> dict[str, Any]:
    """Pick the next dependency-ready chunk; skip anything downstream of a blocked or
    failed chunk (it can never become ready)."""
    deps = chunk_dependencies(state["chunks"], state["dependency_graph"].edges)
    completed = set(state.get("completed_chunk_ids", []))
    stuck = set(state.get("blocked_chunk_ids", [])) | set(state.get("failed_chunk_ids", []))
    skipped = set(state.get("skipped_chunk_ids", []))
    changed = True
    while changed:
        changed = False
        for cid, d in sorted(deps.items()):
            if cid not in completed | stuck | skipped and d & (stuck | skipped):
                skipped.add(cid)
                changed = True
    queue = ready_chunks(deps, completed, excluded=stuck | skipped)
    return {
        "skipped_chunk_ids": sorted(skipped), "chunk_queue": queue,
        "current_chunk_id": queue[0] if queue else None,
        "last_build": None, "last_validation": None,
    }


def route_dispatch(state: GraphState, caps: RetryCaps) -> Step:
    too_many_blocked = len(state.get("blocked_chunk_ids", [])) >= caps.max_blocked_chunks
    if state.get("failed_chunk_ids") or too_many_blocked:
        return "halt"
    return "migrate" if state["current_chunk_id"] else "finalize"
