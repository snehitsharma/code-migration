from __future__ import annotations
from pathlib import Path, PurePosixPath
from typing import Any
from pydantic import BaseModel, Field
from config import Config
from llm import LLMClient
from nodes.common import AgentOutputError, ArtifactStore, call_structured
from schemas import BuildResult, Chunk
from state import GraphState


class FileOutput(BaseModel):
    path: str = Field(min_length=1)
    content: str


class MigrationOutput(BaseModel):
    files: list[FileOutput] = Field(min_length=1)
    notes: str = ""


class ScopeViolation(Exception):
    """The migrator tried to write outside the current chunk's allowed files."""


PROMPT = """You are porting one chunk of a {source} codebase to {target}.
Chunk {chunk_id}. Source: {sources}
Symbols (read them in the source files):
{symbols}
Strategy: {strategy}
Allowed files: {allowed}
Existing target files must keep their other content; return the full new content of each file.
Also provide the parity tests (the *.test.* files) that exercise the migrated functions.
{feedback}Reply with JSON only: {{"files": [{{"path": "...", "content": "..."}}], "notes": "..."}}"""


def allowed_paths(chunk: Chunk, config: Config) -> list[str]:
    pair = config.language_pair
    return sorted({*chunk.target_files, *(pair.test_file_for(f) for f in chunk.target_files)})


def check_scope(out: MigrationOutput, allowed: list[str]) -> None:
    for f in out.files:
        p = PurePosixPath(f.path.replace("\\", "/"))
        if p.is_absolute() or ".." in p.parts or p.as_posix() not in allowed:
            raise ScopeViolation(f"{f.path!r} is outside this chunk's allowed files {allowed}")


def check_prefix(out: MigrationOutput, prefix: str) -> None:
    for f in out.files:
        p = PurePosixPath(f.path.replace("\\", "/"))
        if p.is_absolute() or ".." in p.parts or not p.as_posix().startswith(prefix):
            raise ScopeViolation(f"{f.path!r} is outside {prefix!r}")


def write_files(root: Path, out: MigrationOutput) -> None:
    for f in out.files:
        dest = root / PurePosixPath(f.path.replace("\\", "/"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f.content, encoding="utf-8")


def _feedback(state: GraphState, c: str) -> str:
    parts = []
    if (b := state.get("last_build")) and not b.success:
        parts.append(f"Previous build failed ({b.error_type}):\n{b.stderr}")
    if (v := state.get("last_validation")) and not v.passed:
        parts.append(f"Previous parity check failed:\n{v.details}")
    if (i := state.get("integration_results", {}).get(c)) and not i.passed:
        parts.append(f"Previous integration check failed:\n{i.details}")
    if a := state.get("adjudications", {}).get(c):
        parts.append(f"Adjudicator guidance ({a.outcome}): {a.rationale}")
    return ("\n".join(parts) + "\n") if parts else ""


def code_migrator(
    state: GraphState, *, llm: LLMClient, store: ArtifactStore, config: Config
) -> dict[str, Any]:
    """Migrate exactly the current chunk. Output is schema-validated and scope-checked
    before anything is written; a rejected output counts as a failed agent attempt."""
    c = state["current_chunk_id"]
    chunk, symbols = state["chunks"][c], state["symbols"]
    allowed = allowed_paths(chunk, config)
    prompt = PROMPT.format(
        source=state["source_lang"], target=state["target_lang"], chunk_id=c,
        sources=", ".join(chunk.source_files), strategy=chunk.strategy or "port directly",
        symbols="\n".join(
            f"- {s} [{symbols[s].file}:{symbols[s].start_line}-{symbols[s].end_line}] "
            f"{symbols[s].signature}" for s in chunk.symbol_ids),
        allowed=", ".join(allowed), feedback=_feedback(state, c),
    )
    try:
        out = call_structured(llm, prompt, MigrationOutput, store, f"code_migrator:{c}")
        check_scope(out, allowed)
    except (AgentOutputError, ScopeViolation) as exc:
        store.record(f"code_migrator:{c}", "rejected", {"error": str(exc)})
        counters = dict(state.get("agent_attempts", {}))
        counters[c] = counters.get(c, 0) + 1
        return {
            "last_build": BuildResult(chunk_id=c, success=False, error_type="other",
                                      stderr=f"migrator output rejected: {exc}"[:2000]),
            "last_validation": None, "agent_attempts": counters,
        }
    write_files(Path(state["target_path"]), out)
    return {"last_build": None, "last_validation": None}
