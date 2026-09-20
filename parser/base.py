from __future__ import annotations
from abc import ABC, abstractmethod
from pathlib import Path
from pydantic import BaseModel
from schemas import Symbol


class ParseResult(BaseModel):
    symbols: dict[str, Symbol]
    warnings: list[str] = []  # unsupported syntax, skipped files: never silently dropped


class LanguageParser(ABC):
    """Language-neutral interface. Implementations must be deterministic."""

    extensions: tuple[str, ...]

    @abstractmethod
    def parse_repo(self, root: Path, exclude: list[str]) -> ParseResult:
        """Index every supported file under `root` (excluding glob patterns)."""
