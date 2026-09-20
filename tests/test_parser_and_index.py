from pathlib import Path

from graph_algorithms.scc import contract_cycles
from kb.index import SymbolIndex
from kb.repository import KnowledgeBase
from parser.implementation import PythonParser

FIXTURE = Path(__file__).parent / "fixtures" / "demo_repo"
EXCLUDE = ["legacy/*"]


def parse():
    return PythonParser().parse_repo(FIXTURE, EXCLUDE)


def test_symbols_ids_and_exclusion():
    syms = parse().symbols
    assert sorted(syms) == [
        "app.py::main", "mathops.py::double_all", "mathops.py::helper",
        "mathops.py::is_even", "mathops.py::is_odd",
        "utils.py::format_name", "utils.py::helper",
    ]  # no legacy/old.py, no class methods


def test_duplicate_names_resolved_through_imports():
    s = parse().symbols
    assert s["utils.py::format_name"].calls == ["utils.py::helper"]
    assert "utils.py::helper" in s["app.py::main"].calls        # from utils import helper as clean
    assert "mathops.py::helper" in s["mathops.py::double_all"].calls
    assert "mathops.py::helper" not in s["app.py::main"].calls


def test_module_attribute_calls_and_unresolved():
    main = parse().symbols["app.py::main"]
    assert "mathops.py::is_even" in main.calls and "mathops.py::double_all" in main.calls
    assert "print" in main.calls  # unresolved is kept, not dropped


def test_nested_function_calls_attributed_to_parent():
    da = parse().symbols["mathops.py::double_all"]
    assert da.calls == ["mathops.py::helper"]  # inner() itself is not an edge


def test_unsupported_syntax_is_reported():
    assert any("class Point" in w for w in parse().warnings)


def test_syntax_error_file_is_skipped_with_warning(tmp_path):
    (tmp_path / "ok.py").write_text("def f():\n    return 1\n")
    (tmp_path / "bad.py").write_text("def broken(:\n")
    res = PythonParser().parse_repo(tmp_path, [])
    assert list(res.symbols) == ["ok.py::f"]
    assert any("bad.py" in w for w in res.warnings)


def test_mutual_recursion_becomes_cycle_group():
    idx = SymbolIndex()
    idx.write(parse().symbols)
    groups = contract_cycles(KnowledgeBase(idx).dependency_graph())
    cycles = [g for g in groups.values() if g.is_cycle]
    assert [g.symbol_ids for g in cycles] == [["mathops.py::is_even", "mathops.py::is_odd"]]


def test_indexing_twice_gives_identical_snapshot():
    idx = SymbolIndex()
    idx.write(parse().symbols)
    first = idx.snapshot()
    idx.write(parse().symbols)
    assert idx.snapshot() == first


def test_knowledge_base_lookups_and_summary_retention():
    idx = SymbolIndex()
    idx.write(parse().symbols)
    kb = KnowledgeBase(idx)
    assert kb.dependencies("utils.py::format_name") == ["utils.py::helper"]
    assert "app.py::main" in kb.dependents("utils.py::helper")
    assert kb.unresolved("app.py::main") == ["print"]
    assert kb.get_symbol("nope") is None

    idx.set_summary("utils.py::helper", "strips text")
    idx.write(parse().symbols)  # re-index keeps summaries of surviving symbols
    assert kb.summary("utils.py::helper") == "strips text"
    assert kb.symbols() == parse().symbols
