import pytest
from pydantic import ValidationError

import state  # noqa: F401  (smoke: contracts import together)
from schemas import BuildResult, Chunk, SCCGroup, Symbol, ValidationResult


def _symbol(**kw):
    base = dict(id="a.py::f", file="a.py", name="f", kind="function",
                signature="def f()", start_line=1, end_line=3)
    return Symbol(**{**base, **kw})


def test_symbol_valid():
    assert _symbol().calls == []


def test_symbol_rejects_reversed_range():
    with pytest.raises(ValidationError):
        _symbol(start_line=5, end_line=2)


def test_symbol_rejects_non_positive_lines_and_empty_ids():
    with pytest.raises(ValidationError):
        _symbol(start_line=0)
    with pytest.raises(ValidationError):
        _symbol(id="")
    with pytest.raises(ValidationError):
        _symbol(calls=[""])


def test_groups_and_chunks_need_symbols():
    with pytest.raises(ValidationError):
        SCCGroup(id="g1", symbol_ids=[], is_cycle=False)
    with pytest.raises(ValidationError):
        Chunk(id="c1", group_id="g1", symbol_ids=[], source_files=[],
              target_files=[], strategy="s")


def test_build_result_error_type_consistency():
    BuildResult(chunk_id="c1", success=True)
    BuildResult(chunk_id="c1", success=False, error_type="infra")
    with pytest.raises(ValidationError):
        BuildResult(chunk_id="c1", success=True, error_type="syntax")
    with pytest.raises(ValidationError):
        BuildResult(chunk_id="c1", success=False)


def test_validation_result_error_type_consistency():
    ValidationResult(chunk_id="c1", passed=True)
    with pytest.raises(ValidationError):
        ValidationResult(chunk_id="c1", passed=False)
