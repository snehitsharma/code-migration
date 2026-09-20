import json
import re
from pathlib import Path

import pytest

from config import Config, LanguagePair, RetryCaps
from graph_algorithms.chunking import build_chunks
from graph_algorithms.dependencies import build_dependency_graph
from graph_algorithms.scc import contract_cycles, group_dependencies, topological_order
from llm import FakeLLM
from nodes.adjudicator import chunk_adjudicator
from nodes.build import build, classify_build_error
from nodes.code_migrator import code_migrator
from nodes.common import ArtifactStore
from nodes.integration import integration
from nodes.parity import parity
from nodes.routing import (
    dispatch, finish_chunk, route_after_adjudication, route_after_build,
    route_after_integration, route_after_migrate, route_after_parity, route_dispatch,
)
from runner import CommandResult, FakeRunner
from schemas import Symbol

OK = CommandResult(returncode=0)
TS_SYNTAX = CommandResult(returncode=1, stderr="a.ts(1,5): error TS1005: ';' expected.")
TS_TYPE = CommandResult(returncode=1, stderr="a.ts(2,1): error TS2322: not assignable")
INFRA = CommandResult(returncode=1, stderr="Error: ENOENT: tsc missing")
CAPS = RetryCaps()


def make_config(tmp_path: Path) -> Config:
    return Config(
        repo_path=tmp_path, target_path=tmp_path / "out",
        language_pair=LanguagePair(
            source="python", target="typescript", source_extensions=[".py"],
            target_extensions=[".ts"], build_command=["build"], test_command=["test"]),
    )


def make_state(tmp_path: Path, calls: dict[str, list[str]], max_lines: int = 10) -> dict:
    symbols = {
        n: Symbol(id=n, file=f"{n}.py", name=n, kind="function", signature=f"def {n}()",
                  start_line=1, end_line=10, calls=c)
        for n, c in calls.items()
    }
    graph = build_dependency_graph(symbols)
    groups = contract_cycles(graph)
    order = topological_order(group_dependencies(graph, groups))
    return {
        "source_lang": "python", "target_lang": "typescript",
        "target_path": str(tmp_path / "out"), "symbols": symbols, "dependency_graph": graph,
        "chunks": build_chunks(groups, order, symbols, max_lines, {}, ".ts"),
        "agent_attempts": {}, "infra_attempts": {}, "adjudications": {},
        "parity_adjudications": {}, "integration_results": {}, "completed_chunk_ids": [],
        "blocked_chunk_ids": [], "skipped_chunk_ids": [], "failed_chunk_ids": [],
        "current_chunk_id": None, "last_build": None, "last_validation": None,
    }


def agents(adjudication="real_gap", migrator=None) -> FakeLLM:
    """Migrator writes every allowed file (or whatever `migrator(n)` says for call n)."""
    calls = {"n": 0}

    def reply(prompt: str) -> str:
        if "adjudicating" in prompt:
            if adjudication == "garbage":
                return "not json"
            return json.dumps({"outcome": adjudication, "rationale": "because"})
        calls["n"] += 1
        allowed = re.search(r"Allowed files: (.*)", prompt).group(1).split(", ")
        files = migrator(calls["n"], allowed) if migrator else allowed
        return json.dumps({"files": [{"path": p, "content": f"// {p}"} for p in files]})
    return FakeLLM(reply)


def scripted(build=(OK,), parity=(OK,), tests=(OK,)) -> FakeRunner:
    """Sequences per command kind; the last entry repeats. parity = test cmd with files."""
    seqs = {"build": list(build), "parity": list(parity), "test": list(tests)}
    used = {k: 0 for k in seqs}

    def handler(cmd, cwd):
        kind = "build" if cmd == ["build"] else "test" if cmd == ["test"] else "parity"
        i = min(used[kind], len(seqs[kind]) - 1)
        used[kind] += 1
        return seqs[kind][i]
    return FakeRunner(handler)


def drive(state, llm, runner, config, caps=CAPS, store=None):
    """Tiny stand-in for graph.py: follows the routing functions until finalize/halt."""
    store = store or ArtifactStore()
    trace, step = [], "dispatch"
    while step not in ("finalize", "halt"):
        trace.append(step)
        assert len(trace) < 200, trace
        if step == "dispatch":
            state.update(dispatch(state, caps)); step = route_dispatch(state, caps)
        elif step == "migrate":
            state.update(code_migrator(state, llm=llm, store=store, config=config))
            step = route_after_migrate(state, caps)
        elif step == "build":
            state.update(build(state, runner=runner, config=config))
            step = route_after_build(state, caps)
        elif step == "parity":
            state.update(parity(state, runner=runner, config=config))
            step = route_after_parity(state, caps)
        elif step == "integration":
            state.update(integration(state, runner=runner, config=config))
            step = route_after_integration(state, caps)
        elif step == "adjudicate":
            state.update(chunk_adjudicator(state, llm=llm, store=store, caps=caps))
            step = route_after_adjudication(state, caps)
        elif step in ("complete", "block", "fail"):
            status = {"complete": "completed", "block": "blocked", "fail": "failed"}[step]
            state.update(finish_chunk(state, status)); step = "dispatch"
    return step, trace


def migrator_calls(llm: FakeLLM) -> int:
    return sum("porting" in p for p in llm.prompts)


def test_happy_path_migrates_builds_validates_integrates_and_completes(tmp_path):
    state, cfg, llm = make_state(tmp_path, {"a": []}), make_config(tmp_path), agents()
    step, trace = drive(state, llm, scripted(), cfg)
    assert step == "finalize"
    assert trace == ["dispatch", "migrate", "build", "parity", "integration", "complete", "dispatch"]
    assert state["completed_chunk_ids"] == ["chunk-001"]
    assert (tmp_path / "out" / "a.ts").is_file() and (tmp_path / "out" / "a.test.ts").is_file()
    assert state["integration_results"]["chunk-001"].passed


def test_build_error_retries_migrator_with_error_feedback(tmp_path):
    state, cfg, llm = make_state(tmp_path, {"a": []}), make_config(tmp_path), agents()
    step, trace = drive(state, llm, scripted(build=(TS_SYNTAX, OK)), cfg)
    assert step == "finalize" and state["completed_chunk_ids"] == ["chunk-001"]
    assert trace.count("migrate") == 2 and state["agent_attempts"]["chunk-001"] == 1
    assert "TS1005" in llm.prompts[1]


def test_infra_error_retries_build_only_on_separate_counter(tmp_path):
    state, cfg, llm = make_state(tmp_path, {"a": []}), make_config(tmp_path), agents()
    step, trace = drive(state, llm, scripted(build=(INFRA, OK)), cfg)
    assert trace.count("migrate") == 1 and trace.count("build") == 2
    assert state["infra_attempts"]["chunk-001"] == 1
    assert state["agent_attempts"].get("chunk-001", 0) == 0
    assert state["completed_chunk_ids"] == ["chunk-001"]


def test_infra_retries_exhausted_is_a_hard_failure_that_halts(tmp_path):
    state, cfg, llm = make_state(tmp_path, {"a": []}), make_config(tmp_path), agents()
    step, trace = drive(state, llm, scripted(build=(INFRA,)), cfg)
    assert step == "halt" and state["failed_chunk_ids"] == ["chunk-001"]
    assert state["infra_attempts"]["chunk-001"] == 2 and migrator_calls(llm) == 1


def test_exhausted_agent_retries_go_to_adjudication_then_block(tmp_path):
    state, cfg, llm = make_state(tmp_path, {"a": []}), make_config(tmp_path), agents("real_gap")
    step, trace = drive(state, llm, scripted(build=(TS_TYPE,)), cfg)
    assert step == "finalize"
    assert migrator_calls(llm) == 3 and "adjudicate" in trace and "block" in trace
    assert state["blocked_chunk_ids"] == ["chunk-001"] and not state["failed_chunk_ids"]
    assert state["parity_adjudications"] == {"chunk-001": "real_gap"}


def test_dependents_of_blocked_chunk_are_skipped(tmp_path):
    state = make_state(tmp_path, {"a": ["b"], "b": ["c"], "c": []})
    step, _ = drive(state, agents(), scripted(build=(TS_TYPE,)), make_config(tmp_path))
    assert step == "finalize"
    assert state["blocked_chunk_ids"] == ["chunk-001"]          # c, the deepest callee
    assert state["skipped_chunk_ids"] == ["chunk-002", "chunk-003"]  # b and a, transitively
    assert state["completed_chunk_ids"] == []


def test_fixed_adjudication_grants_exactly_one_more_attempt(tmp_path):
    state, cfg, llm = make_state(tmp_path, {"a": []}), make_config(tmp_path), agents("fixed")
    step, _ = drive(state, llm, scripted(build=(TS_TYPE, TS_TYPE, TS_TYPE, OK)), cfg)
    assert state["completed_chunk_ids"] == ["chunk-001"] and migrator_calls(llm) == 4
    assert "Adjudicator guidance (fixed)" in llm.prompts[-1]


def test_fixed_but_still_failing_is_blocked_after_second_exhaustion(tmp_path):
    state, cfg, llm = make_state(tmp_path, {"a": []}), make_config(tmp_path), agents("fixed")
    step, _ = drive(state, llm, scripted(build=(TS_TYPE,)), cfg)
    assert state["blocked_chunk_ids"] == ["chunk-001"] and migrator_calls(llm) == 4


def test_false_positive_parity_failure_proceeds_to_integration(tmp_path):
    state, cfg = make_state(tmp_path, {"a": []}), make_config(tmp_path)
    bad_tests = CommandResult(returncode=1, stderr="expected 1 got 2")
    step, trace = drive(state, agents("false_positive"), scripted(parity=(bad_tests,)), cfg)
    assert state["completed_chunk_ids"] == ["chunk-001"]
    assert state["parity_adjudications"]["chunk-001"] == "false_positive"
    assert trace.index("adjudicate") < trace.index("integration")


def test_false_positive_on_a_failed_build_is_not_accepted(tmp_path):
    state, cfg = make_state(tmp_path, {"a": []}), make_config(tmp_path)
    drive(state, agents("false_positive"), scripted(build=(TS_TYPE,)), cfg)
    assert state["blocked_chunk_ids"] == ["chunk-001"] and not state["completed_chunk_ids"]


def test_inconclusive_or_garbage_adjudication_never_passes(tmp_path):
    for verdict in ("inconclusive", "garbage"):
        state, cfg = make_state(tmp_path, {"a": []}), make_config(tmp_path)
        drive(state, agents(verdict), scripted(build=(TS_TYPE,)), cfg)
        assert state["blocked_chunk_ids"] == ["chunk-001"]
        assert state["parity_adjudications"]["chunk-001"] == "inconclusive"


def test_integration_failure_is_retried(tmp_path):
    state, cfg = make_state(tmp_path, {"a": []}), make_config(tmp_path)
    regress = CommandResult(returncode=1, stderr="earlier chunk broke")
    step, trace = drive(state, agents(), scripted(tests=(regress, OK)), cfg)
    assert state["completed_chunk_ids"] == ["chunk-001"] and trace.count("integration") == 2
    assert state["agent_attempts"]["chunk-001"] == 1


def test_scope_violation_is_rejected_and_nothing_is_written(tmp_path):
    def migrator(n, allowed):
        return ["../evil.ts", "other.ts"] if n == 1 else allowed
    state, cfg, store = make_state(tmp_path, {"a": []}), make_config(tmp_path), ArtifactStore()
    step, trace = drive(state, agents(migrator=migrator), scripted(), cfg, store=store)
    assert not (tmp_path / "evil.ts").exists() and not (tmp_path / "out" / "other.ts").exists()
    assert any(r["kind"] == "rejected" for r in store.records)
    assert state["agent_attempts"]["chunk-001"] == 1 and state["completed_chunk_ids"] == ["chunk-001"]
    assert trace[:4] == ["dispatch", "migrate", "migrate", "build"]  # rejected output skips the build


def test_missing_parity_tests_fail_parity(tmp_path):
    def migrator(n, allowed):
        return [p for p in allowed if ".test." not in p] if n == 1 else allowed
    state, cfg, llm = make_state(tmp_path, {"a": []}), make_config(tmp_path), agents(migrator=migrator)
    step, trace = drive(state, llm, scripted(), cfg)
    assert trace.count("parity") == 2 and state["completed_chunk_ids"] == ["chunk-001"]
    assert "no parity tests found" in llm.prompts[1]


def test_run_halts_when_blocked_cap_is_reached(tmp_path):
    state = make_state(tmp_path, {"a": [], "b": [], "c": [], "d": []})
    step, _ = drive(state, agents(), scripted(build=(TS_TYPE,)), make_config(tmp_path))
    assert step == "halt" and len(state["blocked_chunk_ids"]) == 3
    assert "chunk-004" not in state["blocked_chunk_ids"] + state["completed_chunk_ids"]


def test_dispatch_only_releases_dependency_ready_chunks(tmp_path):
    state = make_state(tmp_path, {"a": ["b"], "b": []})
    assert dispatch(state, CAPS)["chunk_queue"] == ["chunk-001"]
    state["completed_chunk_ids"] = ["chunk-001"]
    assert dispatch(state, CAPS)["current_chunk_id"] == "chunk-002"


@pytest.mark.parametrize("result,expected", [
    (CommandResult(returncode=1, timed_out=True), "infra"),
    (CommandResult(returncode=127, stderr="command not found: npx"), "infra"),
    (CommandResult(returncode=1, stderr="JavaScript heap out of memory"), "infra"),
    (CommandResult(returncode=1, stderr="error TS1005: ';' expected"), "syntax"),
    (CommandResult(returncode=1, stderr="SyntaxError: Unexpected token"), "syntax"),
    (CommandResult(returncode=1, stderr="error TS2322: not assignable"), "compile"),
    (CommandResult(returncode=1, stderr="something odd"), "other"),
])
def test_classify_build_error(result, expected):
    assert classify_build_error(result) == expected
