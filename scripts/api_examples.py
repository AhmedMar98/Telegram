"""Regenerate docs/28-api-examples.md: a runnable curl for every endpoint.

    python scripts/api_examples.py

Idea 240. The interactive OpenAPI page at ``/docs`` is excellent for
exploring and useless for the thing people actually do, which is paste a
command into a terminal and see what comes back. This is the complement,
not a replacement.

**Generated, for the reason every hand-written API doc eventually needs.**
A curl example is a claim about a route, a method, an auth requirement and
a body shape — four things that drift independently. Written by hand they
are correct on the day they are written; derived from the live application
they cannot say anything the application does not.

Three facts per endpoint, each from where it actually lives:

- route, method and body example — ``app.openapi()``
- which credential it accepts — the real dependency tree, the same source
  ``tests/test_auth_boundary.py`` reads, not a guess from the path
- whether it is a browser page rather than an API call — the response
  content type
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.routing import APIRoute  # noqa: E402

from app.deps import get_current_user, get_session_user  # noqa: E402
from app.main import app  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "28-api-examples.md"

BASE = "$BASE"  # a shell variable, so the whole file is paste-able as-is

# Placeholder values for path parameters. Named per parameter rather than
# a single "1" so a pasted command reads as what it is.
PLACEHOLDERS = {
    "link_id": "123",
    "channel_id": "4",
    "session_id": "9",
    "saved_id": "2",
    "key_id": "5",
    "account_id": "3",
    "notification_id": "17",
    "alert_type": "weekly_digest",
    # nosec B105 - a shell variable *name* for the reader to substitute,
    # not a value. The key must stay "secret" because it is the path
    # parameter's name in /telegram/webhook/{secret}.
    "secret": "$BOT_WEBHOOK_SECRET",  # nosec B105
}

SESSION_ONLY = "🍪 جلسة فقط"
EITHER = "🔑 مفتاح أو جلسة"
PUBLIC = "— بلا مصادقة"
PAGE = "🌐 صفحة متصفّح — تُعيد التوجيه إلى `/login` بلا جلسة"
# nosec B105 - a label rendered in a table, not a credential.
PATH_SECRET = "🤖 السرّ في المسار نفسه، لا ترويسة"  # nosec B105

# Listed rather than inferred from the response class. Inference here would
# be a third mechanism that can disagree with the other two, and there are
# five of them.
BROWSER_PAGES = {"/", "/login", "/register", "/dashboard"}
SECRET_IN_PATH = {"/telegram/webhook/{secret}"}


def _routes() -> list[APIRoute]:
    def walk(router) -> list[APIRoute]:
        found: list[APIRoute] = []
        for route in getattr(router, "routes", []):
            if isinstance(route, APIRoute):
                found.append(route)
            inner = getattr(route, "original_router", None)
            if inner is not None:
                found.extend(walk(inner))
        return found

    return walk(app)


def _openapi_path(route_path: str) -> str:
    """A route's path as OpenAPI spells it.

    FastAPI keeps the converter — ``/auth/api-keys/{key_id:int}`` — while
    the generated schema strips it to ``{key_id}``. Looking a route up by
    the schema's spelling therefore *misses*, and a miss here used to fall
    back to "no authentication", which is the single most dangerous thing
    this document could say about an endpoint. It said exactly that about
    DELETE /auth/api-keys/{key_id} before this function existed.
    """
    return re.sub(r"\{([^}:]+):[^}]+\}", r"{\1}", route_path)


def _auth_of(route: APIRoute) -> str:
    def flatten(dep):
        yield dep.call
        for sub in dep.dependencies:
            yield from flatten(sub)

    calls = set(flatten(route.dependant))
    if get_session_user in calls:
        return SESSION_ONLY
    if get_current_user in calls:
        return EITHER
    return PUBLIC


def _body_example(operation: dict) -> dict | None:
    """The request body example the schema already carries, if any."""
    content = (operation.get("requestBody") or {}).get("content", {})
    schema = content.get("application/json", {}).get("schema", {})
    ref = schema.get("$ref")
    if not ref:
        return None
    name = ref.rsplit("/", 1)[-1]
    definition = app.openapi().get("components", {}).get("schemas", {}).get(name, {})
    examples = definition.get("examples") or ([definition["example"]] if "example" in definition else [])
    return examples[0] if examples else None


def _fill(path: str) -> str:
    for name, value in PLACEHOLDERS.items():
        path = path.replace("{" + name + "}", value)
        path = path.replace("{" + name + ":int}", value)
    return path


def _curl(method: str, path: str, auth: str, body: dict | None) -> str:
    parts = [f"curl -sS -X {method} \\"]
    parts.append(f'  "{BASE}{_fill(path)}" \\')
    if auth == EITHER:
        parts.append('  -H "Authorization: Bearer $LIP_API_KEY" \\')
    elif auth == SESSION_ONLY:
        parts.append("  -b cookies.txt \\")
    if body is not None:
        parts.append('  -H "Content-Type: application/json" \\')
        parts.append(f"  -d '{json.dumps(body, ensure_ascii=False)}'")
    else:
        parts[-1] = parts[-1].rstrip(" \\")
    return "\n".join(parts)


def build() -> str:
    spec = app.openapi()
    by_path = {_openapi_path(route.path): route for route in _routes()}

    # A path in the schema with no route behind it means the lookup above
    # is wrong again, and the failure mode is silent under-reporting of
    # authentication. Refuse to write the file rather than write that.
    unmatched = sorted(set(spec["paths"]) - set(by_path))
    if unmatched:
        raise SystemExit(f"error: no route matched these OpenAPI paths: {unmatched}")

    lines = [
        "# أمثلة `curl` — لكل نقطة، أمر جاهز للّصق",
        "",
        "> **مولَّد آلياً. لا تحرّره يدوياً.**",
        "> `python scripts/api_examples.py`",
        "",
        "الفكرة ٢٤٠. صفحة OpenAPI التفاعلية على `/docs` ممتازة للاستكشاف، وعديمة",
        "الفائدة للشيء الذي يفعله الناس فعلاً: لصق أمر في طرفية ورؤية ما يعود.",
        "هذه مكمّلة لها لا بديل عنها.",
        "",
        "## قبل أي أمر",
        "",
        "```bash",
        'BASE="https://your-service.onrender.com"',
        "```",
        "",
        "**الأوامر المعلَّمة 🍪 تحتاج جلسة متصفّح**، وهي تُنشَأ بتسجيل دخول يحفظ",
        "الكعكة في ملف:",
        "",
        "```bash",
        "curl -sS -c cookies.txt -X POST \\",
        '  "$BASE/auth/login" \\',
        '  -H "Content-Type: application/json" \\',
        '  -d \'{"email": "you@example.com", "password": "..."}\'',
        "```",
        "",
        "**الأوامر المعلَّمة 🔑 تقبل مفتاح API** تُنشئه من اللوحة، وهو الشكل",
        "المناسب لسكربت أو تشغيلة مجدولة:",
        "",
        "```bash",
        'export LIP_API_KEY="lipk_..."',
        "```",
        "",
        "**ولا نقطة معلَّمة 🍪 تقبل مفتاحاً.** ذلك ليس سهواً: مفتاحٌ يستطيع تعطيل",
        "التنبيهات أو إصدار مفاتيح أو قراءة سجلّ أجهزتك يكون تسريبه أخطر بكثير مما",
        "صُمِّم له. التفصيل في `docs/16-api-policy.md`.",
        "",
        "**«بلا مصادقة» تعني ثلاثة أشياء مختلفة**، ولذلك تُميَّز:",
        "",
        "| الوسم | ماذا يعني |",
        "|---|---|",
        f"| {PUBLIC} | مفتوحة فعلاً: تسجيل الدخول والتسجيل وفحوص الحياة |",
        f"| {PAGE} | صفحة HTML لا نقطة API. بلا جلسة تُعيد التوجيه، لا تعرض بيانات |",
        f"| {PATH_SECRET} | تلغرام يستدعيها، والسرّ جزء من المسار — فلا ترويسة تحملها |",
        "",
    ]

    # Grouped by the tag FastAPI already assigns, so the order matches /docs.
    groups: dict[str, list[tuple[str, str, dict]]] = {}
    for path, methods in spec["paths"].items():
        for method, operation in methods.items():
            tag = (operation.get("tags") or ["عام"])[0]
            groups.setdefault(tag, []).append((path, method.upper(), operation))

    for tag in sorted(groups):
        lines += [f"## {tag}", ""]
        for path, method, operation in sorted(groups[tag]):
            auth = _auth_of(by_path[path])
            if path in BROWSER_PAGES:
                auth = PAGE
            elif path in SECRET_IN_PATH:
                auth = PATH_SECRET
            summary = operation.get("summary") or ""
            lines += [
                f"### `{method} {path}`",
                "",
                f"{summary}  " if summary else "",
                f"**المصادقة:** {auth}",
                "",
                "```bash",
                _curl(method, path, auth, _body_example(operation)),
                "```",
                "",
            ]

    lines += [
        "## ما لا يظهر هنا",
        "",
        "قيم معرّفات المسار (`123`، `4`، ...) نائبة عن معرّفات حقيقية تحصل عليها من",
        "استجابة القائمة المقابلة. ومعرّف من مساحة عمل أخرى يردّ **٤٠٤ لا ٤٠٣**",
        "عمداً: الثاني يؤكّد أنّ المعرّف موجود، وهو بالضبط ما يبحث عنه من يجرّب",
        "معرّفات بالتسلسل.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    DOC.write_text(build(), encoding="utf-8")
    print(f"wrote {DOC.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
