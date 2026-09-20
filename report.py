from __future__ import annotations
from typing import Any, Mapping


def chunk_status(state: Mapping[str, Any], chunk_id: str) -> str:
    for key, label in (("completed_chunk_ids", "completed"), ("blocked_chunk_ids", "blocked"),
                       ("failed_chunk_ids", "failed"), ("skipped_chunk_ids", "skipped")):
        if chunk_id in state.get(key, []):
            return label
    return "in progress" if state.get("current_chunk_id") == chunk_id else "pending"


def render_progress(state: Mapping[str, Any], thread_id: str) -> str:
    """Deterministic markdown snapshot of a run, built only from checkpointed state."""
    chunks = state.get("chunks", {})
    lines = [f"# Migration progress: {thread_id}", ""]
    if not chunks:
        lines.append("Planning has not produced chunks yet.")
    else:
        lines += ["| Chunk | Files | Status | Agent tries | Infra tries |", "|---|---|---|---|---|"]
        for cid, chunk in chunks.items():
            lines.append(
                f"| {cid} | {', '.join(chunk.source_files)} | {chunk_status(state, cid)} | "
                f"{state.get('agent_attempts', {}).get(cid, 0)} | "
                f"{state.get('infra_attempts', {}).get(cid, 0)} |")
    lines += ["", f"- final parity: {state.get('final_parity_passed')}",
              f"- e2e: {state.get('final_e2e_passed')}"]
    report = state.get("final_report")
    if report:
        lines += ["", "## Final report", "", report.summary, ""]
        lines += [f"- {n}" for n in report.cleanup_notes]
    return "\n".join(lines) + "\n"
