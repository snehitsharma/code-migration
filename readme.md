# Code Migration Agent

A checkpointed, multi-agent pipeline that migrates a small Python repository to
TypeScript. It builds a dependency-safe plan **before** any agent touches code,
migrates one chunk at a time with bounded retries, verifies each chunk
deterministically, and can resume after a crash. Built on LangGraph.

**Core idea:** never let an LLM change code before the safe order is known.
Ordering, cycle handling and chunking are plain deterministic algorithms; agents
only summarize, plan, adjudicate and write code, and everything they return is
schema-validated and recorded.

## Pipeline

```
Phase 0  Index          parse source -> symbols -> SQLite index
Phase 1  Task graph     dependency graph -> SCCs (cycles become atomic groups) -> topological order
Phase 2  Knowledge      one structured summary per symbol
Phase 3  Planning       strategy per group -> conflict adjudication -> immutable chunks
Phase 4  Migration      per chunk: migrate -> build -> parity tests -> integration, with retries
Phase 5  Final parity   repo-wide build + tests, capped fix loopback
Phase 6  E2E + docs     written in parallel
Phase 8  Report         what worked, what was blocked, what was skipped
```

Phase 7 (idiomatic refactoring) is deliberately not implemented.

## Quick start

```bash
pip install -r requirements.txt
python -m pytest -q                      # 72 tests, no API key needed

pip install anthropic                    # only for real runs
export ANTHROPIC_API_KEY=...

python cli.py dry-run  path/to/repo --target out/    # plan only, writes nothing to out/
python cli.py run      path/to/repo --target out/    # full migration
python cli.py status   path/to/repo --target out/    # progress from the checkpoint
python cli.py resume   path/to/repo --target out/    # continue after an interruption
```

Each run keeps its files under `.migration/<run-id>/`: `plan.json` (dry-run),
`artifacts/` (every agent prompt, raw reply, parsed output and validation
failure), `checkpoints.sqlite`, `progress.md`, `run.log`. The source repository
is never modified. Exit codes: `0` everything passed, `1` finished with blocked
or failed work, `2` interrupted or refused (continue with `resume`).

The target project needs `tsc` and `vitest` available, because build and parity
checks run `npx tsc --noEmit` and `npx vitest run` in it.

## How failures are handled

| Situation | Behaviour |
|---|---|
| Agent output fails its schema | recorded, retried once with the error, then treated as a failed attempt |
| Migrator writes outside its chunk's files | rejected, nothing written, counts as a failed attempt |
| Build/test fails (code error) | up to 3 agent attempts per chunk, with the error fed back |
| Build hits an infrastructure error (timeout, missing tool, OOM) | separate counter, up to 2 tries, then a hard failure that halts the run |
| Attempts exhausted | one adjudication: `fixed` (one extra attempt), `false_positive` (parity only), `real_gap` or `inconclusive` (chunk blocked) |
| Chunk blocked | run continues; chunks that depend on it are skipped; the run halts at 3 blocked chunks |
| Final parity fails | up to 2 fix loops, then the failure is reported and e2e/docs are skipped |

An inconclusive result is never promoted to success. All caps are configurable in
[`config.py`](config.py).

**Parity is deterministic:** a chunk passes only if its own test files exist and
pass. No tests means no parity claim, which fails.

## Layout

```
schemas.py, state.py      data contracts and LangGraph state
config.py                 language pair, caps, chunk size
parser/, kb/              Python parser (ast) and SQLite symbol index
graph_algorithms/         SCC, topological order, chunking (pure, no LangGraph)
nodes/                    agent nodes, build/parity/integration, routing, final phases
graph.py                  LangGraph wiring + SQLite checkpointing
cli.py, report.py         command line and progress report
llm.py, runner.py         LLM and command seams (real + fake implementations)
tests/                    unit, loop, graph and CLI tests with a fixture repo
```

## Testing

Everything above the model call is tested offline with a scripted fake LLM and
fake command runner: the graph algorithms (including shuffled-input
determinism), each retry branch, the full graph, crash-and-resume (completed
chunks are not re-run), and the CLI.

## Limitations

- One language pair (Python to TypeScript); the parser interface is extensible.
- Only top-level functions are indexed. Classes, methods, star imports and
  unparseable files are reported as warnings, not migrated.
- Chunks run one at a time; no wave scheduling or concurrency.
- Infrastructure errors are classified for the build step only.
- The real Claude client has not been exercised end to end; the pipeline is
  tested against fakes.
