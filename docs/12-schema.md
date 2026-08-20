# مخطط قاعدة البيانات (مولَّد آلياً)

> لا تحرّر هذا الملف يدوياً. أعد توليده بـ:
> `python scripts/db_report.py --schema`

المصدر هو `app/models.py` نفسه، فلا يمكن أن يصف عموداً غير موجود
ولا أن يُغفل عموداً أُضيف.

## `action_events`

| العمود | النوع | يقبل NULL | مفتاح |
|---|---|---|---|
| `id` | `INTEGER` | لا | PK |
| `scope` | `VARCHAR(50)` | لا | — |
| `identifier` | `VARCHAR(100)` | لا | — |
| `created_at` | `DATETIME` | لا | — |

**الفهارس:** `ix_action_events_scope_identifier_created` (scope, identifier, created_at)

## `login_attempts`

| العمود | النوع | يقبل NULL | مفتاح |
|---|---|---|---|
| `id` | `INTEGER` | لا | PK |
| `identifier` | `VARCHAR(320)` | لا | — |
| `successful` | `BOOLEAN` | لا | — |
| `created_at` | `DATETIME` | لا | — |
| `ip_address` | `VARCHAR(45)` | نعم | — |

**الفهارس:** `ix_login_attempts_created_at` (created_at)، `ix_login_attempts_identifier` (identifier)

## `workspaces`

| العمود | النوع | يقبل NULL | مفتاح |
|---|---|---|---|
| `id` | `INTEGER` | لا | PK |
| `name` | `VARCHAR(200)` | لا | — |
| `created_at` | `DATETIME` | لا | — |

## `bot_link_codes`

| العمود | النوع | يقبل NULL | مفتاح |
|---|---|---|---|
| `id` | `INTEGER` | لا | PK |
| `workspace_id` | `INTEGER` | لا | FK → workspaces.id |
| `code` | `VARCHAR(16)` | لا | — |
| `created_at` | `DATETIME` | لا | — |
| `used_at` | `DATETIME` | نعم | — |

**الفهارس:** `ix_bot_link_codes_code` (code) فريد، `ix_bot_link_codes_workspace_id` (workspace_id)

## `bot_links`

| العمود | النوع | يقبل NULL | مفتاح |
|---|---|---|---|
| `chat_id` | `VARCHAR(64)` | لا | PK |
| `workspace_id` | `INTEGER` | لا | FK → workspaces.id |
| `created_at` | `DATETIME` | لا | — |

**الفهارس:** `ix_bot_links_workspace_id` (workspace_id)

## `classification_feedback`

| العمود | النوع | يقبل NULL | مفتاح |
|---|---|---|---|
| `id` | `INTEGER` | لا | PK |
| `workspace_id` | `INTEGER` | لا | FK → workspaces.id |
| `link_id` | `INTEGER` | لا | — |
| `url` | `TEXT` | لا | — |
| `previous_category` | `VARCHAR(50)` | لا | — |
| `new_category` | `VARCHAR(50)` | لا | — |
| `previous_confidence` | `FLOAT` | لا | — |
| `previous_matched_rule` | `VARCHAR(100)` | نعم | — |
| `created_at` | `DATETIME` | لا | — |

**الفهارس:** `ix_classification_feedback_link_id` (link_id)، `ix_classification_feedback_workspace_id` (workspace_id)، `ix_feedback_workspace_created` (workspace_id, created_at)

## `saved_searches`

| العمود | النوع | يقبل NULL | مفتاح |
|---|---|---|---|
| `id` | `INTEGER` | لا | PK |
| `workspace_id` | `INTEGER` | لا | FK → workspaces.id |
| `name` | `VARCHAR(100)` | لا | — |
| `filters` | `TEXT` | لا | — |
| `created_at` | `DATETIME` | لا | — |

**الفهارس:** `ix_saved_searches_workspace_id` (workspace_id)

## `telegram_accounts`

| العمود | النوع | يقبل NULL | مفتاح |
|---|---|---|---|
| `id` | `INTEGER` | لا | PK |
| `workspace_id` | `INTEGER` | لا | FK → workspaces.id |
| `label` | `VARCHAR(100)` | لا | — |
| `session_string` | `TEXT` | لا | — |
| `is_active` | `BOOLEAN` | لا | — |
| `last_success_at` | `DATETIME` | نعم | — |
| `last_failure_at` | `DATETIME` | نعم | — |
| `last_error` | `VARCHAR(300)` | نعم | — |
| `consecutive_failures` | `INTEGER` | لا | — |
| `disabled_reason` | `VARCHAR(300)` | نعم | — |
| `links_collected` | `INTEGER` | لا | — |
| `created_at` | `DATETIME` | لا | — |

**الفهارس:** `ix_telegram_accounts_workspace_id` (workspace_id)

## `users`

| العمود | النوع | يقبل NULL | مفتاح |
|---|---|---|---|
| `id` | `INTEGER` | لا | PK |
| `workspace_id` | `INTEGER` | لا | FK → workspaces.id |
| `email` | `VARCHAR(320)` | لا | — |
| `password_hash` | `VARCHAR(200)` | لا | — |
| `role` | `VARCHAR(20)` | لا | — |
| `is_active` | `BOOLEAN` | لا | — |
| `created_at` | `DATETIME` | لا | — |

**الفهارس:** `ix_users_email` (email)، `ix_users_workspace_id` (workspace_id)

## `audit_log`

| العمود | النوع | يقبل NULL | مفتاح |
|---|---|---|---|
| `id` | `INTEGER` | لا | PK |
| `workspace_id` | `INTEGER` | لا | FK → workspaces.id |
| `user_id` | `INTEGER` | نعم | FK → users.id |
| `action` | `VARCHAR(100)` | لا | — |
| `target_type` | `VARCHAR(50)` | نعم | — |
| `target_id` | `VARCHAR(50)` | نعم | — |
| `detail` | `TEXT` | نعم | — |
| `created_at` | `DATETIME` | لا | — |

**الفهارس:** `ix_audit_log_workspace_id` (workspace_id)

## `auth_sessions`

| العمود | النوع | يقبل NULL | مفتاح |
|---|---|---|---|
| `id` | `INTEGER` | لا | PK |
| `user_id` | `INTEGER` | لا | FK → users.id |
| `token_hash` | `VARCHAR(64)` | لا | — |
| `created_at` | `DATETIME` | لا | — |
| `expires_at` | `DATETIME` | لا | — |
| `revoked_at` | `DATETIME` | نعم | — |
| `ip_address` | `VARCHAR(45)` | نعم | — |
| `user_agent` | `VARCHAR(300)` | نعم | — |

**الفهارس:** `ix_auth_sessions_token_hash` (token_hash) فريد، `ix_auth_sessions_user_id` (user_id)

## `channels`

| العمود | النوع | يقبل NULL | مفتاح |
|---|---|---|---|
| `id` | `INTEGER` | لا | PK |
| `workspace_id` | `INTEGER` | لا | FK → workspaces.id |
| `account_id` | `INTEGER` | نعم | FK → telegram_accounts.id |
| `tg_channel_id` | `VARCHAR(64)` | لا | — |
| `username` | `VARCHAR(200)` | نعم | — |
| `title` | `VARCHAR(300)` | نعم | — |
| `last_message_id` | `INTEGER` | لا | — |
| `is_active` | `BOOLEAN` | لا | — |
| `created_at` | `DATETIME` | لا | — |

**الفهارس:** `ix_channels_workspace_id` (workspace_id)

## `links`

| العمود | النوع | يقبل NULL | مفتاح |
|---|---|---|---|
| `id` | `INTEGER` | لا | PK |
| `workspace_id` | `INTEGER` | لا | FK → workspaces.id |
| `channel_id` | `INTEGER` | لا | FK → channels.id |
| `message_id` | `INTEGER` | لا | — |
| `url` | `TEXT` | لا | — |
| `url_hash` | `VARCHAR(64)` | لا | — |
| `domain` | `VARCHAR(300)` | لا | — |
| `category` | `VARCHAR(50)` | لا | — |
| `confidence` | `FLOAT` | لا | — |
| `classified_by` | `VARCHAR(20)` | لا | — |
| `is_favorite` | `BOOLEAN` | لا | — |
| `matched_rule` | `VARCHAR(100)` | نعم | — |
| `source_type` | `VARCHAR(20)` | لا | — |
| `forwarded_from` | `VARCHAR(300)` | نعم | — |
| `language` | `VARCHAR(10)` | نعم | — |
| `notes` | `TEXT` | نعم | — |
| `is_pinned` | `BOOLEAN` | لا | — |
| `click_count` | `INTEGER` | لا | — |
| `raw_text` | `TEXT` | نعم | — |
| `posted_at` | `DATETIME` | نعم | — |
| `created_at` | `DATETIME` | لا | — |
| `last_checked_at` | `DATETIME` | نعم | — |
| `http_status` | `INTEGER` | نعم | — |
| `is_alive` | `BOOLEAN` | نعم | — |
| `last_alive_at` | `DATETIME` | نعم | — |
| `consecutive_failures` | `INTEGER` | لا | — |
| `is_archived` | `BOOLEAN` | لا | — |

**الفهارس:** `ix_links_category` (category)، `ix_links_channel_id` (channel_id)، `ix_links_domain` (domain)، `ix_links_is_archived` (is_archived)، `ix_links_is_favorite` (is_favorite)، `ix_links_is_pinned` (is_pinned)، `ix_links_last_checked_at` (last_checked_at)، `ix_links_source_type` (source_type)، `ix_links_url_hash` (url_hash)، `ix_links_workspace_created` (workspace_id, created_at)، `ix_links_workspace_id` (workspace_id)، `ix_links_ws_archived_domain` (workspace_id, is_archived, domain)
