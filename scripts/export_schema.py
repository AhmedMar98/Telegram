"""Generate the documented export format from the code that produces it.

Idea 173: external integrations need a written contract for the export
JSON/CSV. A hand-written one is a wish — it describes the format on the
day it was typed. This reads ``EXPORT_COLUMNS`` and the row builder in
``app.routers.links``, so the document cannot describe a format the code
does not emit, and a test fails the build when it goes stale.

    python scripts/export_schema.py           # rewrite docs/18-export-format.md
    python scripts/export_schema.py --check   # exit 1 if it would change
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.routers.links import EXPORT_COLUMNS  # noqa: E402

DOC_PATH = Path(__file__).resolve().parent.parent / "docs" / "18-export-format.md"

# What each column means, and — where it matters — what it does *not*.
# Keyed by column so a new column with no description fails generation
# rather than shipping an undocumented field.
DESCRIPTIONS: dict[str, str] = {
    "url": "الرابط كما استُخرج، بعد تجريد الترقيم اللاحق. `http`/`https` فقط",
    "category": "التصنيف النهائي. القيم المعروفة في `/config/categories`",
    "confidence": "ثقة التصنيف، ٠٫٠٠–١٫٠٠، مقرّبة لخانتين",
    "classified_by": "`rules` أو `llm:groq` أو `manual` (تصحيح بشري)",
    "matched_rule": "القاعدة التي طابقت، مثل `domain:github.com`. فارغ للصفوف الأقدم من العمود",
    "source_type": "**أين** وُجد الرابط داخل الرسالة: `text` (نصّ ظاهر) أو `hyperlink` (رابط خلف تسمية) أو `button` (زرّ لوحة مضمَّنة). ليس مصدر الرابط — المصدر هو القناة",
    "forwarded_from": "مصدر إعادة التوجيه إن وُجد",
    "language": "`ar` أو `en` أو فارغ إن تعذّر الحسم",
    "is_favorite": "علامة المستخدم",
    "domain": "نطاق الرابط، مشتقّ لا مُدخل",
    "posted_at": "متى نُشرت الرسالة الأصلية. **فارغ للإضافة اليدوية** — لا وقت نشر لها",
    "collected_at": "متى دخل الصفّ هذه القاعدة. **هذا ما يرشّحه `since`/`until`**",
    "is_alive": "آخر نتيجة فحص حيوية. `null` تعني **لم يُفحَص**، لا «ميت»",
    "status_category": "تصنيف ثابت مشتقّ من الحالة: `ok`/`redirect`/`blocked`/`missing`/`throttled`/`server_error`/`unreachable`/`unchecked`",
    "http_status": "رمز HTTP من آخر فحص، أو فارغ",
    "last_checked_at": "آخر محاولة فحص، ناجحة كانت أم لا",
    "last_alive_at": "آخر مرّة استجاب فيها الرابط فعلاً",
    "is_archived": "مؤرشف. **التصدير يشمل المؤرشف دائماً** — التصدير عن الاكتمال",
    "context": "نصّ الرسالة المحيط، مقطوع عند ٣٠٠ حرف",
}

HEADER = """# صيغة التصدير (مولَّدة آلياً)

> لا تحرّر هذا الملف يدوياً. أعد توليده بـ:
> `python scripts/export_schema.py`

الفكرة ١٧٣. عقد مكتوب لمن يبني تكاملاً فوق `export.json` أو `export.csv`.
**مولَّد من `EXPORT_COLUMNS` نفسها**، فلا يستطيع وصف صيغة لا يُنتجها الكود،
واختبار يفشل إن صار قديماً.

## ما يشترك فيه الشكلان

`export.json` مصفوفة كائنات، و`export.csv` صفوف بترويسة — **بالحقول نفسها
وبالترتيب نفسه**، لأن الاثنين مشتقّان من الدالة ذاتها. الفرق الوحيد أن CSV
بلا `null`: القيمة الغائبة خانة فارغة.

كلاهما **يُبثّ صفاً صفاً** بلا حدّ أعلى مفروض (`docs/16` §٧).

## الحقول

| الحقل | المعنى |
|---|---|
"""

FOOTER = """
## الترشيح

| المعامل | الأثر |
|---|---|
| `q` | بحث نصّي. **`export.md` وحده يحترمه** بالإضافة إلى `csv`/`json` |
| `category` | تصنيف واحد أو عدّة مفصولة بفواصل |
| `since` / `until` | نافذة على `collected_at` بالتاريخ (`YYYY-MM-DD`). شاملة للطرفين — رابط جُمع في يوم `until` **يظهر** |

## ما ليس عهداً

ترتيب الصفوف عند تساوي `collected_at`، وصياغة `context`. أمّا **أسماء
الحقول وأنواعها فعهد** تحكمه `docs/16-api-policy.md`: الإضافة آمنة، والحذف
أو تغيير النوع كاسر.
"""


def render() -> str:
    missing = [column for column in EXPORT_COLUMNS if column not in DESCRIPTIONS]
    if missing:
        sys.exit(f"error: export column(s) with no description: {', '.join(missing)}")
    rows = "".join(f"| `{column}` | {DESCRIPTIONS[column]} |\n" for column in EXPORT_COLUMNS)
    return HEADER + rows + FOOTER


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit 1 if the document is out of date")
    args = parser.parse_args()

    generated = render()
    if args.check:
        current = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else ""
        if current != generated:
            print(f"{DOC_PATH.name} is stale — run: python scripts/export_schema.py")
            return 1
        print(f"{DOC_PATH.name} is current")
        return 0

    DOC_PATH.write_text(generated, encoding="utf-8")
    print(f"wrote {DOC_PATH.relative_to(DOC_PATH.parent.parent)} ({len(EXPORT_COLUMNS)} columns)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
