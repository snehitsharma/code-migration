from __future__ import annotations
import ast
from fnmatch import fnmatch
from pathlib import Path
from parser.base import LanguageParser, ParseResult
from schemas import Symbol


def _module_names(rel: str) -> list[str]:
    mod = rel[: -len(".py")].replace("/", ".")
    return [mod[: -len(".__init__")]] if mod.endswith(".__init__") else [mod]


class PythonParser(LanguageParser):
    """Indexes top-level functions and their direct calls.

    Resolution: same-file functions, `from m import f [as g]`, and `import m` + `m.f()`.
    Anything else is kept as a raw name and reported as unresolved. Not supported (and
    reported as warnings): methods, nested-function symbols, star imports, dynamic calls.
    """

    extensions = (".py",)

    def parse_repo(self, root: Path, exclude: list[str]) -> ParseResult:
        files = sorted(
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.suffix in self.extensions and p.is_file()
        )
        files = [f for f in files if not any(fnmatch(f, pat) for pat in exclude)]
        modules = {m: f for f in files for m in _module_names(f)}

        warnings: list[str] = []
        trees: dict[str, ast.Module] = {}
        for rel in files:
            try:
                trees[rel] = ast.parse((root / rel).read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError) as exc:
                warnings.append(f"{rel}: skipped ({type(exc).__name__}: {exc})")

        defined = {
            rel: {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for rel, tree in trees.items()
        }

        symbols: dict[str, Symbol] = {}
        for rel, tree in trees.items():
            from_names, mod_aliases = self._imports(rel, tree, modules, defined, warnings)
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    warnings.append(f"{rel}: class {node.name} not indexed (methods unsupported)")
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sid = f"{rel}::{node.name}"
                    if sid in symbols:
                        warnings.append(f"{rel}: duplicate definition of {node.name} ignored")
                        continue
                    symbols[sid] = self._symbol(rel, node, defined[rel], from_names, mod_aliases, defined)
        return ParseResult(symbols=symbols, warnings=warnings)

    @staticmethod
    def _imports(rel, tree, modules, defined, warnings):
        from_names: dict[str, str] = {}   # local name -> symbol id
        mod_aliases: dict[str, str] = {}  # local alias -> file
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in modules:
                        mod_aliases[a.asname or a.name] = modules[a.name]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                target = modules.get(node.module)
                for a in node.names:
                    if a.name == "*":
                        warnings.append(f"{rel}: star import from {node.module} unsupported")
                    elif target and a.name in defined.get(target, ()):
                        from_names[a.asname or a.name] = f"{target}::{a.name}"
        return from_names, mod_aliases

    @staticmethod
    def _symbol(rel, node, local_defs, from_names, mod_aliases, defined) -> Symbol:
        nested = {
            n.name for n in ast.walk(node)
            if n is not node and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        calls: set[str] = set()
        for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
            fn = call.func
            if isinstance(fn, ast.Name):
                if fn.id in nested:
                    continue
                if fn.id in local_defs:
                    calls.add(f"{rel}::{fn.id}")
                else:
                    calls.add(from_names.get(fn.id, fn.id))
            elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) \
                    and fn.value.id in mod_aliases and fn.attr in defined[mod_aliases[fn.value.id]]:
                calls.add(f"{mod_aliases[fn.value.id]}::{fn.attr}")
            else:
                calls.add(ast.unparse(fn))
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        return Symbol(
            id=f"{rel}::{node.name}", file=rel, name=node.name, kind="function",
            signature=f"{prefix} {node.name}({ast.unparse(node.args)}){ret}",
            start_line=start, end_line=node.end_lineno, calls=sorted(calls),
        )
