from __future__ import annotations
import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Callable
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from config import Config
from kb.index import SymbolIndex
from llm import LLMClient
from nodes import routing
from nodes.adjudicator import adjudicator, chunk_adjudicator
from nodes.build import build
from nodes.code_migrator import code_migrator
from nodes.common import ArtifactStore
from nodes.final import (
    documentation_writer, e2e_test_crafter, final_parity, finalize_report, parity_fixer,
    route_after_final_parity,
)
from nodes.integration import integration
from nodes.kb_summarizer import kb_summarizer
from nodes.parity import parity
from nodes.planner import finalize_plan, planner
from nodes.setup import derive_graph, parse_index
from parser.base import LanguageParser
from runner import CommandRunner
from state import GraphState

# routing step -> graph node name
_STEP_NODE = {
    "migrate": "code_migrator", "build": "build", "parity": "parity",
    "integration": "integration", "adjudicate": "chunk_adjudicator",
    "complete": "complete_chunk", "block": "block_chunk", "fail": "fail_chunk",
}


def initial_state(config: Config) -> GraphState:
    return {
        "repo_path": str(config.repo_path), "target_path": str(config.target_path),
        "source_lang": config.language_pair.source, "target_lang": config.language_pair.target,
        "warnings": [], "symbols": {}, "docs": {}, "summaries": {}, "dependency_graph": None,
        "scc_groups": {}, "topo_order": [], "strategies": {}, "conflicting_group_ids": [],
        "adjudications": {}, "chunks": {}, "chunk_queue": [], "completed_chunk_ids": [],
        "current_chunk_id": None, "agent_attempts": {}, "infra_attempts": {},
        "last_build": None, "last_validation": None, "parity_adjudications": {},
        "integration_results": {}, "blocked_chunk_ids": [], "skipped_chunk_ids": [],
        "failed_chunk_ids": [], "final_parity_passed": None, "final_parity_details": None,
        "final_parity_loop_count": 0, "final_e2e_passed": None, "documentation": None,
        "final_report": None, "current_phase": 0,
    }


def thread_id_for(config: Config) -> str:
    """Stable per (source repo, language pair): re-running the same migration resumes it."""
    repo = Path(config.repo_path).resolve()
    key = f"{repo.as_posix()}|{config.language_pair.source}->{config.language_pair.target}"
    return f"migration-{repo.name}-{hashlib.sha1(key.encode()).hexdigest()[:10]}"


def run_config(config: Config) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id_for(config)}, "recursion_limit": 1000}


def open_checkpointer(path: str | Path) -> SqliteSaver:
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))


def build_migration_graph(
    *, config: Config, llm: LLMClient, runner: CommandRunner, parser: LanguageParser,
    index: SymbolIndex, store: ArtifactStore, checkpointer: SqliteSaver | None = None,
):
    caps = config.caps
    target_ext = config.language_pair.target_extensions[0]
    g = StateGraph(GraphState)

    def node(name: str, fn: Callable[..., dict[str, Any]], **deps: Any) -> None:
        g.add_node(name, lambda state: fn(state, **deps))

    # Phases 0-3: index, graph, knowledge base, planning
    node("parse_index", parse_index, parser=parser, index=index, config=config)
    node("derive_graph", derive_graph)
    node("kb_summarizer", kb_summarizer, llm=llm, store=store)
    node("planner", planner, llm=llm, store=store)
    node("adjudicator", adjudicator, llm=llm, store=store)
    node("finalize_plan", finalize_plan, max_lines=config.max_lines_per_chunk,
         target_extension=target_ext)
    g.add_edge(START, "parse_index")
    g.add_edge("parse_index", "derive_graph")
    g.add_edge("parse_index", "kb_summarizer")
    g.add_edge(["derive_graph", "kb_summarizer"], "planner")
    g.add_edge("planner", "adjudicator")
    g.add_edge("adjudicator", "finalize_plan")
    g.add_edge("finalize_plan", "dispatch")

    # Phase 4: one chunk at a time
    node("dispatch", routing.dispatch, caps=caps)
    node("code_migrator", code_migrator, llm=llm, store=store, config=config)
    node("build", build, runner=runner, config=config)
    node("parity", parity, runner=runner, config=config)
    node("integration", integration, runner=runner, config=config)
    node("chunk_adjudicator", chunk_adjudicator, llm=llm, store=store, caps=caps)
    for name, status in (("complete_chunk", "completed"), ("block_chunk", "blocked"),
                         ("fail_chunk", "failed")):
        g.add_node(name, lambda state, s=status: routing.finish_chunk(state, s))
        g.add_edge(name, "dispatch")

    def branch(source: str, router: Callable[..., str], extra: dict[str, str] | None = None):
        targets = {**_STEP_NODE, **(extra or {})}
        g.add_conditional_edges(source, lambda s: router(s, caps), targets)

    branch("dispatch", routing.route_dispatch,
           {"finalize": "final_parity", "halt": "finalize_report"})
    branch("code_migrator", routing.route_after_migrate)
    branch("build", routing.route_after_build)
    branch("parity", routing.route_after_parity)
    branch("integration", routing.route_after_integration)
    branch("chunk_adjudicator", routing.route_after_adjudication)

    # Phases 5-8: repo-wide parity (capped loopback), e2e + docs in parallel, report
    node("final_parity", final_parity, runner=runner, config=config)
    node("parity_fixer", parity_fixer, llm=llm, store=store, config=config)
    node("e2e_test_crafter", e2e_test_crafter, llm=llm, store=store, runner=runner, config=config)
    node("documentation_writer", documentation_writer, llm=llm, store=store)
    node("finalize_report", finalize_report, store=store, caps=caps)

    def after_final_parity(state: GraphState) -> str | list[str]:
        route = route_after_final_parity(state, caps)
        if route == "ok":  # fan out: e2e and docs run in parallel
            return ["e2e_test_crafter", "documentation_writer"]
        return "parity_fixer" if route == "fix" else "finalize_report"

    g.add_conditional_edges("final_parity", after_final_parity)
    g.add_edge("parity_fixer", "final_parity")
    g.add_edge(["e2e_test_crafter", "documentation_writer"], "finalize_report")
    g.add_edge("finalize_report", END)
    return g.compile(checkpointer=checkpointer)
