from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any, TypeVar
from pydantic import BaseModel, ValidationError
from llm import LLMClient

T = TypeVar("T", bound=BaseModel)


class AgentOutputError(Exception):
    """An agent's reply never matched the required schema."""

    def __init__(self, name: str, detail: str) -> None:
        super().__init__(f"{name}: invalid agent output after retries: {detail}")
        self.name, self.detail = name, detail


class PlanningError(Exception):
    """The plan cannot be completed (e.g. an unresolved strategy conflict)."""


class ArtifactStore:
    """Records every agent call (prompt, raw reply, parsed output or validation failure).
    Kept in memory and, when `directory` is set, written as numbered JSON files."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory
        self.records: list[dict[str, Any]] = []
        if directory:
            directory.mkdir(parents=True, exist_ok=True)

    def record(self, name: str, kind: str, payload: dict[str, Any]) -> None:
        entry = {"seq": len(self.records) + 1, "name": name, "kind": kind, **payload}
        self.records.append(entry)
        if self.directory:
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
            path = self.directory / f"{entry['seq']:04d}_{kind}_{safe}.json"
            path.write_text(json.dumps(entry, indent=2), encoding="utf-8")

    def failures(self) -> list[dict[str, Any]]:
        return [r for r in self.records if r["kind"] == "validation_failure"]


def _strip_fences(raw: str) -> str:
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    return (m.group(1) if m else raw).strip()


def call_structured(
    llm: LLMClient, prompt: str, model: type[T], store: ArtifactStore, name: str,
    max_attempts: int = 2,
) -> T:
    """Call the agent and validate the reply. Invalid replies are recorded, then retried
    with the error appended; after `max_attempts` an AgentOutputError is raised."""
    error = ""
    for attempt in range(1, max_attempts + 1):
        p = prompt if not error else (
            f"{prompt}\n\nYour previous reply was rejected: {error}\nReply with valid JSON only."
        )
        raw = llm.complete(p)
        try:
            out = model.model_validate_json(_strip_fences(raw))
        except ValidationError as exc:
            error = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            store.record(name, "validation_failure",
                         {"attempt": attempt, "prompt": p, "raw": raw, "error": error})
            continue
        store.record(name, "call", {"attempt": attempt, "prompt": p, "raw": raw,
                                    "output": out.model_dump()})
        return out
    raise AgentOutputError(name, error)
