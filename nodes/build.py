from __future__ import annotations
import re
from pathlib import Path
from typing import Any, Literal
from config import Config
from runner import CommandResult, CommandRunner
from schemas import BuildResult
from state import GraphState

ErrorType = Literal["syntax", "compile", "infra", "other"]

_INFRA = re.compile(
    r"ENOENT|command not found|ECONNRESET|ETIMEDOUT|EADDRINUSE|ENOSPC|EMFILE|EBUSY|EPERM"
    r"|out of memory|Cannot allocate memory", re.I)
_SYNTAX = re.compile(r"SyntaxError|Unexpected token|error TS1\d{3}|Unterminated")
_COMPILE = re.compile(
    r"error TS\d+|Cannot find (module|name)|is not assignable|does not exist on type")


def classify_build_error(result: CommandResult) -> ErrorType:
    """Deterministic classification of a failed build command. Infrastructure problems
    (timeouts, missing tools, resource errors) are separated from code errors so they
    never consume the agent's retry budget."""
    text = f"{result.stderr}\n{result.stdout}"
    if result.timed_out or result.returncode == 127 or _INFRA.search(text):
        return "infra"
    if _SYNTAX.search(text):
        return "syntax"
    if _COMPILE.search(text):
        return "compile"
    return "other"


def build(state: GraphState, *, runner: CommandRunner, config: Config) -> dict[str, Any]:
    """Run the target build. A failure bumps the infra counter for infrastructure errors
    and the agent counter for everything else."""
    c = state["current_chunk_id"]
    res = runner.run(config.language_pair.build_command, Path(state["target_path"]))
    if res.ok:
        return {"last_build": BuildResult(chunk_id=c, success=True)}
    error_type = classify_build_error(res)
    key = "infra_attempts" if error_type == "infra" else "agent_attempts"
    counters = dict(state.get(key, {}))
    counters[c] = counters.get(c, 0) + 1
    return {
        "last_build": BuildResult(chunk_id=c, success=False, error_type=error_type,
                                  stderr=(res.stderr or res.stdout)[-2000:]),
        key: counters,
    }
