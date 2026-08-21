"""What the platform is allowed to tell you, and how you turn it off.

Phase 9. The phase's exit criterion — every alert individually
disableable — is not one requirement among fifteen: it is the gate. An
alerting system whose switches arrive later is one that has already sent
the message somebody did not want, and no later setting takes that back.
So preferences exist before any alert does.

**On the governing principle.** ``docs/06`` cites ``docs/02`` §3 for "no
unwanted proactive sending". §3 actually says something narrower and
stricter: *collection is read-only — no sending, no group creation, no
acting on the user's behalf*. That rule is about acting **as the user
toward third parties**, and nothing here does: an alert goes to the
workspace's own linked chat, from the platform, about the workspace's own
data. The distinction is worth stating rather than glossed, because a
system that sends is exactly where that principle would be eroded first.

Two default policies, because one would be wrong for half the list:

- **Operational and security alerts default on.** Silence is the failure
  they exist to fix — a collector that stopped, a database filling up.
  Shipping those off by default means the person finds out by noticing
  their collection stopped growing, which is the situation before this
  module existed.
- **Digests and content alerts default off.** A weekly summary nobody
  asked for is proactive sending, and that is the reading of the
  principle that actually binds.

Every type, in both groups, can be switched off individually.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AlertType:
    key: str
    label: str
    description: str
    # Whether it fires unless switched off. See the module docstring for
    # why this is not uniform.
    default_on: bool


# --- operational and security: silence is the failure ----------------------

COLLECTOR_FAILED = AlertType(
    key="collector_failed",
    label="فشل الجامع بالكامل",
    description="كل حسابات الجمع أخفقت في تشغيلة واحدة رغم وجود قنوات نشطة",
    default_on=True,
)
STORAGE_HIGH = AlertType(
    key="storage_high",
    label="اقتراب حدّ التخزين",
    description="حجم قاعدة البيانات اقترب من حدّ الخطة المجانية",
    default_on=True,
)
NEW_DEVICE = AlertType(
    key="new_device",
    label="دخول من جهاز جديد",
    description="تسجيل دخول ناجح من عنوان أو متصفّح لم يُستخدم من قبل",
    default_on=True,
)
BACKUP_RESULT = AlertType(
    key="backup_result",
    label="نتيجة النسخ الاحتياطي",
    description="تأكيد أسبوعي بنجاح النسخة الاحتياطية — تأكيد لا صمت",
    default_on=True,
)

# --- digests and content: proactive, so off until asked for ----------------

WEEKLY_DIGEST = AlertType(
    key="weekly_digest",
    label="ملخّص أسبوعي",
    description="روابط جديدة، روابط ماتت، وقنوات صامتة خلال الأسبوع",
    default_on=False,
)
MONTHLY_DOMAINS = AlertType(
    key="monthly_domains",
    label="ملخّص شهري بأعلى النطاقات",
    description="أكثر عشرة نطاقات جُمعت خلال الشهر",
    default_on=False,
)
ADULT_CONTENT = AlertType(
    key="adult_content",
    label="رابط مصنَّف للبالغين",
    description="تنبيه فوري عند تصنيف رابط جديد ضمن محتوى البالغين",
    default_on=False,
)
UNSTABLE_CATEGORY = AlertType(
    key="unstable_category",
    label="تصنيف متضارب لنطاق",
    description="تصحيحات بشرية متكرّرة على نفس النطاق — مؤشّر قاعدة غير مستقرّة",
    default_on=False,
)

ALERT_TYPES: tuple[AlertType, ...] = (
    COLLECTOR_FAILED,
    STORAGE_HIGH,
    NEW_DEVICE,
    BACKUP_RESULT,
    WEEKLY_DIGEST,
    MONTHLY_DOMAINS,
    ADULT_CONTENT,
    UNSTABLE_CATEGORY,
)

BY_KEY: dict[str, AlertType] = {alert.key: alert for alert in ALERT_TYPES}


def is_known(key: str) -> bool:
    return key in BY_KEY


def default_for(key: str) -> bool:
    """Whether an alert fires when nobody has expressed a preference.

    Unknown keys default to *off*: a typo must not become a new channel
    for unrequested messages.
    """
    alert = BY_KEY.get(key)
    return alert.default_on if alert else False
