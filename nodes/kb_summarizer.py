from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field
from llm import LLMClient
from nodes.common import ArtifactStore, call_structured
from state import GraphState


class SymbolSummary(BaseModel):
    summary: str = Field(min_length=1)


PROMPT = """You are documenting a {source} codebase before it is migrated to {target}.
Read lines {start}-{end} of {path} (function `{name}`, signature: {signature}).
Callees already known: {callees}.
Reply with JSON only: {{"summary": "<one or two sentences: what it does, inputs, outputs, side effects>"}}"""


def kb_summarizer(
    state: GraphState, *, llm: LLMClient, store: ArtifactStore
) -> dict[str, Any]:
    """One structured summary per symbol. Already-summarized symbols are skipped so a
    resumed run does not repeat agent calls. The prompt carries paths, not file contents."""
    summaries = dict(state.get("summaries", {}))
    for sid, sym in sorted(state["symbols"].items()):
        if sid in summaries:
            continue
        prompt = PROMPT.format(
            source=state["source_lang"], target=state["target_lang"], start=sym.start_line,
            end=sym.end_line, path=sym.file, name=sym.name, signature=sym.signature,
            callees=", ".join(c for c in sym.calls) or "none",
        )
        summaries[sid] = call_structured(
            llm, prompt, SymbolSummary, store, f"kb_summarizer:{sid}").summary
    return {"summaries": summaries}
