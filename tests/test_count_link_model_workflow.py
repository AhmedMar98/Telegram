"""The diagnostic that answers "how big is the links/resources gap?"

Pins two things the diagnostic depends on staying true: it never fires on
its own (a diagnostic that runs on every push is a diagnostic that has
become a job someone has to explain), and it never reads more than the
counts it needs — no row content, no other secret.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github/workflows/count-link-model.yml"
SCRIPT = REPO_ROOT / "scripts/count_link_model_tables.py"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_diagnostic_only_runs_when_asked():
    """No schedule, no push trigger. A one-off diagnostic that fired on
    every commit would be indistinguishable from a monitoring job nobody
    decided to add."""
    triggers = _workflow()[True]  # PyYAML parses the bare key `on` as True

    assert set(triggers) == {"workflow_dispatch"}


def test_the_diagnostic_cannot_write_to_the_repository():
    assert _workflow()["permissions"] == {"contents": "read"}


def test_the_diagnostic_reads_only_the_database_secret():
    """DATABASE_URL only — nothing else this repository treats as a
    secret is in scope for a table-counting question."""
    steps = _workflow()["jobs"]["count"]["steps"]
    run_step = next(step for step in steps if step.get("name", "").startswith("Count links"))

    env = run_step.get("env", {})
    secret_refs = {v for v in env.values() if isinstance(v, str) and "secrets." in v}

    assert secret_refs == {"${{ secrets.DATABASE_URL }}"}


def test_the_script_never_selects_row_content():
    """Every query is COUNT(*) or MAX(created_at)/an identity probe —
    never a column that could carry a URL, a name, or a secret.

    Checked against the code, not the module docstring: the docstring
    explains the diagnostic in terms of the very tables and the risk it
    guards against, so it legitimately says "URL" and "resources". What
    must never appear is a SELECT of an actual content column.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    marker = '"""\n\nfrom __future__'
    assert marker in text, "the module docstring boundary moved; update this test's marker"
    code = text.split(marker, 1)[1]

    assert "SELECT *" not in code
    # "url_hash" and "fingerprint" are identity columns, not content, and
    # are the whole point of the join — deliberately not on this list.
    # Excludes "password" and "webhook": neither is a column on links,
    # resources or occurrences, so forbidding the word would only ever
    # catch the DSN-handling comment this script shares with
    # backup_full_dump.py and verify_migration.py, not a real read.
    for forbidden in ("representative_url", "raw_text", "domain"):
        assert forbidden not in code.lower(), (
            f"the script's code references {forbidden!r}, which is row content and does not "
            "belong in a count-only diagnostic"
        )
