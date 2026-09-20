from __future__ import annotations
import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any
from config import PYTHON_TO_TYPESCRIPT, Config
from graph import (
    build_migration_graph, initial_state, open_checkpointer, run_config, thread_id_for,
)
from kb.index import SymbolIndex
from llm import ClaudeLLM, LLMClient
from nodes.adjudicator import adjudicator
from nodes.common import ArtifactStore, PlanningError
from nodes.kb_summarizer import kb_summarizer
from nodes.planner import finalize_plan, planner
from nodes.setup import derive_graph, parse_index
from parser.implementation import PythonParser
from report import render_progress
from runner import CommandRunner, SubprocessRunner

log = logging.getLogger("code_migration")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="code-migration",
        description="Migrate a small Python repo to TypeScript with a checkpointed LangGraph pipeline.")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("repo", type=Path, help="source repository (never modified)")
        sp.add_argument("--target", type=Path, required=True, help="where migrated files go")
        sp.add_argument("--artifacts-dir", type=Path, default=Path(".migration"))
        sp.add_argument("--max-lines", type=int, default=150, help="line budget per chunk")
        sp.add_argument("--exclude", action="append", default=None, metavar="GLOB",
                        help="source globs to skip (repeatable; default: tests/*)")
        sp.add_argument("--model", default="claude-opus-5")

    common(sub.add_parser("dry-run", help="plan only (phases 0-3): no code is generated"))
    r = sub.add_parser("run", help="full migration")
    common(r)
    r.add_argument("--restart", action="store_true", help="discard an existing checkpoint")
    common(sub.add_parser("resume", help="continue an interrupted run from its checkpoint"))
    common(sub.add_parser("status", help="show progress of the latest run"))
    return p


def build_config(args: argparse.Namespace) -> Config:
    kwargs: dict[str, Any] = {}
    if args.exclude is not None:
        kwargs["exclude"] = args.exclude
    return Config(
        repo_path=args.repo, target_path=args.target, artifacts_dir=args.artifacts_dir,
        language_pair=PYTHON_TO_TYPESCRIPT, max_lines_per_chunk=args.max_lines, **kwargs)


def _run_dir(config: Config) -> Path:
    rd = config.run_dir(thread_id_for(config))
    rd.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(rd / "run.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    return rd


def _close_log() -> None:
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)


def _write_progress(rd: Path, state: dict[str, Any], config: Config) -> str:
    text = render_progress(state, thread_id_for(config))
    (rd / "progress.md").write_text(text, encoding="utf-8")
    return text


def _exit_code(state: dict[str, Any]) -> int:
    report = state.get("final_report")
    ok = (report is not None and not report.chunks_blocked and not report.chunks_failed
          and state.get("final_parity_passed") and state.get("final_e2e_passed"))
    return 0 if ok else 1


def cmd_dry_run(config: Config, llm: LLMClient) -> int:
    rd = _run_dir(config)
    store = ArtifactStore(rd / "artifacts")
    state = initial_state(config)
    log.info("dry-run start: %s", config.repo_path)
    state.update(parse_index(state, parser=PythonParser(), index=SymbolIndex(), config=config))
    state.update(derive_graph(state))
    state.update(kb_summarizer(state, llm=llm, store=store))
    state.update(planner(state, llm=llm, store=store))
    state.update(adjudicator(state, llm=llm, store=store))
    try:
        state.update(finalize_plan(
            state, max_lines=config.max_lines_per_chunk,
            target_extension=config.language_pair.target_extensions[0]))
    except PlanningError as exc:
        print(f"planning stopped: {exc}", file=sys.stderr)
        return 1
    plan = {
        "chunks": {k: v.model_dump() for k, v in state["chunks"].items()},
        "strategies": {k: v.model_dump() for k, v in state["strategies"].items()},
        "adjudications": {k: v.model_dump() for k, v in state["adjudications"].items()},
        "parser_warnings": state["warnings"],
    }
    (rd / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    print(f"{len(state['chunks'])} chunks planned; nothing was written to the target.")
    for cid, c in state["chunks"].items():
        print(f"  {cid}: {', '.join(c.source_files)} -> {', '.join(c.target_files)}")
    print(f"plan: {rd / 'plan.json'}")
    return 0


def cmd_run(config: Config, llm: LLMClient, runner: CommandRunner, resume: bool, restart: bool) -> int:
    rd = _run_dir(config)
    db = rd / "checkpoints.sqlite"
    if restart and db.exists():
        db.unlink()
    saver, index = open_checkpointer(db), SymbolIndex(str(rd / "index.sqlite"))
    try:
        graph = build_migration_graph(
            config=config, llm=llm, runner=runner, parser=PythonParser(), index=index,
            store=ArtifactStore(rd / "artifacts"), checkpointer=saver)
        cfg = run_config(config)
        existing = graph.get_state(cfg).values
        if resume and not existing:
            print("nothing to resume: no checkpoint for this repo and language pair",
                  file=sys.stderr)
            return 2
        if not resume and existing:
            print("a checkpoint exists for this migration: use `resume`, or `run --restart`",
                  file=sys.stderr)
            return 2
        try:
            log.info("%s", "resuming" if resume else "starting")
            state = graph.invoke(None if resume else initial_state(config), cfg)
        except Exception as exc:  # the checkpoint keeps everything finished so far
            log.exception("run interrupted")
            _write_progress(rd, dict(graph.get_state(cfg).values), config)
            print(f"run interrupted: {type(exc).__name__}: {exc}\n"
                  f"progress is saved; continue with `resume`.", file=sys.stderr)
            return 2
        print(_write_progress(rd, state, config))
        return _exit_code(state)
    finally:
        saver.conn.close()
        index.close()


def cmd_status(config: Config) -> int:
    rd = config.run_dir(thread_id_for(config))
    db = rd / "checkpoints.sqlite"
    if not db.exists():
        print("no run found for this repo and language pair", file=sys.stderr)
        return 1
    saver = open_checkpointer(db)
    try:
        tup = saver.get_tuple(run_config(config))
    finally:
        saver.conn.close()
    if tup is None:
        print("no checkpoint saved yet", file=sys.stderr)
        return 1
    print(render_progress(tup.checkpoint["channel_values"], thread_id_for(config)))
    return 0


def main(argv: list[str] | None = None, *, llm: LLMClient | None = None,
         runner: CommandRunner | None = None) -> int:
    """`llm` and `runner` can be injected (tests, demos); defaults are Claude and subprocess."""
    args = build_parser().parse_args(argv)
    config = build_config(args)
    try:
        if args.command == "status":
            return cmd_status(config)
        llm = llm or ClaudeLLM(model=args.model)
        if args.command == "dry-run":
            return cmd_dry_run(config, llm)
        return cmd_run(config, llm, runner or SubprocessRunner(),
                       resume=args.command == "resume", restart=getattr(args, "restart", False))
    finally:
        _close_log()


if __name__ == "__main__":
    sys.exit(main())
