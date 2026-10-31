"""Guards the hexagonal dependency rule: dependencies point inward only."""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "willay"

# layer -> package prefixes it must NOT import
FORBIDDEN = {
    "domain": ("willay.application", "willay.ports", "willay.adapters", "willay.api", "fastapi", "httpx", "pydantic"),
    "ports": ("willay.application", "willay.adapters", "willay.api", "fastapi", "httpx"),
    "application": ("willay.adapters", "willay.api", "fastapi", "httpx"),
}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_inner_layers_do_not_import_outer_layers():
    violations = []
    for layer, forbidden in FORBIDDEN.items():
        for file in (PACKAGE / layer).rglob("*.py"):
            for module in imported_modules(file):
                if module.startswith(forbidden):
                    violations.append(f"{file.relative_to(PACKAGE)} imports {module}")
    assert violations == []


def test_domain_uses_only_standard_library_and_itself():
    allowed_roots = {"willay", "__future__", "math", "dataclasses", "datetime", "enum", "typing", "random"}
    offenders = []
    for file in (PACKAGE / "domain").rglob("*.py"):
        for module in imported_modules(file):
            if module.split(".")[0] not in allowed_roots:
                offenders.append(f"{file.name} imports {module}")
    assert offenders == []
