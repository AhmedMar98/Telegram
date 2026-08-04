# 🔗 Link Intelligence Platform v4.1

> **منصة موزّعة تجمع الروابط من حسابات تيليجرام المتعددة وتصنّفها وتتحقق من حيويتها آلياً عبر ذكاء اصطناعي مجاني بالكامل، مع سلوك بشري يمنع الحظر، وجاهزية للتحول إلى منتج SaaS.**

[![CI](https://github.com/AhmedMar98/Telegram/actions/workflows/ci.yml/badge.svg)](https://github.com/AhmedMar98/Telegram/actions/workflows/ci.yml)
[![Deploy](https://github.com/AhmedMar98/Telegram/actions/workflows/deploy.yml/badge.svg)](https://github.com/AhmedMar98/Telegram/actions/workflows/deploy.yml)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-93%20passing-brightgreen.svg)](tests/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## 📑 جدول المحتويات

- [نظرة عامة](#نظرة-عامة)
- [المعمارية](#المعمارية)
- [المكدّس التقني](#المكدّس-التقني)
- [التثبيت السريع](#التثبيت-السريع)
- [التشغيل](#التشغيل)
- [واجهة الويب](#واجهة-الويب)
- [واجهة API](#واجهة-api)
- [بوت تيليجرام](#بوت-تيليجرام)
- [الإعدادات](#الإعدادات)
- [الاختبارات](#الاختبارات)
- [النشر على Oracle Cloud](#النشر-على-oracle-cloud)
- [الضمانات الأمنية](#الضمانات-الأمنية)
- [القيود الصادقة](#القيود-الصادقة)
- [خارطة الطريق](#خارطة-الطريق)
- [المساهمة](#المساهمة)
- [الترخيص](#الترخيص)

---

## نظرة عامة

**المشكلة:** تيليجرام غارق في الروابط المبعثرة عبر آلاف القنوات. يدوياً يستحيل تتبّعها، وتصنيفها يستهلك ساعات، وأغلبها يموت بسرعة.

**الحل:** منصة موزّعة على ثلاث طبقات منفصلة المسؤوليات:

1. **طبقة الجمع (Userbots):** حسابات تيليجرام تلتقط الرسائل والملفات فقط — لا تصنّف، لا تُرسل، فقط تجمع. سلوكها بشري: تأخير عشوائي ٨-٢٥ ثانية، نشاط ١٠-١٦ ساعة يومياً، احترام كامل لـ FloodWait.

2. **طبقة المعالجة (VPS Hub مركزي):** يستقبل البيانات، يخصّص العمل على بوتات متخصصة (واتساب، تيليجرام، ملفات، إعدادات)، يصنّف بطبقات ذكية: ٨٠٪ عبر قواعد، ١٥٪ عبر بيانات وصفية، ٤٪ عبر LLM، ١٪ تحليل عميق. يتحقق من حيوية كل رابط بمعدل عشوائي ٥-١٠ دقائق.

3. **طبقة الذكاء (AI Orchestrator):** شبكة من ٥ مزودي LLM مجانيين مع failover تلقائي وتتبّع الحصص من headers الاستجابة. يطبّق ذاكرة دلالية لإعادة استخدام نتائج التصنيف المتشابهة.

---

## المعمارية

```
┌──────────────────────────────────────────────────────────────┐
│                     Telegram Sources                          │
│  (channels, groups, bots, files)                             │
└──────────┬───────────────────────────────────────────────────┘
           │ Telethon (userbot) — read-only, human-like pacing
           ▼
┌──────────────────────────────┐    ┌──────────────────────────┐
│    COLLECTOR LAYER           │    │     MOCK COLLECTOR       │
│  (Telethon + Mock fallback)  │    │  (for dev / testing)     │
└──────────┬───────────────────┘    └──────────────────────────┘
           │ LinkExtraction
           ▼
┌──────────────────────────────────────────────────────────────┐
│                   PROCESSING HUB                             │
│                                                              │
│   ┌─────────────────────────────────────────────────────┐   │
│   │  Classification Pipeline                             │   │
│   │  ┌──────────────┐  ┌──────────────┐  ┌───────────┐  │   │
│   │  │ Tier 1 Rules │→ │ Tier 2 Meta  │→ │ Tier 3 AI │  │   │
│   │  │   (~80%)     │  │   (~15%)     │  │  (~4%)    │  │   │
│   │  └──────────────┘  └──────────────┘  └───────────┘  │   │
│   │                                       ┌───────────┐  │   │
│   │                                       │ Tier 4    │  │   │
│   │                                       │ LLM Deep  │  │   │
│   │                                       │  (~1%)    │  │   │
│   │                                       └───────────┘  │   │
│   └─────────────────────────────────────────────────────┘   │
│                                                              │
│   ┌─────────────────────────────────────────────────────┐   │
│   │  Vitality Checker (async, 5-10min pacing)           │   │
│   └─────────────────────────────────────────────────────┘   │
└──────────┬───────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────┐
│              STORAGE (SQLite + Redis, NO Docker)             │
│  • SQLite (WAL) — links, accounts, logs, vitality history   │
│  • FTS5 virtual table — full-text search                    │
│  • sqlite-vec — semantic vector search (MiniLM embeddings)  │
│  • Redis — task queue, rate-limit, caching                  │
└──────────┬───────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────┐
│                     INTERFACES                               │
│                                                              │
│   ┌──────────────────┐    ┌─────────────────────────────┐   │
│   │  FastAPI Web     │    │   Telegram Bot              │   │
│   │  (admin panel)   │    │   /search /stats /popular   │   │
│   │  + REST API      │    │   /alive                    │   │
│   └──────────────────┘    └─────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
```

---

## المكدّس التقني

| الطبقة | التقنية | السبب |
|---|---|---|
| اللغة | Python 3.11+ | سرعة التطوير، نظام بيئي غني للـ AI/Telegram |
| إطار الويب | FastAPI | async أصلي، Swagger تلقائي، أداء عالٍ |
| Telegram userbot | Telethon | أحدث وأشمل مكتبة لـ MTProto |
| Telegram bot | aiogram 3.x | async، بنيوي، نشط الصيانة |
| قاعدة البيانات | SQLite + WAL | صفر إعداد، ملف واحد، أداء ممتاز للقراءة |
| البحث النصي | SQLite FTS5 | مدمج في sqlite3، دعم Unicode61 |
| البحث الدلالي | sqlite-vec | إضافة sqlite للبحث المتجهي، بدون Docker |
| الطوابير | Redis + RQ | لإدارة المهام الخلفية والكاش |
| LLM | 5 مزودين مع failover | Groq + Gemini + OpenRouter + HuggingFace + Z.AI |
| التضمينات | sentence-transformers (all-MiniLM-L6-v2) | محلي، مجاني، 384-dim |
| التشفير | cryptography (AES-256-GCM + HMAC) | معتمد صناعياً |
| ORM | SQLAlchemy 2.0 + Alembic | typed، migrations آمنة |
| CI/CD | GitHub Actions | lint + test + auto-deploy إلى Oracle |
| الاعتماد | venv + requirements.txt | أبسط وأكثر توافقاً |

---

## التثبيت السريع

### المتطلبات
- Python 3.11+
- Redis (مثبّت على النظام، **بدون Docker**)
- ~500MB مساحة فارغة

### الخطوات

```bash
# 1. استنساخ المستودع
git clone https://github.com/AhmedMar98/Telegram.git
cd Telegram

# 2. إنشاء بيئة افتراضية
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# أو: venv\Scripts\activate  # Windows

# 3. تثبيت الاعتمادات
pip install --upgrade pip
pip install -r requirements.txt

# 4. إعداد المتغيرات البيئية
cp .env.example .env
# ولّد مفاتيح التشفير:
python scripts/generate_keys.py
# انسخ الناتج إلى .env

# 5. (اختياري) ثبّت Redis على النظام:
# Ubuntu: sudo apt install redis-server && sudo systemctl start redis-server
# macOS:  brew install redis && brew services start redis

# 6. تهيئة قاعدة البيانات
python scripts/init_db.py

# 7. عدّل .env وأضف بياناتك:
#    - TG_API_ID, TG_API_HASH, TG_PHONE (من my.telegram.org)
#    - TG_MOCK_MODE=false (لتفعيل الجمع الحقيقي)
#    - TG_MONITOR_CHANNELS=channel1,channel2
#    - OPENROUTER_API_KEY / GEMINI_API_KEY / GROQ_API_KEY (مزود LLM واحد على الأقل)
#    - TG_BOT_TOKEN (من @BotFather، اختياري للبوت)
```

---

## التشغيل

### الطريقة 1: تشغيل كل شيء في عملية واحدة (للتطوير)

```bash
python scripts/run_all.py
```

يبدأ: web server + collect worker + classify worker + vitality worker.

### الطريقة 2: تشغيل كل خدمة منفصلة (للإنتاج)

```terminal #1 — Web server
python scripts/run_web.py
```

```terminal #2 — Collector
python scripts/run_worker.py collect
```

```terminal #3 — Classifier
python scripts/run_worker.py classify
```

```terminal #4 — Vitality checker
python scripts/run_worker.py vitality
```

### الطريقة 3: systemd services (للإنتاج على Linux)

```bash
# بعد تشغيل deploy/setup.sh
sudo systemctl start link-intel-web link-intel-worker
sudo systemctl enable link-intel-web link-intel-worker
journalctl -u link-intel-web -f      # متابعة اللوغ
```

---

## واجهة الويب

بعد التشغيل، افتح: **http://localhost:8000**

### الصفحات

| المسار | الوصف |
|---|---|
| `/` | لوحة التحكم: KPIs، حالة المزودين، أحدث الروابط |
| `/links` | تصفّح الروابط مع فلترة + بحث هجين |
| `/accounts` | إدارة حسابات تيليجرام |
| `/docs` | توثيق OpenAPI / Swagger التفاعلي |
| `/redoc` | توثيق ReDoc البديل |

### KPIs المعروضة
- إجمالي الروابط
- روابط حية / ميتة
- روابط جديدة / مُصنّفة
- توزيع الفئات (رسم بياني)
- حالة مزودي LLM (حصص، cooldown، أخطاء)

---

## واجهة API

جميع النقاط تحت prefix `/api/v1`.

### الأهداف الرئيسية

| Method | Path | الوصف |
|---|---|---|
| `GET` | `/api/v1/stats` | إحصائيات شاملة |
| `GET` | `/api/v1/links` | قائمة الروابط (فلترة بـ `category`, `alive`) |
| `GET` | `/api/v1/links/{id}` | تفاصيل رابط + سجل التصنيف والحيوية |
| `POST` | `/api/v1/links/{id}/check` | تحقق فوري من حيوية رابط |
| `POST` | `/api/v1/links/{id}/click` | تسجيل نقرة (للشعبية) |
| `GET` | `/api/v1/search?q=...` | بحث هجين (نصي + دلالي) |
| `GET` | `/health` | فحص الصحة |

### مثال

```bash
# بحث هجين
curl "http://localhost:8000/api/v1/search?q=python%20tutorial&alive_only=true&limit=5"

# إحصائيات
curl http://localhost:8000/api/v1/stats | jq

# تحقق من رابط
curl -X POST http://localhost:8000/api/v1/links/42/check
```

---

## بوت تيليجرام

بعد ضبط `TG_BOT_TOKEN` في `.env`:

1. ابدأ محادثة مع البوت عبر `/start`
2. الأوامر المتاحة:

| الأمر | الوصف |
|---|---|
| `/search <query>` | بحث هجين (نصي + دلالي)، أفضل ١٠ نتائج |
| `/stats` | إحصائيات قاعدة البيانات |
| `/popular` | أفضل ١٠ روابط شعبية |
| `/alive` | أحدث ١٠ روابط حية |

**معدّل محدود:** ١٠ أوامر/دقيقة لكل مستخدم (ضمان anti-spam).

---

## الإعدادات

كل الإعدادات في `.env` (انسخ من `.env.example`).

### الإعدادات الحرجة

| المتغير | افتراضي | الوصف |
|---|---|---|
| `TG_MOCK_MODE` | `true` | يستخدم Mock Collector (بدون اتصال تيليجرام حقيقي) |
| `TG_API_ID` | — | من my.telegram.org |
| `TG_API_HASH` | — | من my.telegram.org |
| `TG_PHONE` | — | رقم هاتفك بصيغة دولية |
| `TG_MONITOR_CHANNELS` | — | قائمة قنوات للمراقبة (مفصولة بفواصل) |
| `TG_BOT_TOKEN` | — | من @BotFather |
| `AES_ENCRYPTION_KEY` | — | 64 hex chars (32 bytes) — **مطلوب للإنتاج** |
| `HMAC_KEY` | — | 64 hex chars — **مطلوب للإنتاج** |
| `SEMANTIC_REUSE_THRESHOLD` | `0.92` | حد التشابه لإعادة استخدام تصنيف سابق |

### توليد المفاتيح

```bash
python scripts/generate_keys.py
# انسخ الناتج إلى .env
```

---

## الاختبارات

```bash
# جميع الاختبارات + التغطية
pytest

# اختبار محدد
pytest tests/test_crypto.py -v

# بدون تغطية (أسرع)
pytest --no-cov
```

### التقارير
- التغطية: `coverage.xml` + إخراج طرفي
- CI يرفع تقرير التغطية كـ artifact على GitHub Actions

---

## النشر على Oracle Cloud

المنصة مصمّمة للعمل على Oracle Cloud Always Free (ARM Ampere, 4 cores / 24GB RAM).

### الطريقة الآلية (CI/CD)

1. اضبط Repository Secrets في GitHub:
   - `ORACLE_HOST` — عنوان IP للـ VM
   - `ORACLE_SSH_KEY` — مفتاح SSH الخاص
   - `ORACLE_USER` — `ubuntu` (افتراضي)
   - `ORACLE_DEPLOY_PATH` — `/home/ubuntu/link-intelligence` (افتراضي)

2. عند الدفع إلى `main`، سيقوم workflow `deploy.yml` بـ:
   - مزامنة الكود عبر rsync
   - تثبيت الاعتمادات
   - تهيئة قاعدة البيانات
   - إعادة تشغيل systemd services

### الطريقة اليدوية

```bash
# SSH إلى VM
ssh ubuntu@your-oracle-vm

# استنساخ + إعداد
git clone https://github.com/AhmedMar98/Telegram.git link-intelligence
cd link-intelligence
bash deploy/setup.sh

# عدّل .env بالمفاتيح الحقيقية
nano .env

# ابدأ الخدمات
sudo systemctl start link-intel-web link-intel-worker
```

---

## الضمانات الأمنية

### Anti-Spam المُبرمج
- ✅ البوتات **لا تُرسل أبداً** رسائل لمجموعات الآخرين — قراءة فقط.
- ✅ حساب Main وحده يُنشئ المجموعات (حد أقصى ٣) — يُطبّق في طبقة الويب.
- ✅ احترام كامل لـ FloodWait (نوم للمدة المطلوبة + jitter).
- ✅ تأخير عشوائي بين الجمع (8-25s) وبين تحققات الحيوية (5-10min).

### التشفير
- ✅ **AES-256-GCM** لكل المعرّفات الحساسة (api_id, api_hash, phone).
- ✅ **HMAC منفصل** لكل حساب (مشتق من master key).
- ✅ مفاتيح التشفير في `.env` (لا تُخزّن في الكود).
- ✅ Nonce عشوائي (12 bytes) لكل عملية تشفير.

### قاعدة البيانات
- ✅ SQLite في وضع WAL (atomic commits).
- ✅ Foreign keys مفعّلة.
- ✅ Hash فريد لكل URL (منع التكرار).
- ✅ جاهزية Row-Level Security لـ SaaS متعدد المستأجرين (TODO في v5).

### API
- ✅ توثيق OpenAPI تلقائي.
- ✅ Rate limiting لكل مستخدم (بوت: 10 cmd/min).
- ✅ CORS قابل للتهيئة.

---

## القيود الصادقة

| القيد | التفاصيل |
|---|---|
| لا ضمان ضد الحظر | خوارزميات تيليجرام سرية؛ السلوك البشري يقلّل المخاطر لكن لا يلغيها |
| الحصص المجانية قابلة للتغيير | مزودو LLM قد يغيّرون حدودهم في أي وقت |
| يتطلب ٣ حسابات شرعية | للإنتاج الموزّع (MVP يعمل بحساب واحد) |
| RLS على مستوى ORM فقط | SQLite لا يدعم RLS على مستوى DB؛ للإنتاج متعدد المستأجرين، استخدم PostgreSQL |
| Federated Learning = stub | `LocalStubAggregator` لا يتدرب فعلياً؛ الواجهة موجودة للاستبدال لاحقاً بـ Flower |
| Stripe billing يحتاج اختبار إنتاج | الـ webhooks معرّضة لكن تحتاج اختبار مع مفاتيح Stripe حقيقية |
| Mobile app مؤجّل | يتطلب فريق React Native منفصل (خارج نطاق هذا المستودع) |

---

## خارطة الطريق

### v4.0 ✅ (السابق)
- بنية موزّعة (collector → processor → storage → interfaces)
- ٥ مزودي LLM مع failover
- AES-256-GCM + HMAC
- بحث هجين (FTS5 + sqlite-vec)
- لوحة ويب FastAPI + REST API
- بوت تيليجرام
- CI/CD كامل

### v4.1 ✅ (الحالي)
- ✅ **Semantic Memory**: إعادة استخدام التصنيف عند تشابه > 0.92 (جدول `semantic_cache`)
- ✅ **Multi-tenant**: `TenantContext` + `X-API-Key` header + `tenant_id` في كل الجداول + `SAAS_MODE`
- ✅ **Daily Summary Cron**: `DailySummaryWorker` يولّد ملخصاً يومياً للروابط الرائجة
- ✅ **WebSockets Live Logs**: `/ws/logs` endpoint + صفحة `/logs` مع auto-reconnect
- ✅ **Prometheus Metrics**: `/metrics` endpoint مع counters/histograms/gauges

### v5.0 ✅ (جزئياً — انظر التفاصيل)
- ✅ **١٠ مزودي LLM**: Cohere + Mistral + Together + Anyscale + Cloudflare مُضافين
- ✅ **Distributed Userbots**: `WorkerCoordinator` + Redis claim locks + heartbeats + dead-worker detection
- ✅ **Stripe Billing**: `Subscription` model + Checkout + Webhooks + usage tracking (`/api/v1/billing/*`)
- ✅ **Federated Learning**: واجهة `BaseFederatedAggregator` + `LocalStubAggregator` + وثيقة تصميم
- ⏳ **Mobile app (React Native)**: مؤجّل (يحتاج فريق منفصل)

### v5.1 (التالي)
- [ ] استبدال `LocalStubAggregator` بـ `FlowerAggregator` حقيقي
- [ ] نقل قاعدة البيانات إلى PostgreSQL لتفعيل RLS حقيقي على مستوى الـ DB
- [ ] Mobile app (React Native) — يحتاج فريق منفصل
- [ ] Prometheus alerting rules + Grafana dashboards
- [ ] Multi-region deployment

---

## المساهمة

1. Fork المستودع
2. أنشئ فرع: `git checkout -b feature/amazing-feature`
3. التزم بـ ruff: `ruff check . && ruff format --check .`
4. اجتاز الاختبارات: `pytest`
5. افتح Pull Request

### معايير القبول
- ✅ ruff بدون أخطاء
- ✅ pytest يجتاز
- ✅ لا secrets في الكود
- ✅ توثيق للـ API الجديد
- ✅ اختبارات للمنطق الجديد

---

## الترخيص

MIT License — راجع [LICENSE](LICENSE).

---

## الروابط ذات الصلة

- **المستودع القديم:** https://github.com/AhmedMar98/link-intelligence-platform
- **المستودع الحالي:** https://github.com/AhmedMar98/Telegram
- **Telethon docs:** https://docs.telethon.dev
- **FastAPI docs:** https://fastapi.tiangolo.com
- **sqlite-vec:** https://github.com/asg017/sqlite-vec

---

<p align="center">
  صُنع بـ ❤️ — جاهز للتحول إلى SaaS
</p>
