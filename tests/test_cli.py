from pathlib import Path

from cli import main
from graph import thread_id_for
from report import render_progress
from runner import CommandResult, FakeRunner
from test_graph import FAIL_TS, FIXTURE, N_CHUNKS, agents, make_config, scripted, tree_hash


def argv(cmd, tmp_path, *extra):
    return [cmd, str(FIXTURE), "--target", str(tmp_path / "out"),
            "--artifacts-dir", str(tmp_path / "runs"), "--max-lines", "1",
            "--exclude", "legacy/*", *extra]


def tsc_fails() -> FakeRunner:
    """The CLI uses the real Python->TypeScript preset, so match its actual build command."""
    return FakeRunner(lambda cmd, cwd: FAIL_TS if cmd[:2] == ["npx", "tsc"] else CommandResult(returncode=0))


def run_dir(tmp_path) -> Path:
    return tmp_path / "runs" / thread_id_for(make_config(tmp_path))


def test_dry_run_plans_without_generating_code(tmp_path, capsys):
    before = tree_hash(FIXTURE)
    assert main(argv("dry-run", tmp_path), llm=agents()) == 0
    out = capsys.readouterr().out
    assert f"{N_CHUNKS} chunks planned" in out and "nothing was written" in out
    assert (run_dir(tmp_path) / "plan.json").is_file()
    assert not (tmp_path / "out").exists() and tree_hash(FIXTURE) == before


def test_run_writes_progress_report_and_status_reads_the_checkpoint(tmp_path, capsys):
    assert main(argv("run", tmp_path), llm=agents(), runner=scripted()) == 0
    rd = run_dir(tmp_path)
    progress = (rd / "progress.md").read_text(encoding="utf-8")
    assert progress.count("| completed |") == N_CHUNKS and "## Final report" in progress
    assert (rd / "run.log").is_file() and (rd / "artifacts").is_dir()
    assert (tmp_path / "out" / "MIGRATION.md").is_file()
    capsys.readouterr()

    assert main(argv("status", tmp_path)) == 0
    assert capsys.readouterr().out.count("| completed |") == N_CHUNKS


def test_run_refuses_existing_checkpoint_unless_restarted(tmp_path):
    assert main(argv("run", tmp_path), llm=agents(), runner=scripted()) == 0
    assert main(argv("run", tmp_path), llm=agents(), runner=scripted()) == 2
    assert main(argv("run", tmp_path, "--restart"), llm=agents(), runner=scripted()) == 0


def test_resume_without_checkpoint_and_status_without_run_fail_cleanly(tmp_path):
    assert main(argv("resume", tmp_path), llm=agents(), runner=scripted()) == 2
    assert main(argv("status", tmp_path)) == 1


def test_failing_chunks_give_nonzero_exit_and_visible_report(tmp_path):
    code = main(argv("run", tmp_path), llm=agents(), runner=tsc_fails())
    assert code == 1
    progress = (run_dir(tmp_path) / "progress.md").read_text(encoding="utf-8")
    assert "| blocked |" in progress and "run halted" in progress


def test_interrupted_run_can_be_resumed(tmp_path, capsys):
    assert main(argv("run", tmp_path), llm=agents(crash_on=3), runner=scripted()) == 2
    assert "resume" in capsys.readouterr().err
    partial = (run_dir(tmp_path) / "progress.md").read_text(encoding="utf-8")
    assert partial.count("| completed |") == 2

    healthy = agents()
    assert main(argv("resume", tmp_path), llm=healthy, runner=scripted()) == 0
    assert healthy.migrate_calls["migrate"] == N_CHUNKS - 2


def test_render_progress_before_planning():
    assert "not produced chunks" in render_progress({}, "t")
