import hashlib
import json
import re
from pathlib import Path

import pytest

from config import Config, LanguagePair
from graph import (
    build_migration_graph, initial_state, open_checkpointer, run_config, thread_id_for,
)
from kb.index import SymbolIndex
from llm import FakeLLM
from nodes.common import ArtifactStore
from parser.implementation import PythonParser
from runner import CommandResult, FakeRunner

FIXTURE = Path(__file__).parent / "fixtures" / "demo_repo"
OK = CommandResult(returncode=0)
FAIL_TS = CommandResult(returncode=1, stderr="error TS2322: not assignable")
FAIL_TEST = CommandResult(returncode=1, stderr="1 test failed")
N_CHUNKS = 6  # fixture: 7 functions, is_even/is_odd form one atomic cycle; max_lines=1


def make_config(tmp_path: Path) -> Config:
    return Config(
        repo_path=FIXTURE, target_path=tmp_path / "out", exclude=["legacy/*"],
        max_lines_per_chunk=1,
        language_pair=LanguagePair(
            source="python", target="typescript", source_extensions=[".py"],
            target_extensions=[".ts"], build_command=["build"], test_command=["test"]),
    )


def agents(interface_changer=None, adjudication="real_gap", crash_on=None) -> FakeLLM:
    calls = {"migrate": 0}

    def reply(prompt: str) -> str:
        if "documenting" in prompt:
            return '{"summary": "does a thing"}'
        if "planning the migration" in prompt:
            touches = interface_changer is not None and f"Unit: {interface_changer} " in prompt
            return json.dumps({"description": "d", "approach": "port directly",
                               "touches_public_interface": touches})
        if "disagreeing views" in prompt:
            return '{"outcome": "resolved", "chosen_strategy": "keep signature", "rationale": "r"}'
        if "adjudicating a chunk" in prompt:
            return json.dumps({"outcome": adjudication, "rationale": "because"})
        if "You are fixing" in prompt:
            allowed = re.search(r"Allowed files: (.*)", prompt).group(1).split(", ")
            return json.dumps({"files": [{"path": allowed[0], "content": "// fixed"}]})
        if "end-to-end tests" in prompt:
            return json.dumps({"files": [{"path": "e2e/all.test.ts", "content": "// e2e"}]})
        if "migration documentation" in prompt:
            return '{"markdown": "# Migration notes"}'
        calls["migrate"] += 1
        if crash_on and calls["migrate"] == crash_on:
            raise RuntimeError("simulated crash")
        allowed = re.search(r"Allowed files: (.*)", prompt).group(1).split(", ")
        return json.dumps({"files": [{"path": p, "content": f"// {p}"} for p in allowed]})
    fake = FakeLLM(reply)
    fake.migrate_calls = calls
    return fake


def scripted(build=(OK,), parity=(OK,), tests=(OK,)) -> FakeRunner:
    seqs = {"build": list(build), "parity": list(parity), "test": list(tests)}
    used = {k: 0 for k in seqs}

    def handler(cmd, cwd):
        kind = "build" if cmd == ["build"] else "test" if cmd == ["test"] else "parity"
        i = min(used[kind], len(seqs[kind]) - 1)
        used[kind] += 1
        return seqs[kind][i]
    return FakeRunner(handler)


def make_graph(cfg, llm, runner, store=None, checkpointer=None):
    store = store or ArtifactStore()
    graph = build_migration_graph(
        config=cfg, llm=llm, runner=runner, parser=PythonParser(), index=SymbolIndex(),
        store=store, checkpointer=checkpointer)
    return graph, store


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.as_posix().encode() + p.read_bytes())
    return h.hexdigest()


def test_full_run_on_fixture_repo(tmp_path):
    cfg, before = make_config(tmp_path), tree_hash(FIXTURE)
    graph, store = make_graph(cfg, agents(interface_changer="group::utils.py::helper"), scripted())
    final = graph.invoke(initial_state(cfg), run_config(cfg))
    report = final["final_report"]
    assert len(report.chunks_completed) == N_CHUNKS and not report.chunks_blocked
    assert final["final_parity_passed"] and final["final_e2e_passed"]
    assert report.documentation_path == "MIGRATION.md"
    out = tmp_path / "out"
    assert (out / "MIGRATION.md").is_file() and (out / "e2e" / "all.test.ts").is_file()
    assert (out / "mathops.test.ts").is_file()
    assert any("class Point" in n for n in report.cleanup_notes)  # parser gap is reported
    assert tree_hash(FIXTURE) == before                            # source repo untouched
    assert any(r["kind"] == "report" for r in store.records)


def test_run_halts_and_reports_when_chunks_keep_failing(tmp_path):
    cfg = make_config(tmp_path)
    graph, _ = make_graph(cfg, agents(), scripted(build=(FAIL_TS,)))
    final = graph.invoke(initial_state(cfg), run_config(cfg))
    report = final["final_report"]
    assert len(report.chunks_blocked) == 3 and not report.chunks_completed
    assert any("run halted" in n for n in report.cleanup_notes)
    assert final["final_e2e_passed"] is None and report.documentation_path is None


def test_final_parity_loopback_recovers(tmp_path):
    cfg = make_config(tmp_path)
    # N integration runs pass, first final-parity run fails, then everything passes
    runner = scripted(tests=(OK,) * N_CHUNKS + (FAIL_TEST, OK))
    graph, store = make_graph(cfg, agents(), runner)
    final = graph.invoke(initial_state(cfg), run_config(cfg))
    assert final["final_parity_loop_count"] == 1 and final["final_parity_passed"]
    assert final["final_report"].documentation_path == "MIGRATION.md"
    assert (tmp_path / "out" / "MIGRATION.md").is_file()


def test_final_parity_loopback_is_capped_and_failure_is_reported(tmp_path):
    cfg = make_config(tmp_path)
    runner = scripted(tests=(OK,) * N_CHUNKS + (FAIL_TEST,))
    graph, _ = make_graph(cfg, agents(), runner)
    final = graph.invoke(initial_state(cfg), run_config(cfg))
    assert final["final_parity_loop_count"] == cfg.caps.max_final_parity_loops
    assert final["final_parity_passed"] is False and final["final_e2e_passed"] is None
    assert any("final parity failed" in n for n in final["final_report"].cleanup_notes)


def test_resume_after_crash_does_not_rerun_completed_chunks(tmp_path):
    cfg, db = make_config(tmp_path), tmp_path / "checkpoints.sqlite"
    config = run_config(cfg)

    crashing = agents(crash_on=3)  # chunks 1-2 finish, the 3rd migrator call dies
    graph, _ = make_graph(cfg, crashing, scripted(), checkpointer=open_checkpointer(db))
    with pytest.raises(RuntimeError, match="simulated crash"):
        graph.invoke(initial_state(cfg), config)
    assert crashing.migrate_calls["migrate"] == 3

    healthy = agents()  # a fresh process: new graph, same checkpoint db and thread_id
    graph2, _ = make_graph(cfg, healthy, scripted(), checkpointer=open_checkpointer(db))
    snapshot = graph2.get_state(config)
    assert snapshot.values["completed_chunk_ids"] == ["chunk-001", "chunk-002"]
    final = graph2.invoke(None, config)

    assert healthy.migrate_calls["migrate"] == N_CHUNKS - 2     # only the remaining chunks
    assert len(final["final_report"].chunks_completed) == N_CHUNKS
    assert not any("documenting" in p for p in healthy.prompts)  # planning phases not redone


def test_thread_id_is_stable_per_repo_and_language_pair(tmp_path):
    a = make_config(tmp_path)
    assert thread_id_for(a) == thread_id_for(make_config(tmp_path / "elsewhere"))
    b = make_config(tmp_path)
    b.language_pair = b.language_pair.model_copy(update={"target": "go"})
    assert thread_id_for(a) != thread_id_for(b)
