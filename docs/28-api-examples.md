# أمثلة `curl` — لكل نقطة، أمر جاهز للّصق

> **مولَّد آلياً. لا تحرّره يدوياً.**
> `python scripts/api_examples.py`

الفكرة ٢٤٠. صفحة OpenAPI التفاعلية على `/docs` ممتازة للاستكشاف، وعديمة
الفائدة للشيء الذي يفعله الناس فعلاً: لصق أمر في طرفية ورؤية ما يعود.
هذه مكمّلة لها لا بديل عنها.

## قبل أي أمر

```bash
BASE="https://your-service.onrender.com"
```

**الأوامر المعلَّمة 🍪 تحتاج جلسة متصفّح**، وهي تُنشَأ بتسجيل دخول يحفظ
الكعكة في ملف:

```bash
curl -sS -c cookies.txt -X POST \
  "$BASE/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com", "password": "..."}'
```

**الأوامر المعلَّمة 🔑 تقبل مفتاح API** تُنشئه من اللوحة، وهو الشكل
المناسب لسكربت أو تشغيلة مجدولة:

```bash
export LIP_API_KEY="lipk_..."
```

**ولا نقطة معلَّمة 🍪 تقبل مفتاحاً.** ذلك ليس سهواً: مفتاحٌ يستطيع تعطيل
التنبيهات أو إصدار مفاتيح أو قراءة سجلّ أجهزتك يكون تسريبه أخطر بكثير مما
صُمِّم له. التفصيل في `docs/16-api-policy.md`.

**«بلا مصادقة» تعني ثلاثة أشياء مختلفة**، ولذلك تُميَّز:

| الوسم | ماذا يعني |
|---|---|
| — بلا مصادقة | مفتوحة فعلاً: تسجيل الدخول والتسجيل وفحوص الحياة |
| 🌐 صفحة متصفّح — تُعيد التوجيه إلى `/login` بلا جلسة | صفحة HTML لا نقطة API. بلا جلسة تُعيد التوجيه، لا تعرض بيانات |
| 🤖 السرّ في المسار نفسه، لا ترويسة | تلغرام يستدعيها، والسرّ جزء من المسار — فلا ترويسة تحملها |

## auth

### `GET /auth/api-keys`

List My Api Keys  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X GET \
  "$BASE/auth/api-keys" \
  -b cookies.txt
```

### `POST /auth/api-keys`

Create My Api Key  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X POST \
  "$BASE/auth/api-keys" \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{"name": "obsidian sync script"}'
```

### `DELETE /auth/api-keys/{key_id}`

Revoke My Api Key  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X DELETE \
  "$BASE/auth/api-keys/5" \
  -b cookies.txt
```

### `POST /auth/change-password`

Change Password  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X POST \
  "$BASE/auth/change-password" \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{"current_password": "correct-horse-battery-staple", "new_password": "a-different-long-one"}'
```

### `POST /auth/login`

Login  
**المصادقة:** — بلا مصادقة

```bash
curl -sS -X POST \
  "$BASE/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email": "sara@example.com", "password": "correct-horse-battery-staple"}'
```

### `POST /auth/logout`

Logout  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X POST \
  "$BASE/auth/logout" \
  -b cookies.txt
```

### `POST /auth/logout-all`

Logout All  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X POST \
  "$BASE/auth/logout-all" \
  -b cookies.txt
```

### `GET /auth/me`

Me  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X GET \
  "$BASE/auth/me" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `POST /auth/me/delete`

Delete My Workspace  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X POST \
  "$BASE/auth/me/delete" \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{"confirm": "DELETE", "current_password": "correct-horse-battery-staple"}'
```

### `GET /auth/me/export`

Export My Data  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X GET \
  "$BASE/auth/me/export" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `GET /auth/me/security-export`

Export Security Log  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X GET \
  "$BASE/auth/me/security-export" \
  -b cookies.txt
```

### `GET /auth/me/summary`

Me Summary  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X GET \
  "$BASE/auth/me/summary" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `POST /auth/register`

Register  
**المصادقة:** — بلا مصادقة

```bash
curl -sS -X POST \
  "$BASE/auth/register" \
  -H "Content-Type: application/json" \
  -d '{"email": "sara@example.com", "password": "correct-horse-battery-staple", "workspace_name": "Research links"}'
```

### `GET /auth/security-activity`

Security Activity  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X GET \
  "$BASE/auth/security-activity" \
  -b cookies.txt
```

### `GET /auth/sessions`

List Sessions  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X GET \
  "$BASE/auth/sessions" \
  -b cookies.txt
```

### `DELETE /auth/sessions/{session_id}`

Revoke Session Endpoint  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X DELETE \
  "$BASE/auth/sessions/9" \
  -b cookies.txt
```

### `GET /auth/totp`

Totp Status  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X GET \
  "$BASE/auth/totp" \
  -b cookies.txt
```

### `POST /auth/totp/disable`

Totp Disable  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X POST \
  "$BASE/auth/totp/disable" \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{"current_password": "correct-horse-battery-staple"}'
```

### `POST /auth/totp/enable`

Totp Enable  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X POST \
  "$BASE/auth/totp/enable" \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{"code": "123456"}'
```

### `POST /auth/totp/recovery-codes`

Totp Regenerate Recovery  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X POST \
  "$BASE/auth/totp/recovery-codes" \
  -b cookies.txt
```

### `POST /auth/totp/setup`

Totp Setup  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X POST \
  "$BASE/auth/totp/setup" \
  -b cookies.txt
```

### `PATCH /auth/workspace`

Rename Workspace  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X PATCH \
  "$BASE/auth/workspace" \
  -H "Authorization: Bearer $LIP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "Research links"}'
```

## bot

### `GET /bot/diagnostics`

Bot Diagnostics  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X GET \
  "$BASE/bot/diagnostics" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `POST /bot/link-code`

Create Link Code  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X POST \
  "$BASE/bot/link-code" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `POST /telegram/webhook`

Telegram Webhook  
**المصادقة:** — بلا مصادقة

```bash
curl -sS -X POST \
  "$BASE/telegram/webhook"
```

## channels

### `GET /channels`

List Channels  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X GET \
  "$BASE/channels" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `POST /channels`

Add Channel  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X POST \
  "$BASE/channels" \
  -H "Authorization: Bearer $LIP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"account_id": 3, "tg_channel_id": "-1001234567890", "title": "Python Weekly", "username": "python_weekly"}'
```

### `GET /channels/accounts`

List Accounts  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X GET \
  "$BASE/channels/accounts" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `POST /channels/accounts/{account_id}/reactivate`

Reactivate Account  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X POST \
  "$BASE/channels/accounts/3/reactivate" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `DELETE /channels/{channel_id}`

Deactivate Channel  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X DELETE \
  "$BASE/channels/4" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `GET /channels/{channel_id}`

Get Channel  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X GET \
  "$BASE/channels/4" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `PATCH /channels/{channel_id}`

Reassign Channel  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X PATCH \
  "$BASE/channels/4" \
  -H "Authorization: Bearer $LIP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"account_id": 3}'
```

## links

### `GET /links`

Search Links  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X GET \
  "$BASE/links" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `POST /links`

Add Links  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X POST \
  "$BASE/links" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `POST /links/bulk/delete`

Bulk Delete  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X POST \
  "$BASE/links/bulk/delete" \
  -H "Authorization: Bearer $LIP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"category": "other", "q": "webinar"}'
```

### `POST /links/bulk/recategorize`

Bulk Recategorize  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X POST \
  "$BASE/links/bulk/recategorize" \
  -H "Authorization: Bearer $LIP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"category": "other", "new_category": "programming", "q": "pep"}'
```

### `GET /links/export.csv`

Export Links Csv  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X GET \
  "$BASE/links/export.csv" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `GET /links/export.json`

Export Links Json  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X GET \
  "$BASE/links/export.json" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `GET /links/export.md`

Export Links Markdown  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X GET \
  "$BASE/links/export.md" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `GET /links/feedback`

List Feedback  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X GET \
  "$BASE/links/feedback" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `GET /links/random`

Random Links  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X GET \
  "$BASE/links/random" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `GET /links/saved`

List Saved Searches  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X GET \
  "$BASE/links/saved" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `POST /links/saved`

Create Saved Search  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X POST \
  "$BASE/links/saved" \
  -H "Authorization: Bearer $LIP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"filters": {"alive": "false", "category": "programming", "q": "python"}, "name": "dead python links"}'
```

### `DELETE /links/saved/{saved_id}`

Delete Saved Search  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X DELETE \
  "$BASE/links/saved/2" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `GET /links/stats`

Stats  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X GET \
  "$BASE/links/stats" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `DELETE /links/{link_id}`

Delete Link  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X DELETE \
  "$BASE/links/123" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `GET /links/{link_id}`

Get Link  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X GET \
  "$BASE/links/123" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `PATCH /links/{link_id}`

Recategorize Link  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X PATCH \
  "$BASE/links/123" \
  -H "Authorization: Bearer $LIP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"category": "programming"}'
```

### `POST /links/{link_id}/archive`

Set Archived  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X POST \
  "$BASE/links/123/archive" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `POST /links/{link_id}/favorite`

Set Favorite  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X POST \
  "$BASE/links/123/favorite" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `PATCH /links/{link_id}/notes`

Set Notes  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X PATCH \
  "$BASE/links/123/notes" \
  -H "Authorization: Bearer $LIP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"notes": "read before the meeting"}'
```

### `GET /links/{link_id}/open`

Open Link  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X GET \
  "$BASE/links/123/open" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

### `POST /links/{link_id}/pin`

Set Pinned  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X POST \
  "$BASE/links/123/pin" \
  -H "Authorization: Bearer $LIP_API_KEY"
```

## notifications

### `GET /notifications`

List Notifications  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X GET \
  "$BASE/notifications" \
  -b cookies.txt
```

### `GET /notifications/preferences`

List Preferences  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X GET \
  "$BASE/notifications/preferences" \
  -b cookies.txt
```

### `PATCH /notifications/preferences/{alert_type}`

Update Preference  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X PATCH \
  "$BASE/notifications/preferences/weekly_digest" \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'
```

### `POST /notifications/read-all`

Mark All Read  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X POST \
  "$BASE/notifications/read-all" \
  -b cookies.txt
```

### `GET /notifications/unread-count`

Unread Count  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X GET \
  "$BASE/notifications/unread-count" \
  -b cookies.txt
```

### `DELETE /notifications/webhook`

Delete Webhook  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X DELETE \
  "$BASE/notifications/webhook" \
  -b cookies.txt
```

### `GET /notifications/webhook`

Get Webhook  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X GET \
  "$BASE/notifications/webhook" \
  -b cookies.txt
```

### `PUT /notifications/webhook`

Set Webhook  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X PUT \
  "$BASE/notifications/webhook" \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{"url": "https://hooks.example.com/services/T000/B000/XXXX"}'
```

### `POST /notifications/webhook/test`

Test Webhook  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X POST \
  "$BASE/notifications/webhook/test" \
  -b cookies.txt
```

### `POST /notifications/{notification_id}/read`

Mark Read  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X POST \
  "$BASE/notifications/17/read" \
  -b cookies.txt
```

## status

### `GET /status`

System Status  
**المصادقة:** 🍪 جلسة فقط

```bash
curl -sS -X GET \
  "$BASE/status" \
  -b cookies.txt
```

### `POST /status/workflow-runs`

Report Workflow Run  
**المصادقة:** 🔑 مفتاح أو جلسة

```bash
curl -sS -X POST \
  "$BASE/status/workflow-runs" \
  -H "Authorization: Bearer $LIP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"commit_sha": "d97841c733f29358f242798e89270a389ca5201b", "conclusion": "success", "detail": "37 new link(s) across 6 channel(s)", "duration_seconds": 74, "name": "collector"}'
```

## عام

### `GET /`

Index  
**المصادقة:** 🌐 صفحة متصفّح — تُعيد التوجيه إلى `/login` بلا جلسة

```bash
curl -sS -X GET \
  "$BASE/"
```

### `GET /dashboard`

Dashboard Page  
**المصادقة:** 🌐 صفحة متصفّح — تُعيد التوجيه إلى `/login` بلا جلسة

```bash
curl -sS -X GET \
  "$BASE/dashboard"
```

### `GET /healthz`

Healthz  
**المصادقة:** — بلا مصادقة

```bash
curl -sS -X GET \
  "$BASE/healthz"
```

### `GET /login`

Login Page  
**المصادقة:** 🌐 صفحة متصفّح — تُعيد التوجيه إلى `/login` بلا جلسة

```bash
curl -sS -X GET \
  "$BASE/login"
```

### `GET /readyz`

Readyz  
**المصادقة:** — بلا مصادقة

```bash
curl -sS -X GET \
  "$BASE/readyz"
```

### `GET /register`

Register Page  
**المصادقة:** 🌐 صفحة متصفّح — تُعيد التوجيه إلى `/login` بلا جلسة

```bash
curl -sS -X GET \
  "$BASE/register"
```

## ما لا يظهر هنا

قيم معرّفات المسار (`123`، `4`، ...) نائبة عن معرّفات حقيقية تحصل عليها من
استجابة القائمة المقابلة. ومعرّف من مساحة عمل أخرى يردّ **٤٠٤ لا ٤٠٣**
عمداً: الثاني يؤكّد أنّ المعرّف موجود، وهو بالضبط ما يبحث عنه من يجرّب
معرّفات بالتسلسل.
