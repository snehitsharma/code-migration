from __future__ import annotations
from pathlib import Path
from pydantic import BaseModel, Field


class RetryCaps(BaseModel):
    agent_attempts: int = Field(default=3, ge=1)
    infra_attempts: int = Field(default=2, ge=1)
    max_blocked_chunks: int = Field(default=3, ge=1)
    max_final_parity_loops: int = Field(default=2, ge=0)


class LanguagePair(BaseModel):
    source: str
    target: str
    source_extensions: list[str]
    target_extensions: list[str]
    build_command: list[str]  # run inside the target repo, e.g. ["npx", "tsc", "--noEmit"]
    test_command: list[str]
    test_file_template: str = "{stem}.test{ext}"  # parity tests live next to each target file

    def test_file_for(self, target_file: str) -> str:
        path = Path(target_file)
        return path.with_name(
            self.test_file_template.format(stem=path.stem, ext=path.suffix)
        ).as_posix()


class Config(BaseModel):
    repo_path: Path
    target_path: Path
    artifacts_dir: Path = Path(".migration")
    language_pair: LanguagePair
    max_lines_per_chunk: int = Field(default=150, ge=1)
    exclude: list[str] = ["tests/*", "*/__pycache__/*"]
    caps: RetryCaps = RetryCaps()

    def run_dir(self, run_id: str) -> Path:
        return self.artifacts_dir / run_id


PYTHON_TO_TYPESCRIPT = LanguagePair(
    source="python",
    target="typescript",
    source_extensions=[".py"],
    target_extensions=[".ts"],
    build_command=["npx", "tsc", "--noEmit"],
    test_command=["npx", "vitest", "run"],
)
