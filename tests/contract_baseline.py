"""Frozen-structure comparison against the `main` baseline.

Upstream modules stay frozen at the AST level. The only literals that may differ from `main` are
docstrings and string constants containing non-ASCII text: Field descriptions, prompt copy, display
labels and Chinese-only vocabulary sequences (offline rule fixtures), which are business-scenario wording. Everything else must be identical: imports,
classes, signatures, fields, defaults, control flow, numeric constants, and every ASCII literal
(enum wire values, SQL, regexes, keys, identifiers). Version tags may change only through the
explicit VERSION_BUMPS allowlist, and each listed tag must actually differ from `main`, so a
wording change cannot ship without its version bump. Safety keyword sets listed in MONOTONIC_SETS
may only grow: every baseline keyword must still be present (guardrails are never weakened).
"""
from __future__ import annotations

import ast
from pathlib import Path
import string
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BASELINE_REF = "main"

VERSION_BUMPS: dict[str, dict[str, object]] = {
    # Prompt copy was re-skinned for the e-commerce scenario (prompts.py: any text change bumps PROMPT_VERSION).
    "agent_matrix/store_sop/prompts.py": {"PROMPT_VERSION": "store_sop.v2"},
    # Metric display names / aliases changed (semantic_layer.py: any metric change bumps catalog_version).
    "agent_matrix/supply_chain_bi/semantic_layer.py": {"catalog_version": 2},
}

MONOTONIC_SETS: dict[str, set[str]] = {
    "agent_matrix/store_sop/prompts.py": {"SAFETY_HAZARD_KEYWORDS"},
}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def baseline_source(path: str) -> str:
    return git("show", f"{BASELINE_REF}:{path}")


def baseline_files(*directories: str) -> list[str]:
    return [p for p in git("ls-tree", "-r", "--name-only", BASELINE_REF, "--", *directories).splitlines() if p]


def current_files(*directories: str) -> list[str]:
    tracked = git("ls-files", "--", *directories).splitlines()
    untracked = git("ls-files", "--others", "--exclude-standard", "--", *directories).splitlines()
    return sorted({p for p in tracked + untracked if p and (ROOT / p).exists()})


class _Normalize(ast.NodeTransformer):
    def __init__(self, bumps: dict[str, object], monotonic: set[str] = frozenset()):
        self.bumps = bumps
        self.monotonic = monotonic

    def _docstring(self, node):
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body[0] = ast.copy_location(ast.Expr(value=ast.Constant(value="<doc>")), body[0])
        return self.generic_visit(node)

    visit_Module = visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _docstring

    def visit_Constant(self, node: ast.Constant):
        if isinstance(node.value, str) and not node.value.isascii():
            return ast.copy_location(ast.Constant(value="<copy>"), node)
        return node

    def _sequence(self, node):
        self.generic_visit(node)
        if node.elts and all(isinstance(e, ast.Constant) and e.value == "<copy>" for e in node.elts):
            node.elts = [ast.Constant(value="<copy-seq>")]
        return node

    visit_Tuple = visit_List = visit_Set = _sequence

    def _assigned(self, node, names):
        if len(names) == 1 and names[0] in self.bumps:
            node.value = ast.Constant(value="<version>")
        elif len(names) == 1 and names[0] in self.monotonic:
            node.value = ast.Constant(value="<monotonic-set>")
        return node

    def visit_Assign(self, node: ast.Assign):
        self.generic_visit(node)
        return self._assigned(node, [t.id for t in node.targets if isinstance(t, ast.Name)])

    def visit_AnnAssign(self, node: ast.AnnAssign):
        self.generic_visit(node)
        return self._assigned(node, [node.target.id] if isinstance(node.target, ast.Name) else [])

    def visit_keyword(self, node: ast.keyword):
        self.generic_visit(node)
        if node.arg in self.bumps:
            node.value = ast.Constant(value="<version>")
        return node


def string_set(source: str, name: str) -> set[str]:
    for node in ast.parse(source).body:
        target = node.target if isinstance(node, ast.AnnAssign) else node.targets[0] if isinstance(node, ast.Assign) else None
        if isinstance(target, ast.Name) and target.id == name:
            return {n.value for n in ast.walk(node.value) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    raise AssertionError(f"{name} not found")


def _bumped_values(tree: ast.AST, names: set[str]) -> dict[str, object]:
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in names and isinstance(node.value, ast.Constant):
                found[node.targets[0].id] = node.value.value
        elif isinstance(node, ast.keyword) and node.arg in names and isinstance(node.value, ast.Constant):
            found[node.arg] = node.value.value
    return found


def structure(source: str, path: str = "") -> str:
    """AST dump with non-ASCII copy and declared version tags blanked out."""
    return ast.dump(parse_normalized(source, path))


def parse_normalized(source: str, path: str = "") -> ast.AST:
    return _Normalize(VERSION_BUMPS.get(path, {}), MONOTONIC_SETS.get(path, set())).visit(ast.parse(source))


def assert_structure_frozen(path: str) -> None:
    before, after = baseline_source(path), (ROOT / path).read_text(encoding="utf-8")
    assert structure(before, path) == structure(after, path), f"{path}: code structure or ASCII literal changed"
    bumps = VERSION_BUMPS.get(path)
    if bumps and before != after:
        old = _bumped_values(ast.parse(before), set(bumps))
        new = _bumped_values(ast.parse(after), set(bumps))
        for name, expected in bumps.items():
            assert new.get(name) == expected and old.get(name) != expected, f"{path}: {name} must be bumped to {expected!r}"
    for name in MONOTONIC_SETS.get(path, ()):
        missing = string_set(before, name) - string_set(after, name)
        assert not missing, f"{path}: {name} weakened, removed {sorted(missing)}"


def assert_python_tree_frozen(*directories: str) -> None:
    """Same .py file set as the baseline, and every module structurally identical to it."""
    before = [p for p in baseline_files(*directories) if p.endswith(".py")]
    after = [p for p in current_files(*directories) if p.endswith(".py")]
    assert sorted(before) == after, sorted(set(before) ^ set(after))
    for path in before:
        assert_structure_frozen(path)


def format_fields(text: str) -> list[str]:
    return sorted({field for _, field, _, _ in string.Formatter().parse(text) if field is not None})
