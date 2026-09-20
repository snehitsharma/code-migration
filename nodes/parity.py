from __future__ import annotations
from pathlib import Path
from typing import Any
from config import Config
from runner import CommandRunner
from schemas import ValidationResult
from state import GraphState


def parity(state: GraphState, *, runner: CommandRunner, config: Config) -> dict[str, Any]:
    """Deterministic parity: the chunk's own test files must exist and pass. No tests
    means no parity claim, which is a failure rather than a pass."""
    c = state["current_chunk_id"]
    chunk, pair = state["chunks"][c], config.language_pair
    root = Path(state["target_path"])
    tests = [pair.test_file_for(f) for f in chunk.target_files]
    existing = [t for t in tests if (root / t).is_file()]

    if not existing:
        result = ValidationResult(
            chunk_id=c, passed=False, error_type="test",
            details=f"no parity tests found; expected one of: {', '.join(tests)}")
    else:
        res = runner.run([*pair.test_command, *existing], root)
        result = (ValidationResult(chunk_id=c, passed=True) if res.ok else ValidationResult(
            chunk_id=c, passed=False, error_type="test",
            details=(res.stderr or res.stdout or "tests failed")[-2000:]))

    update: dict[str, Any] = {"last_validation": result}
    if not result.passed:
        counters = dict(state.get("agent_attempts", {}))
        counters[c] = counters.get(c, 0) + 1
        update["agent_attempts"] = counters
    return update
