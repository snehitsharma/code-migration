from __future__ import annotations
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field
from config import Config, RetryCaps
from llm import LLMClient
from nodes.code_migrator import (
    MigrationOutput, ScopeViolation, allowed_paths, check_prefix, check_scope, write_files,
)
from nodes.common import AgentOutputError, ArtifactStore, call_structured
from runner import CommandRunner
from schemas import FinalReport
from state import GraphState

DOC_FILE = "MIGRATION.md"
E2E_DIR = "e2e/"


def final_parity(state: GraphState, *, runner: CommandRunner, config: Config) -> dict[str, Any]:
    """Phase 5: repository-wide build + full test suite over everything migrated."""
    root, pair = Path(state["target_path"]), config.language_pair
    for cmd in (pair.build_command, pair.test_command):
        res = runner.run(cmd, root)
        if not res.ok:
            return {"final_parity_passed": False,
                    "final_parity_details": (res.stderr or res.stdout or "failed")[-2000:]}
    return {"final_parity_passed": True, "final_parity_details": None}


def route_after_final_parity(state: GraphState, caps: RetryCaps) -> str:
    if state["final_parity_passed"]:
        return "ok"
    return "fix" if state.get("final_parity_loop_count", 0) < caps.max_final_parity_loops else "report"


FIX_PROMPT = """You are fixing a repository-wide check that failed after every chunk of a
{source} -> {target} migration was completed individually.
Evidence:
{evidence}
Allowed files: {allowed}
Reply with JSON only: {{"files": [{{"path": "...", "content": "..."}}], "notes": "..."}}"""


def parity_fixer(
    state: GraphState, *, llm: LLMClient, store: ArtifactStore, config: Config
) -> dict[str, Any]:
    """Loopback for a failed final parity check. Every call consumes one loop, even if the
    agent's output is rejected, so the cap always bounds the run."""
    allowed = sorted({p for c in state["chunks"].values() for p in allowed_paths(c, config)})
    prompt = FIX_PROMPT.format(
        source=state["source_lang"], target=state["target_lang"],
        evidence=state.get("final_parity_details") or "none recorded", allowed=", ".join(allowed))
    count = state.get("final_parity_loop_count", 0) + 1
    try:
        out = call_structured(llm, prompt, MigrationOutput, store, f"parity_fixer:{count}")
        check_scope(out, allowed)
        write_files(Path(state["target_path"]), out)
    except (AgentOutputError, ScopeViolation) as exc:
        store.record(f"parity_fixer:{count}", "rejected", {"error": str(exc)})
    return {"final_parity_loop_count": count}


E2E_PROMPT = """You are writing end-to-end tests for a repository migrated from {source} to {target}.
Symbols: {symbols}
Write test files under {dir} that exercise the migrated public functions together.
Reply with JSON only: {{"files": [{{"path": "{dir}<name>.test.<ext>", "content": "..."}}], "notes": "..."}}"""


def e2e_test_crafter(
    state: GraphState, *, llm: LLMClient, store: ArtifactStore, runner: CommandRunner,
    config: Config,
) -> dict[str, Any]:
    """Phase 6a: agent-written e2e tests (confined to e2e/), then a full deterministic run."""
    root = Path(state["target_path"])
    prompt = E2E_PROMPT.format(
        source=state["source_lang"], target=state["target_lang"], dir=E2E_DIR,
        symbols=", ".join(sorted(state["symbols"])))
    try:
        out = call_structured(llm, prompt, MigrationOutput, store, "e2e_test_crafter")
        check_prefix(out, E2E_DIR)
    except (AgentOutputError, ScopeViolation) as exc:
        store.record("e2e_test_crafter", "rejected", {"error": str(exc)})
        return {"final_e2e_passed": False}
    write_files(root, out)
    return {"final_e2e_passed": runner.run(config.language_pair.test_command, root).ok}


class DocOutput(BaseModel):
    markdown: str = Field(min_length=1)


DOC_PROMPT = """You are writing migration documentation for a repository ported from {source} to {target}.
Migrated chunks: {chunks}. Blocked: {blocked}. Summaries:
{summaries}
Reply with JSON only: {{"markdown": "<the contents of {file}>"}}"""


def documentation_writer(
    state: GraphState, *, llm: LLMClient, store: ArtifactStore
) -> dict[str, Any]:
    """Phase 6b: runs in parallel with e2e; writes only its own state key."""
    prompt = DOC_PROMPT.format(
        source=state["source_lang"], target=state["target_lang"], file=DOC_FILE,
        chunks=", ".join(state.get("completed_chunk_ids", [])) or "none",
        blocked=", ".join(state.get("blocked_chunk_ids", [])) or "none",
        summaries="\n".join(f"- {k}: {v}" for k, v in sorted(state.get("summaries", {}).items())))
    try:
        doc = call_structured(llm, prompt, DocOutput, store, "documentation_writer").markdown
    except AgentOutputError:
        return {"documentation": None}
    (Path(state["target_path"])).mkdir(parents=True, exist_ok=True)
    (Path(state["target_path"]) / DOC_FILE).write_text(doc, encoding="utf-8")
    return {"documentation": doc}


def finalize_report(state: GraphState, *, store: ArtifactStore, caps: RetryCaps) -> dict[str, Any]:
    """Phase 8: deterministic report. Says plainly what did not work."""
    done = state.get("completed_chunk_ids", [])
    blocked = state.get("blocked_chunk_ids", [])
    failed = state.get("failed_chunk_ids", [])
    skipped = state.get("skipped_chunk_ids", [])
    notes: list[str] = []
    halted = bool(failed) or len(blocked) >= caps.max_blocked_chunks
    if failed:
        notes.append(f"run halted: hard failure in {failed}")
    elif halted:
        notes.append(f"run halted: {len(blocked)} chunks blocked (cap {caps.max_blocked_chunks})")
    if skipped:
        notes.append(f"skipped (depend on blocked/failed chunks): {skipped}")
    if not halted and state.get("final_parity_passed") is False:
        notes.append(f"final parity failed after {state.get('final_parity_loop_count', 0)} "
                     f"fix loops: {state.get('final_parity_details')}")
    if state.get("final_parity_passed") and state.get("final_e2e_passed") is False:
        notes.append("e2e tests did not pass")
    if state.get("final_parity_passed") and not state.get("documentation"):
        notes.append("documentation was not produced")
    notes += [f"parser: {w}" for w in state.get("warnings", [])]
    total = len(state.get("chunks", {}))
    report = FinalReport(
        summary=(f"{len(done)}/{total} chunks migrated; {len(blocked)} blocked, "
                 f"{len(failed)} failed, {len(skipped)} skipped; final parity: "
                 f"{state.get('final_parity_passed')}; e2e: {state.get('final_e2e_passed')}"),
        chunks_completed=list(done), chunks_blocked=list(blocked), chunks_failed=list(failed),
        cleanup_notes=notes, documentation_path=DOC_FILE if state.get("documentation") else None,
    )
    store.record("final_report", "report", report.model_dump())
    return {"final_report": report}
