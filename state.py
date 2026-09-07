

from __future__ import annotations
from typing import TypedDict, Optional, Literal
from schemas import (
    Symbol,
    DependencyGraph,
    SCCGroup,
    PlannerStrategy,
    AdjudicationResult,
    Chunk,
    BuildResult,
    ValidationResult,
    IntegrationResult,
    FinalReport,
)

AdjudicationOutcome = Literal["fixed", "false_positive", "real_gap", "inconclusive"]


class GraphState(TypedDict):
    # --- setup ---
    repo_path: str
    source_lang: str
    target_lang: str


    symbols: dict[str, Symbol]           # sym_id -> Symbol, one per function
    docs: dict[str, str]                 # doc_id -> linked_symbol_ids (optional)

    #  (run in parallel) 
    summaries: dict[str, str]            # sym_id -> KB-written summary
    dependency_graph: Optional[DependencyGraph]
    scc_groups: dict[str, SCCGroup]       # group_id -> contracted cycle/unit
    topo_order: list[str]                # group_ids, dependency-safe order

  # planning + adjudication 
    strategies: dict[str, PlannerStrategy]
    conflicting_group_ids: list[str]
    adjudications: dict[str, AdjudicationResult]
    chunks: dict[str, Chunk]             # final split/merged work units

    #  per-chunk migration loop 
    chunk_queue: list[str]               # dependency-ready chunk_ids, refilled as parents complete
    completed_chunk_ids: list[str]
    current_chunk_id: Optional[str]

    agent_attempts: dict[str, int]       # chunk_id -> agent-failure retries (cap 3)
    infra_attempts: dict[str, int]       # chunk_id -> infra-error retries, separate (cap 2)

    last_build: Optional[BuildResult]
    last_validation: Optional[ValidationResult]
    parity_adjudications: dict[str, AdjudicationOutcome]
    integration_results: dict[str, IntegrationResult]

    blocked_chunk_ids: list[str]          # exhausted retries/adjudication; run continues (halt if >3)
    failed_chunk_ids: list[str]           # hard failures; halt the run

    # final parity check
    final_parity_passed: Optional[bool]
    final_parity_loop_count: int          # cap 2

    #  e2e + docs (run in parallel) 
    final_e2e_passed: Optional[bool]
    documentation: Optional[str]

    final_report: Optional[FinalReport]

    # --- resumability ---
    current_phase: int                    # 0-8; full state is checkpointed via SqliteSaver
