from __future__ import annotations
from pathlib import Path
from typing import Any
from config import Config
from runner import CommandRunner
from schemas import IntegrationResult
from state import GraphState


def integration(state: GraphState, *, runner: CommandRunner, config: Config) -> dict[str, Any]:
    """Repository-wide regression check: full build, then the full test suite, so a chunk
    cannot be completed if it breaks previously migrated chunks."""
    c = state["current_chunk_id"]
    root, pair = Path(state["target_path"]), config.language_pair
    passed, details = True, None
    for cmd in (pair.build_command, pair.test_command):
        res = runner.run(cmd, root)
        if not res.ok:
            passed, details = False, (res.stderr or res.stdout or "failed")[-2000:]
            break
    results = dict(state.get("integration_results", {}))
    results[c] = IntegrationResult(chunk_id=c, passed=passed, details=details)
    update: dict[str, Any] = {"integration_results": results}
    if not passed:
        counters = dict(state.get("agent_attempts", {}))
        counters[c] = counters.get(c, 0) + 1
        update["agent_attempts"] = counters
    return update
