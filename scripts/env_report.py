"""Regenerate docs/29-env-vars.md: every environment variable, in one table.

    python scripts/env_report.py

Idea 234, which ``docs/06-execution-plan.md`` §4 lists on the *continuous*
track — "updated the moment any new secret appears". It had never been
created, so the thing that was supposed to track every new secret tracked
none of them. Generating it rather than writing it by hand is the fix for
the failure mode that produced that: a hand-kept table is correct exactly
until the next commit.

Three sources, because a variable can appear in any of them and a table
missing one of them is the table nobody trusts:

- ``app/config.py`` — what the running service reads, with its defaults
- ``.github/workflows/*.yml`` — what the scheduled jobs need as secrets
- ``.env.example`` — what a local developer is told to set

**Disagreements between them are the interesting output**, not an error to
smooth over: a variable a workflow needs but ``.env.example`` never
mentions is exactly the one that is unset on the day it matters.
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "29-env-vars.md"

# ${{ secrets.NAME }} and ${{ vars.NAME }} in a workflow file.
_WORKFLOW_REF = re.compile(r"\$\{\{\s*(?:secrets|vars)\.([A-Z0-9_]+)\s*\}\}")
_ENV_EXAMPLE_KEY = re.compile(r"^([A-Z0-9_]+)=", re.MULTILINE)

# Variables that exist only inside CI and never reach the application.
# Listed rather than filtered by pattern so that adding one is a decision.
CI_ONLY = {"GITHUB_TOKEN"}

# Which variables carry a bearer credential. Drives the one column a reader
# actually scans for, and it is stated per variable rather than guessed
# from the name — TG_SESSION_STRING does not contain "KEY" or "TOKEN".
SECRETS = {
    "SECRET_KEY",
    "FIELD_ENCRYPTION_KEY",
    "BOT_TOKEN",
    "BOT_WEBHOOK_SECRET",
    "DATABASE_URL",
    "GROQ_API_KEY",
    "TG_API_HASH",
    "TG_SESSION_STRING",
    "INVITE_CODE",
    "LIP_API_KEY",
}


def _settings_fields() -> dict[str, str]:
    """Every setting the application reads, with its default rendered."""
    out = {}
    for name, field in Settings.model_fields.items():
        default = field.default
        if default is None:
            rendered = "—"
        elif default == "":
            rendered = "*(فارغ)*"
        else:
            rendered = f"`{default}`"
        out[name.upper()] = rendered
    return out


def _workflow_refs() -> dict[str, list[str]]:
    """Which workflows reference which variable."""
    out: dict[str, list[str]] = {}
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for name in sorted(set(_WORKFLOW_REF.findall(path.read_text(encoding="utf-8")))):
            out.setdefault(name, []).append(path.name)
    return out


def _env_example_keys() -> set[str]:
    path = ROOT / ".env.example"
    if not path.exists():
        return set()
    return set(_ENV_EXAMPLE_KEY.findall(path.read_text(encoding="utf-8")))


def build() -> str:
    settings = _settings_fields()
    workflows = _workflow_refs()
    example = _env_example_keys()

    names = sorted(set(settings) | set(workflows) | example)

    lines = [
        "# متغيّرات البيئة — الجدول المركزي",
        "",
        "> **مولَّد آلياً. لا تحرّره يدوياً.**",
        "> `python scripts/env_report.py`",
        "",
        "الفكرة ٢٣٤. جدول واحد بدل تبعثر المتغيّرات بين `README.md` و`.env.example`",
        "وتعليقات `app/config.py` وملفات `.github/workflows/`.",
        "",
        "**لماذا مولَّد لا مكتوب:** الخطة (`docs/06` §٤) تضع هذا البند على المسار",
        "المستمر — «يُحدَّث فور أي سرّ جديد». جدولٌ يُكتب باليد صحيحٌ حتى أوّل commit",
        "بعده، والوعد بتحديثه يدوياً هو بالضبط الوعد الذي لم يُوفَّ به هنا: البند كان",
        "على المسار المستمر ولم يُنشَأ أصلاً.",
        "",
        "## الأعمدة",
        "",
        "| العمود | معناه |",
        "|---|---|",
        "| **الخدمة** | تقرؤه `app/config.py`، أي تحتاجه الخدمة على Render |",
        "| **الافتراضي** | ما يحدث إن لم يُضبَط. `—` يعني لا افتراضي |",
        "| **مسارات العمل** | ملفات `.github/workflows/` التي تمرّره |",
        "| **`.env.example`** | هل يجده مطوّر محلّي في القالب |",
        "| **سرّ؟** | 🔑 يعني بيانات اعتماد: لا يُسجَّل، ولا يُعاد في استجابة، ولا يُكتب في ملف |",
        "",
        "## الجدول",
        "",
        "| المتغيّر | الخدمة | الافتراضي | مسارات العمل | `.env.example` | سرّ؟ |",
        "|---|---|---|---|---|---|",
    ]

    for name in names:
        if name in CI_ONLY:
            continue
        in_app = "✅" if name in settings else "—"
        default = settings.get(name, "—")
        flows = "، ".join(f"`{f}`" for f in workflows.get(name, [])) or "—"
        in_example = "✅" if name in example else "❌"
        secret = "🔑" if name in SECRETS else ""
        lines.append(f"| `{name}` | {in_app} | {default} | {flows} | {in_example} | {secret} |")

    # The disagreements, called out rather than left for the reader to
    # find by comparing columns.
    needed_by_workflows = set(workflows) - CI_ONLY
    missing_from_example = sorted(needed_by_workflows - example)
    app_only = sorted(set(settings) - needed_by_workflows - example)

    lines += ["", "## ما لا تتّفق عليه المصادر", ""]
    if missing_from_example:
        lines += [
            "**يحتاجه مسار عمل ولا يذكره `.env.example`** — وهو بالضبط المتغيّر الذي",
            "يكون غير مضبوط يوم يهمّ:",
            "",
            *(f"- `{name}`" for name in missing_from_example),
            "",
        ]
    else:
        lines += ["كل ما تحتاجه مسارات العمل مذكور في `.env.example`.", ""]

    if app_only:
        lines += [
            "**تقرؤه الخدمة ولا يظهر في القالب ولا في أي مسار عمل** — أي يعمل بافتراضيّه",
            "دائماً حتى يقرّر أحد غير ذلك:",
            "",
            *(f"- `{name}`" for name in app_only),
            "",
        ]

    lines += [
        "## قاعدة ثابتة",
        "",
        "**لا يُكتب أيّ سرّ في هذا المستودع.** المفاتيح تُضبَط في لوحة Render",
        "(للخدمة) وفي أسرار GitHub Actions (للتشغيلات المجدولة). القيمة الافتراضية",
        "لـ`FIELD_ENCRYPTION_KEY` منشورة في `app/config.py` عمداً، وهي **مفتاح تطوير**",
        "يجعل التشفير زخرفياً إن استُخدم في الإنتاج — استبدلها.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    DOC.write_text(build(), encoding="utf-8")
    print(f"wrote {DOC.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
