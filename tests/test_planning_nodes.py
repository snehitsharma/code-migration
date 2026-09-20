import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from graph_algorithms.dependencies import build_dependency_graph
from graph_algorithms.scc import contract_cycles, group_dependencies, topological_order
from llm import FakeLLM
from nodes.adjudicator import adjudicator, route_adjudication
from nodes.common import AgentOutputError, ArtifactStore, PlanningError
from nodes.kb_summarizer import kb_summarizer
from nodes.planner import find_conflict_pairs, finalize_plan, planner
from parser.implementation import PythonParser
from schemas import AdjudicationResult

FIXTURE = Path(__file__).parent / "fixtures" / "demo_repo"
INTERFACE_CHANGER = "group::utils.py::helper"


def initial_state():
    symbols = PythonParser().parse_repo(FIXTURE, ["legacy/*"]).symbols
    graph = build_dependency_graph(symbols)
    groups = contract_cycles(graph)
    order = topological_order(group_dependencies(graph, groups))
    return {
        "repo_path": str(FIXTURE), "source_lang": "python", "target_lang": "typescript",
        "symbols": symbols, "summaries": {}, "dependency_graph": graph,
        "scc_groups": groups, "topo_order": order, "strategies": {},
        "conflicting_group_ids": [], "adjudications": {},
    }


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.as_posix().encode() + p.read_bytes())
    return h.hexdigest()


def fake_agents(interface_changer=INTERFACE_CHANGER, adjudication="resolved"):
    def reply(prompt: str) -> str:
        if "documenting" in prompt:
            return '{"summary": "does a thing"}'
        if "planning the migration" in prompt:
            touches = f"Unit: {interface_changer} " in prompt
            return json.dumps({"description": "d", "approach": "port directly",
                               "touches_public_interface": touches})
        if adjudication == "resolved":
            return '{"outcome": "resolved", "chosen_strategy": "keep old signature", "rationale": "r"}'
        return '{"outcome": "inconclusive", "rationale": "unclear"}'
    return FakeLLM(reply)


def run_planning(llm, store, state=None):
    state = state or initial_state()
    state.update(kb_summarizer(state, llm=llm, store=store))
    state.update(planner(state, llm=llm, store=store))
    state.update(adjudicator(state, llm=llm, store=store))
    return state


def test_summarizer_covers_all_symbols_and_skips_existing():
    state, store = initial_state(), ArtifactStore()
    llm = fake_agents()
    state["summaries"] = {"utils.py::helper": "already known"}
    out = kb_summarizer(state, llm=llm, store=store)["summaries"]
    assert set(out) == set(state["symbols"])
    assert out["utils.py::helper"] == "already known"
    assert len(llm.prompts) == len(state["symbols"]) - 1
    assert all("Read lines" in p for p in llm.prompts)  # prompts point at files, no source pasted


def test_malformed_output_is_rejected_reported_and_retried_with_error():
    state, store = initial_state(), ArtifactStore()
    llm = FakeLLM(["not json", '{"summary": ""}'])
    with pytest.raises(AgentOutputError):
        kb_summarizer(state, llm=llm, store=store)
    assert len(store.failures()) == 2
    assert "rejected" in llm.prompts[1]


def test_malformed_then_valid_recovers():
    state, store = initial_state(), ArtifactStore()
    state["symbols"] = {k: v for k, v in state["symbols"].items() if k == "utils.py::helper"}
    llm = FakeLLM(["oops", '```json\n{"summary": "ok"}\n```'])
    assert kb_summarizer(state, llm=llm, store=store)["summaries"] == {"utils.py::helper": "ok"}
    assert len(store.failures()) == 1 and store.records[-1]["kind"] == "call"


def test_planner_creates_one_strategy_per_group_callees_first():
    state = run_planning(fake_agents(interface_changer="none"), ArtifactStore())
    assert set(state["strategies"]) == set(state["scc_groups"])
    assert state["conflicting_group_ids"] == []


def test_planner_flags_disagreeing_neighbours():
    state = run_planning(fake_agents(), ArtifactStore())
    assert state["conflicting_group_ids"] == [
        "group::app.py::main", "group::utils.py::format_name", INTERFACE_CHANGER,
    ]
    deps = group_dependencies(state["dependency_graph"], state["scc_groups"])
    assert ("group::utils.py::format_name", INTERFACE_CHANGER) in \
        find_conflict_pairs(state["strategies"], deps)


def test_finalize_plan_uses_adjudicated_strategy_and_covers_every_symbol():
    state = run_planning(fake_agents(), ArtifactStore())
    state.update(finalize_plan(state, max_lines=15))
    chunks = state["chunks"]
    assert sorted(s for c in chunks.values() for s in c.symbol_ids) == sorted(state["symbols"])
    helper_chunk = next(c for c in chunks.values() if "utils.py::helper" in c.symbol_ids)
    assert "keep old signature" in helper_chunk.strategy
    assert state["chunk_queue"] and all(q in chunks for q in state["chunk_queue"])


def test_inconclusive_adjudication_blocks_planning():
    state = run_planning(fake_agents(adjudication="inconclusive"), ArtifactStore())
    assert all(a.outcome == "inconclusive" for a in state["adjudications"].values())
    with pytest.raises(PlanningError):
        finalize_plan(state, max_lines=50)


def test_planning_never_modifies_source_repo_and_records_artifacts(tmp_path):
    before = tree_hash(FIXTURE)
    store = ArtifactStore(tmp_path / "artifacts")
    state = run_planning(fake_agents(), store)
    finalize_plan(state, max_lines=50)
    assert tree_hash(FIXTURE) == before
    files = sorted((tmp_path / "artifacts").iterdir())
    assert len(files) == len(store.records) > 0
    assert all(":" not in f.name for f in files)


def test_route_adjudication_semantics():
    assert route_adjudication("fixed") == "retry"
    assert route_adjudication("false_positive") == "pass"
    assert route_adjudication("real_gap") == "blocked"
    assert route_adjudication("inconclusive") == "blocked"


def test_resolved_adjudication_requires_a_strategy():
    with pytest.raises(ValidationError):
        AdjudicationResult(target_id="g", outcome="resolved", rationale="r")
