

from __future__ import annotations
from typing import Optional, Literal
from pydantic import BaseModel


class Symbol(BaseModel):
    id: str
    file: str
    name: str
    kind: Literal["function"]
    signature: str
    start_line: int
    end_line: int
    calls: list[str] = []


class DependencyGraph(BaseModel):
    edges: dict[str, list[str]]  # symbol_id -> symbol_ids it calls


class SCCGroup(BaseModel):
    id: str
    symbol_ids: list[str]
    is_cycle: bool


class PlannerStrategy(BaseModel):
    group_id: str
    description: str
    approach: str
    touches_public_interface: bool = False


class AdjudicationResult(BaseModel):
    target_id: str  # group_id (Phase 3 conflict) or chunk_id (Phase 4 retry exhaustion)
    outcome: Literal["fixed", "false_positive", "real_gap", "inconclusive", "resolved"]
    chosen_strategy: Optional[str] = None
    fix_path: Optional[str] = None
    rationale: str


class Chunk(BaseModel):
    id: str
    group_id: str
    symbol_ids: list[str]
    source_files: list[str]
    target_files: list[str]
    strategy: str


class BuildResult(BaseModel):
    chunk_id: str
    success: bool
    error_type: Optional[Literal["syntax", "compile", "infra", "other"]] = None
    stderr: Optional[str] = None


class ValidationResult(BaseModel):
    chunk_id: str
    passed: bool
    error_type: Optional[Literal["parity", "test", "lint"]] = None
    details: Optional[str] = None


class IntegrationResult(BaseModel):
    chunk_id: str
    passed: bool
    details: Optional[str] = None


class FinalReport(BaseModel):
    summary: str
    chunks_completed: list[str]
    chunks_blocked: list[str]
    chunks_failed: list[str]
    cleanup_notes: list[str]
    documentation_path: Optional[str] = None
