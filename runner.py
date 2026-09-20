from __future__ import annotations
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Protocol
from pydantic import BaseModel


class CommandResult(BaseModel):
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class CommandRunner(Protocol):
    def run(self, cmd: list[str], cwd: Path, timeout: float = 300) -> CommandResult: ...


class SubprocessRunner:
    def run(self, cmd: list[str], cwd: Path, timeout: float = 300) -> CommandResult:
        exe = shutil.which(cmd[0])  # resolves npx.cmd etc. on Windows
        if exe is None:
            return CommandResult(returncode=127, stderr=f"command not found: {cmd[0]}")
        try:
            p = subprocess.run([exe, *cmd[1:]], cwd=cwd, capture_output=True, text=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            return CommandResult(returncode=-1, stderr="timed out", timed_out=True)
        return CommandResult(returncode=p.returncode, stdout=p.stdout, stderr=p.stderr)


class FakeRunner:
    """Offline stand-in: `handler(cmd, cwd) -> CommandResult`. Every call is kept in `calls`."""

    def __init__(self, handler: Callable[[list[str], Path], CommandResult]) -> None:
        self.handler = handler
        self.calls: list[list[str]] = []

    def run(self, cmd: list[str], cwd: Path, timeout: float = 300) -> CommandResult:
        self.calls.append(list(cmd))
        return self.handler(cmd, cwd)
