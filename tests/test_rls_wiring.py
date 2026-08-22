"""Does anything that reads a protected table remember to say which tenant?

This file runs on SQLite in every CI run, unlike ``tests/test_rls.py``
which needs a real Postgres and skips without one. It cannot check that
row-level security *works* — SQLite has none — so it checks the thing that
can be checked anywhere and is the actual source of the bug: whether the
code that reads those tables ever tells the database who it is asking for.

That failure is invisible by construction. A scheduled job that forgets
reads **zero rows** and exits 0, so it looks like a healthy run over an
empty week. Measured on the real thing: with the scoping removed,
``scripts/weekly_digest.py`` still printed a digest — just silently
missing the entire channels section.

The guard is intentionally coarse. It does not try to prove a script is
correct; it proves a script that touches this data was written by someone
who had to think about tenancy, because leaving the call out is what
turns the failure silent.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app import models
from app.rls import BOOTSTRAP_TABLES, PROTECTED_TABLES, UNPROTECTED_BY_DESIGN

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"

SCOPING_CALL = "scope_session_to_workspace"

# Scripts that legitimately read a protected table without scoping. Each
# entry needs a reason, and the reason has to survive being read out loud.
EXEMPT: dict[str, str] = {}


def _model_names_for(tables: tuple[str, ...]) -> set[str]:
    """ORM class names whose ``__tablename__`` is in ``tables``."""
    found = set()
    for name in dir(models):
        obj = getattr(models, name)
        table = getattr(obj, "__tablename__", None)
        if isinstance(table, str) and table in tables:
            found.add(name)
    return found


PROTECTED_MODELS = _model_names_for(PROTECTED_TABLES)


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


def _script_paths() -> list[Path]:
    return sorted(p for p in SCRIPTS.glob("*.py") if p.name != "__init__.py")


def test_the_model_lookup_actually_found_something():
    """Guards every assertion below.

    If ``PROTECTED_MODELS`` came back empty — a renamed table, a moved
    model — the parametrised test underneath would collect nothing and
    report a clean pass over zero scripts. That is the failure mode this
    whole file exists to prevent, so it must not be this file's own.
    """
    assert PROTECTED_MODELS, "no ORM classes matched PROTECTED_TABLES — the guard below checks nothing"
    assert len(PROTECTED_MODELS) == len(PROTECTED_TABLES), (
        f"{len(PROTECTED_TABLES)} protected tables but {len(PROTECTED_MODELS)} models: {sorted(PROTECTED_MODELS)}"
    )


@pytest.mark.parametrize("path", _script_paths(), ids=lambda p: p.name)
def test_a_script_reading_protected_data_names_its_tenant(path: Path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    touched = _imported_names(tree) & PROTECTED_MODELS
    if not touched:
        return

    if path.name in EXEMPT:
        return

    assert SCOPING_CALL in source, (
        f"{path.name} imports {sorted(touched)} — tables under row-level security — but never calls "
        f"{SCOPING_CALL}(). Under RLS it will read zero rows and exit successfully, reporting nothing "
        f"as though there were nothing to report. Scope it, or add it to EXEMPT with a reason."
    )


def test_the_three_table_lists_do_not_overlap():
    """One table cannot be both protected and deliberately unprotected.

    The lists in app/rls.py are documentation as much as configuration —
    tests/test_rls.py asserts the database matches them in both
    directions. A table appearing twice would make one of those claims
    unfalsifiable.
    """
    groups = {
        "protected": set(PROTECTED_TABLES),
        "unprotected_by_design": set(UNPROTECTED_BY_DESIGN),
        "bootstrap": set(BOOTSTRAP_TABLES),
    }
    for a, b in (
        ("protected", "unprotected_by_design"),
        ("protected", "bootstrap"),
        ("unprotected_by_design", "bootstrap"),
    ):
        assert not groups[a] & groups[b], f"{a} and {b} both list {sorted(groups[a] & groups[b])}"


def test_every_listed_table_is_a_real_table():
    """A typo in any of the three lists would silently protect nothing."""
    real = {
        getattr(models, n).__tablename__
        for n in dir(models)
        if isinstance(getattr(getattr(models, n, None), "__tablename__", None), str)
    }
    for group in (PROTECTED_TABLES, UNPROTECTED_BY_DESIGN, BOOTSTRAP_TABLES):
        for table in group:
            assert table in real, f"{table!r} is listed in app/rls.py but no model declares it"
