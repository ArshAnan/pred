"""Import rules from LLD section 3. Parses source; it does not execute it."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_ENGINE = {
    "src/airscen/trust.py",
    "src/airscen/factors.py",
    "src/airscen/scenario.py",
}
_SERVICE = {
    "airscen.types",
    "airscen.store",
    "airscen.llm",
    "airscen.extract",
    "airscen.scout",
    "airscen.trust",
    "airscen.factors",
    "airscen.scenario",
    "airscen.brief",
}


def _package_parts(rel: str) -> tuple[str, ...]:
    rel = rel.replace("\\", "/")
    if rel.startswith("src/"):
        rel = rel[4:]
    parts = rel.split("/")
    if parts and parts[-1].endswith(".py"):
        parts = parts[:-1]
    return tuple(part for part in parts if part)


def _imported(tree: ast.AST, package: tuple[str, ...]) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                if node.module:
                    found.append(".".join((*base, *node.module.split("."))))
                else:
                    found.extend(".".join((*base, alias.name)) for alias in node.names)
            elif node.module == "airscen":
                found.extend(f"airscen.{alias.name}" for alias in node.names)
            elif node.module:
                found.append(node.module)
    return found


def _uses_sqlite(modules: list[str], tree: ast.AST) -> bool:
    if any(name == "sqlite3" or name.startswith("sqlite3.") for name in modules):
        return True
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "connect":
            continue
        value = node.func.value
        if isinstance(value, ast.Name) and value.id == "sqlite3":
            return True
    return False


def _allowed_airscen(rel: str) -> set[str] | None:
    rel = rel.replace("\\", "/")
    if rel == "src/airscen/types.py":
        return set()
    if rel in _ENGINE:
        return {"airscen.types"}
    if "/adapters/" in f"/{rel}":
        return {"airscen.types", "airscen.adapters.http"}
    if rel == "src/airscen/service.py":
        return set(_SERVICE)
    if rel == "src/airscen/api.py":
        # The lifespan calls load_settings(); that function lives in config.
        return {"airscen.service", "airscen.store", "airscen.types", "airscen.config"}
    if rel == "src/airscen/brief.py":
        return {"airscen.types", "airscen.llm"}
    if rel == "app.py":
        # Streamlit reaches the API only through the typed client (DD-1).
        return {"airscen.types", "airscen.ui.client"}
    if rel.startswith("src/airscen/ui/"):
        return {"airscen.types"}
    return None


def check_source(relpath: str, source: str) -> list[str]:
    """Return the import-rule violations in one module."""
    rel = relpath.replace("\\", "/")
    tree = ast.parse(source)
    modules = _imported(tree, _package_parts(rel))
    errors = []
    if _uses_sqlite(modules, tree) and rel != "src/airscen/store.py":
        errors.append(f"{rel} opens sqlite outside store.py")
    allowed = _allowed_airscen(rel)
    if allowed is not None:
        extra = sorted(
            name for name in modules
            if (name == "airscen" or name.startswith("airscen.")) and name not in allowed
        )
        if extra:
            errors.append(f"{rel} imports {extra}")
    if rel in _ENGINE:
        for name in modules:
            top = name.split(".", 1)[0]
            if top in {"airscen", "numpy"} or top in sys.stdlib_module_names:
                continue
            errors.append(f"{rel} imports non-stdlib module {name}")
    return errors


def _sources():
    app = ROOT / "app.py"
    yield "app.py", app.read_text()
    for path in sorted((ROOT / "src" / "airscen").rglob("*.py")):
        yield path.relative_to(ROOT).as_posix(), path.read_text()


def test_import_rules_hold_on_the_tree():
    errors = []
    for rel, source in _sources():
        errors.extend(check_source(rel, source))
    assert errors == []


def test_scenario_cannot_import_store():
    assert check_source("src/airscen/scenario.py", "import airscen.store\n")
    assert check_source("src/airscen/scenario.py", "from .store import Repository\n")
    assert check_source("src/airscen/scenario.py", "from airscen import store\n")


def test_api_and_ui_stay_off_the_engine_and_the_store():
    assert check_source("src/airscen/api.py", "from airscen.scenario import run_scenarios\n")
    assert check_source("src/airscen/api.py", "from airscen.adapters.kalshi import Kalshi\n")
    assert check_source("app.py", "import airscen.store\n")
    assert check_source("src/airscen/ui/client.py", "from airscen.config import Settings\n")
    assert check_source("src/airscen/trust.py", "import pydantic\n")
