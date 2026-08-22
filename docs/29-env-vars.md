# متغيّرات البيئة — الجدول المركزي

> **مولَّد آلياً. لا تحرّره يدوياً.**
> `python scripts/env_report.py`

الفكرة ٢٣٤. جدول واحد بدل تبعثر المتغيّرات بين `README.md` و`.env.example`
وتعليقات `app/config.py` وملفات `.github/workflows/`.

**لماذا مولَّد لا مكتوب:** الخطة (`docs/06` §٤) تضع هذا البند على المسار
المستمر — «يُحدَّث فور أي سرّ جديد». جدولٌ يُكتب باليد صحيحٌ حتى أوّل commit
بعده، والوعد بتحديثه يدوياً هو بالضبط الوعد الذي لم يُوفَّ به هنا: البند كان
على المسار المستمر ولم يُنشَأ أصلاً.

## الأعمدة

| العمود | معناه |
|---|---|
| **الخدمة** | تقرؤه `app/config.py`، أي تحتاجه الخدمة على Render |
| **الافتراضي** | ما يحدث إن لم يُضبَط. `—` يعني لا افتراضي |
| **مسارات العمل** | ملفات `.github/workflows/` التي تمرّره |
| **`.env.example`** | هل يجده مطوّر محلّي في القالب |
| **سرّ؟** | 🔑 يعني بيانات اعتماد: لا يُسجَّل، ولا يُعاد في استجابة، ولا يُكتب في ملف |

## الجدول

| المتغيّر | الخدمة | الافتراضي | مسارات العمل | `.env.example` | سرّ؟ |
|---|---|---|---|---|---|
| `APP_API_KEY` | — | — | `report-run.yml` | ✅ |  |
| `APP_BASE_URL` | — | — | `report-run.yml`، `smoke.yml` | ✅ |  |
| `APP_NAME` | ✅ | `Link Intelligence Platform` | — | ❌ |  |
| `BCRYPT_ROUNDS` | ✅ | `12` | — | ❌ |  |
| `BOT_TOKEN` | ✅ | — | `monthly-report.yml`، `verify-setup.yml`، `weekly-digest.yml` | ✅ | 🔑 |
| `BOT_WEBHOOK_SECRET` | ✅ | — | `verify-setup.yml` | ✅ | 🔑 |
| `COLLECTOR_MESSAGE_LIMIT` | — | — | — | ✅ |  |
| `COLLECTOR_WORKSPACE_ID` | — | — | `collector.yml`، `monthly-report.yml`، `verify-setup.yml`، `weekly-digest.yml` | ✅ |  |
| `DATABASE_URL` | ✅ | `sqlite:///./local.db` | `backup.yml`، `collector.yml`، `monthly-report.yml`، `prune.yml`، `verify-setup.yml`، `vitality.yml`، `weekly-digest.yml` | ✅ | 🔑 |
| `DB_MAX_OVERFLOW` | ✅ | `10` | — | ❌ |  |
| `DB_POOL_SIZE` | ✅ | `5` | — | ❌ |  |
| `DB_POOL_TIMEOUT_SECONDS` | ✅ | `5` | — | ❌ |  |
| `ENVIRONMENT` | ✅ | `development` | — | ✅ |  |
| `FIELD_ENCRYPTION_KEY` | ✅ | `S7uvgQ59s2Xo-V2u3yZdnqZLxhnienyS6rirAOJ_pnA=` | `collector.yml`، `monthly-report.yml`، `weekly-digest.yml` | ✅ | 🔑 |
| `FIELD_ENCRYPTION_KEY_OLD` | — | — | — | ✅ |  |
| `GROQ_API_KEY` | ✅ | — | `collector.yml`، `verify-setup.yml` | ✅ | 🔑 |
| `GROQ_MODEL` | ✅ | `llama-3.1-8b-instant` | — | ❌ |  |
| `GROQ_QUOTA_ALERT_FRACTION` | ✅ | `0.1` | — | ❌ |  |
| `INVITE_CODE` | ✅ | — | `verify-setup.yml` | ✅ | 🔑 |
| `LOG_LEVEL` | — | — | — | ✅ |  |
| `MAX_ACCOUNTS_PER_WORKSPACE` | ✅ | `5` | — | ❌ |  |
| `PUBLIC_BASE_URL` | ✅ | — | `verify-setup.yml` | ✅ |  |
| `RENDER_GIT_COMMIT` | ✅ | — | — | ❌ |  |
| `RENDER_SERVICE_NAME` | ✅ | — | — | ❌ |  |
| `SECRET_KEY` | ✅ | `dev-secret-key-change-me` | `monthly-report.yml`، `verify-setup.yml`، `weekly-digest.yml` | ✅ | 🔑 |
| `SESSION_TTL_HOURS` | ✅ | `336` | — | ❌ |  |
| `STORAGE_ALERT_FRACTION` | ✅ | `0.8` | — | ❌ |  |
| `STORAGE_LIMIT_BYTES` | ✅ | `1073741824` | — | ❌ |  |
| `TG_API_HASH` | — | — | `collector.yml`، `verify-setup.yml` | ✅ | 🔑 |
| `TG_API_ID` | — | — | `collector.yml`، `verify-setup.yml` | ✅ |  |
| `TG_SESSION_STRING` | — | — | `collector.yml`، `verify-setup.yml` | ✅ | 🔑 |
| `VITALITY_CHECK_BATCH_LIMIT` | — | — | — | ✅ |  |
| `VITALITY_CHECK_CONCURRENCY` | — | — | — | ✅ |  |
| `VITALITY_CHECK_TIMEOUT_SECONDS` | — | — | — | ✅ |  |

## ما لا تتّفق عليه المصادر

كل ما تحتاجه مسارات العمل مذكور في `.env.example`.

**تقرؤه الخدمة ولا يظهر في القالب ولا في أي مسار عمل** — أي يعمل بافتراضيّه
دائماً حتى يقرّر أحد غير ذلك:

- `APP_NAME`
- `BCRYPT_ROUNDS`
- `DB_MAX_OVERFLOW`
- `DB_POOL_SIZE`
- `DB_POOL_TIMEOUT_SECONDS`
- `GROQ_MODEL`
- `GROQ_QUOTA_ALERT_FRACTION`
- `MAX_ACCOUNTS_PER_WORKSPACE`
- `RENDER_GIT_COMMIT`
- `RENDER_SERVICE_NAME`
- `SESSION_TTL_HOURS`
- `STORAGE_ALERT_FRACTION`
- `STORAGE_LIMIT_BYTES`

## قاعدة ثابتة

**لا يُكتب أيّ سرّ في هذا المستودع.** المفاتيح تُضبَط في لوحة Render
(للخدمة) وفي أسرار GitHub Actions (للتشغيلات المجدولة). القيمة الافتراضية
لـ`FIELD_ENCRYPTION_KEY` منشورة في `app/config.py` عمداً، وهي **مفتاح تطوير**
يجعل التشفير زخرفياً إن استُخدم في الإنتاج — استبدلها.
