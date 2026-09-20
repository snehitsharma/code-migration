from __future__ import annotations
from typing import Optional, Literal
from pydantic import BaseModel, Field, model_validator


class Symbol(BaseModel):
    id: str = Field(min_length=1)  # "<file>::<name>" keeps duplicate names unique
    file: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: Literal["function"]
    signature: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    calls: list[str] = []

    @model_validator(mode="after")
    def _check_range(self) -> Symbol:
        if self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        if any(not c for c in self.calls):
            raise ValueError("calls must not contain empty symbol ids")
        return self


class DependencyGraph(BaseModel):
    edges: dict[str, list[str]]  # symbol_id -> symbol_ids it calls


class SCCGroup(BaseModel):
    id: str = Field(min_length=1)
    symbol_ids: list[str] = Field(min_length=1)
    is_cycle: bool


class PlannerStrategy(BaseModel):
    group_id: str
    description: str
    approach: str
    touches_public_interface: bool = False


# "resolved" is only used for Phase 3 strategy conflicts; the other four apply
# to Phase 4 retry exhaustion.
AdjudicationOutcome = Literal["fixed", "false_positive", "real_gap", "inconclusive", "resolved"]


class AdjudicationResult(BaseModel):
    target_id: str  # group_id (Phase 3 conflict) or chunk_id (Phase 4 retry exhaustion)
    outcome: AdjudicationOutcome
    chosen_strategy: Optional[str] = None
    fix_path: Optional[str] = None
    rationale: str

    @model_validator(mode="after")
    def _resolved_needs_strategy(self) -> AdjudicationResult:
        if self.outcome == "resolved" and not self.chosen_strategy:
            raise ValueError("a resolved adjudication requires chosen_strategy")
        return self


class Chunk(BaseModel):
    id: str = Field(min_length=1)
    group_id: str
    symbol_ids: list[str] = Field(min_length=1)
    source_files: list[str]
    target_files: list[str]
    strategy: str


class BuildResult(BaseModel):
    chunk_id: str
    success: bool
    error_type: Optional[Literal["syntax", "compile", "infra", "other"]] = None
    stderr: Optional[str] = None

    @model_validator(mode="after")
    def _check_error_type(self) -> BuildResult:
        if self.success and self.error_type is not None:
            raise ValueError("a successful build cannot have an error_type")
        if not self.success and self.error_type is None:
            raise ValueError("a failed build requires an error_type")
        return self


class ValidationResult(BaseModel):
    chunk_id: str
    passed: bool
    error_type: Optional[Literal["parity", "test", "lint"]] = None
    details: Optional[str] = None

    @model_validator(mode="after")
    def _check_error_type(self) -> ValidationResult:
        if self.passed and self.error_type is not None:
            raise ValueError("a passing validation cannot have an error_type")
        if not self.passed and self.error_type is None:
            raise ValueError("a failed validation requires an error_type")
        return self


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
