# Migration Agent

Multi-agent codebase migration system. Parses a legacy repo, builds a
dependency-safe task graph, plans a migration strategy per unit, executes
it chunk-by-chunk with retries and adjudication, validates the result.
Built as a LangGraph project.

**Status: early.** `schemas.py` and `state.py` exist. 
 This README describes the target design so implementation
has something concrete to build against.

## High-Level Design

The pipeline is 9 phases, run roughly in this order (2 pairs run in
parallel; everything else is sequential):

```
Phase 0  Indexing              tree-sitter parse -> symbols -> SQLite + MCP server
Phase 1  Task graph derivation  \_ run in parallel, join before Phase 3
Phase 2  Knowledge base          /
Phase 3  Planning + adjudication  planner -> adjudicator (on conflict) -> chunking
Phase 4  Iterative migration      per-chunk: migrate -> build/retry -> validate -> integrate
Phase 5  Final parity             repo-wide validation, loopback fixes (capped)
Phase 6  E2E + documentation      \_ run in parallel, join before Phase 8
                                    /
Phase 8  Completion               assemble final report
```

Phase 7 (idiomatic refactor) is **not built in v1** — skipped entirely,
not just deferred silently.

**Core idea:** not to let an LLM touch code before it knows the safe
order. Phases 0-1 exist purely to produce that order
(and to detect where "safe order" doesn't exist — cycles — and merge
those into atomic units) before any agent starts making changes.

**Execution model (v1, locked):** per-chunk only. One chunk in flight
at a time, dispatched off `chunk_queue` in dependency order. No
wave-barrier mode, no concurrency, no model-tier routing. These are
simplifications to get a working end-to-end run before adding
complexity back in.

## Low-Level Design

### schemas.py — data models (built)

| Model | Purpose |
|---|---|
| `Symbol` | one function: id, file, signature, line range, `calls` list |
| `DependencyGraph` | `symbol_id -> [symbol_ids it calls]` |
| `SCCGroup` | contracted cycle or single unit; `is_cycle` flag |
| `PlannerStrategy` | one group's proposed migration approach |
| `AdjudicationResult` | resolves a conflict (Phase 3) or a retry-exhaustion decision (Phase 4); `outcome` is one of `fixed / false_positive / real_gap / inconclusive / resolved` |
| `Chunk` | final work unit after planning — immutable plan artifact, not execution state |
| `BuildResult` | pass/fail + error type (`syntax / compile / infra / other`) |
| `ValidationResult` | pass/fail + error type (`parity / test / lint`) |
| `IntegrationResult` | per-chunk integration/test outcome |
| `FinalReport` | Phase 8 output |

### state.py — LangGraph state (built)

Execution state (completed/blocked/failed chunk ids, retry counters,
current chunk) lives here, separately from `Chunk` in schemas.py, which
stays an immutable plan artifact — the plan doesn't mutate as execution
runs, only the state does.

Key fields:
- `chunk_queue: list[str]` — dependency-ready chunks, refilled as parents complete
- `agent_attempts` / `infra_attempts` — **separate dicts**, different caps, different failure branches feed them (compile/syntax vs. lock/OOM/network/timeout)
- `parity_adjudications: dict[str, AdjudicationOutcome]` — just the outcome literal, not a full `AdjudicationResult`
- `blocked_chunk_ids` vs `failed_chunk_ids` — blocked doesn't halt the run (up to a cap), failed does
- `current_phase: int` — for quick resume reporting; full state is checkpointed by LangGraph's `SqliteSaver` regardless

### Config caps (not yet in a config.py — values decided, file not written)

| Cap | Value |
|---|---|
| `agent_attempts` per chunk | 3 |
| `infra_attempts` per chunk | 2 |
| `max_blocked_chunks` before halting the run | 3 |
| `final_parity_loop_count` | 2 |

### graph.py — node wiring (not built)

Planned nodes:
- `parse_index` (deterministic) — Phase 0
- `build_dependency_graph` (deterministic) — Phase 1, runs parallel to `kb_summarizer`
- `kb_summarizer` (agent) — Phase 2
- `planner` (agent) — Phase 3, also owns chunking after adjudication clears
- `adjudicator` (agent) — reused at two call sites: Phase 3 conflict resolution, Phase 4 retry-exhaustion decisions
- `code_migrator` (agent) — Phase 4 transform step
- `run_build` (deterministic/tool) — compiles, classifies error type
- `parity_verifier` (agent) — reused at Phase 4 (per-chunk) and Phase 5 (repo-wide)
- `integration_test` (deterministic/tool)
- `dispatch_next_chunk` (deterministic) — pops `chunk_queue`, routes to `code_migrator` or on to Phase 5 when empty
- `e2e_test_crafter`, `documentation_writer` (agents) — Phase 6, parallel
- `finalize_report` (deterministic) — Phase 8

Conditional edges needed: build success/fail routing, agent vs infra
retry routing, retry-exhaustion → adjudicator, adjudication outcome
routing (`fixed/false_positive/real_gap/inconclusive`), final-parity
loopback (capped), blocked-count halt check.

## Not yet built

- `parser/` — tree-sitter walk, symbol extraction, SQLite write
- `kb/` — SQLite index + in-process MCP server (symbol/summary/edge lookup)
- `graph/` — dependency graph build, SCC contraction, topo sort, chunking (pure algorithm — should be testable standalone, no LangGraph dependency)
- `nodes/` — every agent node listed above
- `graph.py` — actual LangGraph wiring
- `config.py` — the caps table above, as code
- Checkpointing wiring (`SqliteSaver`, `thread_id` scheme)

## Build order

1. ~~`schemas.py` + `state.py`~~ — done
2. `parser/` — get real `Symbol` objects out of a small demo repo
3. `graph/` — dependency graph + SCC contraction + topo sort + chunking; test standalone before touching LangGraph at all
4. `kb/` — MCP server over the SQLite index
5. `nodes/kb_summarizer.py`, `nodes/planner.py`, `nodes/adjudicator.py` — get Phases 2-3 producing real `Chunk`s
6. `nodes/code_migrator.py`, `nodes/parity_verifier.py` + the retry/adjudication loop — Phase 4 core; get one chunk migrating end-to-end before wiring the full queue
7. Wire `chunk_queue` dispatch, run the demo repo through all chunks
8. Phase 5 (reuse `parity_verifier`), Phase 6 (parallel), Phase 8 (`finalize_report`)
9. `SqliteSaver` checkpointing; test resume by killing the process mid-run

## Scope notes (v1, decided)

One language pair, small demo repo (5-10 files). No wave-barrier mode.
