"""Import hygiene: the reliability package must only ever be imported as `reliability`.

Importing it as `backend.reliability` too would load a second copy of every module,
so monkeypatching `reliability.executor._crash` would not affect the code under test.
Owner: Rhea.
"""
import ast
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _offending_imports(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "backend.reliability" or a.name.startswith("backend.reliability.")
                   for a in node.names):
                lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            if module == "backend.reliability" or module.startswith("backend.reliability."):
                lines.append(node.lineno)
            elif module == "backend" and any(a.name == "reliability" for a in node.names):
                lines.append(node.lineno)
    return lines


def test_nothing_imports_backend_reliability():
    offenders = []
    for path in BACKEND_DIR.rglob("*.py"):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for lineno in _offending_imports(tree):
            offenders.append(f"{path.relative_to(BACKEND_DIR)}:{lineno}")
    assert offenders == [], f"import `reliability`, not `backend.reliability`: {offenders}"


def test_hygiene_checker_catches_the_bad_forms():
    for source in (
        "import backend.reliability.executor",
        "from backend.reliability import executor",
        "from backend.reliability.executor import _crash",
        "from backend import reliability",
    ):
        assert _offending_imports(ast.parse(source)), source
    for source in ("from reliability import executor", "import reliability.worker",
                   "from backend.agent import graph"):
        assert not _offending_imports(ast.parse(source)), source
