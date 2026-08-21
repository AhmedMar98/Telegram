async function api(path, opts) {
  const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
  if (res.status === 401) { window.location.href = "/login"; throw new Error("unauthenticated"); }
  return res;
}

async function loadStats() {
  const res = await api("/links/stats");
  const data = await res.json();
  const topDomains = data.top_domains.slice(0, 5).map(([d, n]) => `${d} (${n})`).join("، ");
  const v = data.vitality;
  const deadest = v.deadest_domains.slice(0, 3).map(([d, n]) => `${d} (${n})`).join("، ");
  document.getElementById("stats").textContent =
    `إجمالي الروابط: ${data.total_links} | القنوات: ${data.total_channels}` +
    ` | هذا الأسبوع: ${data.added_this_week} | هذا الشهر: ${data.added_this_month}` +
    ` | 🟢 ${v.alive} · 🔴 ${v.dead} · ⚪ ${v.unchecked}` +
    (v.archived ? ` · 🗄 ${v.archived}` : "") +
    (topDomains ? ` | أعلى النطاقات: ${topDomains}` : "") +
    (deadest ? ` | الأكثر موتاً: ${deadest}` : "") +
    storageSummary(data.storage);
  renderVitalityBar(v);
  renderCollectorHealth(data.collection);
  renderQuickStart(data.total_links);
}

function storageSummary(s) {
  if (!s) return "";
  // Size is Postgres-only. On SQLite the server sends null rather than a
  // made-up number, and the page says nothing rather than inventing one.
  if (s.database_bytes == null) return "";
  const mb = s.database_bytes / (1024 * 1024);
  const size = mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(1)} MB`;
  return ` | التخزين: ${size}` + (s.largest_table ? ` (أكبر جدول: ${escapeText(s.largest_table)})` : "");
}

// A stopped collector is the failure with no symptom: everything keeps
// working, the collection just stops growing. This is the only place that
// says so.
function renderCollectorHealth(c) {
  const el = document.getElementById("collectorWarning");
  if (!c || !c.looks_stalled) { el.hidden = true; return; }
  const days = Math.floor(c.hours_since_last_run / 24);
  el.hidden = false;
  el.textContent = days >= 1
    ? `⚠️ لم يعمل الجمع التلقائي منذ ${days} يوم${days > 1 ? "اً" : ""}. تحقّق من GitHub Actions.`
    : `⚠️ لم يعمل الجمع التلقائي منذ ${Math.round(c.hours_since_last_run)} ساعة. تحقّق من GitHub Actions.`;
}

// Shown only to a workspace with nothing in it, and only until dismissed.
// A "getting started" panel that keeps reappearing to an established user
// is noise, not help.
function renderQuickStart(totalLinks) {
  let dismissed = false;
  try { dismissed = localStorage.getItem("quickStartDismissed") === "1"; } catch (e) { /* private mode */ }
  document.getElementById("quickStart").hidden = dismissed || totalLinks > 0;
}

function dismissQuickStart() {
  try { localStorage.setItem("quickStartDismissed", "1"); } catch (e) { /* private mode */ }
  document.getElementById("quickStart").hidden = true;
}

function renderVitalityBar(v) {
  const bar = document.getElementById("vitalityBar");
  const total = v.alive + v.dead + v.unchecked;
  if (!total) { bar.innerHTML = ""; return; }
  const segments = [
    ["#16a34a", v.alive, "حيّة"],
    ["#dc2626", v.dead, "ميتة"],
    ["#9ca3af", v.unchecked, "لم تُفحص"],
  ];
  // Built as elements with their width and colour set through the CSSOM
  // rather than an inline style attribute in an HTML string. Both express
  // the same thing, but only the attribute form is inline CSS as far as
  // the Content-Security-Policy is concerned — and these two values are
  // genuinely per-render, so they cannot become classes.
  bar.replaceChildren(
    ...segments
      .filter(([, count]) => count > 0)
      .map(([colour, count, label]) => {
        const segment = document.createElement("div");
        segment.style.width = `${(count / total * 100).toFixed(1)}%`;
        segment.style.background = colour;
        segment.title = `${label}: ${count}`;
        return segment;
      })
  );
}

// Server-rendered, and read from the markup rather than baked into this
// file: a static asset cannot contain a template expression, and a data
// attribute keeps the value on the page without an extra round trip or an
// inline script (which the CSP no longer allows).
const CATEGORIES = JSON.parse(document.getElementById("appData").dataset.categories);
let currentPage = 1;

function changePage(delta) {
  currentPage = Math.max(1, currentPage + delta);
  search(false);
}

// Deleting one link used to be a modal confirm: a decision demanded before
// the consequence is visible, and irreversible once taken. An undo window
// inverts that — the row disappears immediately, and the request is only
// sent if the five seconds pass without an undo. Nothing is deleted
// server-side until then, so undo is a genuine cancellation rather than a
// re-insert that would lose the link's id and history.
const UNDO_SECONDS = 5;
let pendingDelete = null;

async function removeLink(id) {
  if (pendingDelete) await flushPendingDelete();

  const row = document.getElementById(`cat-${id}`)?.closest(".card");
  if (row) row.style.display = "none";

  const bar = document.getElementById("undoBar");
  bar.hidden = false;
  bar.querySelector("#undoText").textContent = `حُذف رابط. التراجع خلال ${UNDO_SECONDS} ثوانٍ.`;
  announce("حُذف رابط. يمكنك التراجع.");

  const timer = setTimeout(flushPendingDelete, UNDO_SECONDS * 1000);
  pendingDelete = { id, timer, row };
}

async function flushPendingDelete() {
  if (!pendingDelete) return;
  const { id, timer } = pendingDelete;
  clearTimeout(timer);
  pendingDelete = null;
  document.getElementById("undoBar").hidden = true;
  await api(`/links/${id}`, { method: "DELETE" });
  search(false);
  loadStats();
}

function undoDelete() {
  if (!pendingDelete) return;
  clearTimeout(pendingDelete.timer);
  if (pendingDelete.row) pendingDelete.row.style.display = "";
  pendingDelete = null;
  document.getElementById("undoBar").hidden = true;
  announce("أُلغي الحذف.");
}

// A pending delete must not be lost by navigating away — either it happens
// or it does not, and leaving it ambiguous is worse than either.
window.addEventListener("beforeunload", () => {
  // keepalive lets the request outlive the page; sendBeacon cannot be used
  // because it only issues POST. If the browser drops it anyway the link
  // simply survives, which is the safe direction for a delete to fail in.
  if (pendingDelete) {
    fetch(`/links/${pendingDelete.id}`, { method: "DELETE", keepalive: true, credentials: "same-origin" });
  }
});

async function recategorize(id, category) {
  await api(`/links/${id}`, { method: "PATCH", body: JSON.stringify({ category }) });
  loadStats();
}

async function toggleFavorite(id, makeFavorite) {
  await api(`/links/${id}/favorite?is_favorite=${makeFavorite}`, { method: "POST" });
  search(false);
}

async function toggleArchive(id, makeArchived) {
  await api(`/links/${id}/archive?is_archived=${makeArchived}`, { method: "POST" });
  search(false);
  loadStats();
}

// All three exports carry the search term as well as the category. Before
// this, "export" next to a search box exported the whole workspace, which
// is the opposite of what the button appears to promise.
function exportWith(path) {
  const params = new URLSearchParams();
  const q = document.getElementById("q").value;
  const category = document.getElementById("category").value;
  if (q) params.set("q", q);
  if (category) params.set("category", category);
  const query = params.toString();
  window.location = path + (query ? "?" + query : "");
}

function exportCsv() { exportWith("/links/export.csv"); }
function exportJson() { exportWith("/links/export.json"); }
function exportMarkdown() { exportWith("/links/export.md"); }

function currentFilter() {
  return { q: document.getElementById("q").value || null, category: document.getElementById("category").value || null };
}

async function bulkDelete() {
  const filter = currentFilter();
  if (!confirm("حذف كل الروابط المطابقة للفلتر الحالي؟ لا يمكن التراجع.")) return;
  const res = await api("/links/bulk/delete", { method: "POST", body: JSON.stringify(filter) });
  const data = await res.json();
  alert(`تم حذف ${data.affected} رابط.`);
  search(false);
  loadStats();
}

async function bulkRecategorize() {
  const filter = currentFilter();
  const new_category = prompt("التصنيف الجديد لكل النتائج المطابقة للفلتر الحالي:\n" + CATEGORIES.join(", "));
  if (!new_category) return;
  if (!CATEGORIES.includes(new_category)) { alert("تصنيف غير معروف."); return; }
  const res = await api("/links/bulk/recategorize", {
    method: "POST",
    body: JSON.stringify(Object.assign({}, filter, { new_category })),
  });
  const data = await res.json();
  alert(`تم تحديث ${data.affected} رابط.`);
  search(false);
}

// The server decides the category; the page only decides the wording. Keeps
// "what does a 403 mean" in one place instead of two.
const STATUS_LABEL = {
  ok: "🟢 حيّة",
  redirect: "🟢 حيّة (تحويل)",
  blocked: "🟠 محجوبة عن الفاحص",
  missing: "🔴 غير موجودة",
  throttled: "🟡 الخادم يحدّ الطلبات",
  server_error: "🟡 عطل مؤقّت في الخادم",
  unreachable: "🟡 تعذّر الوصول",
  unchecked: "⚪ لم تُفحص بعد",
  client_error: "🔴 خطأ في الطلب",
};

function vitalityBadge(i) {
  return STATUS_LABEL[i.status_category] || STATUS_LABEL.unchecked;
}

function vitalityTitle(i) {
  const parts = [];
  parts.push(i.last_checked_at ? "آخر فحص: " + new Date(i.last_checked_at).toLocaleString("ar") : "لم يُفحص بعد");
  if (i.http_status) parts.push("HTTP " + i.http_status);
  // The question a dead link actually raises is "when did this last work?",
  // which last_checked_at cannot answer.
  if (i.last_alive_at) parts.push("آخر مرة كانت حيّة: " + new Date(i.last_alive_at).toLocaleString("ar"));
  if (i.consecutive_failures > 0) parts.push(`إخفاقات متتالية: ${i.consecutive_failures}`);
  return escapeText(parts.join(" · "));
}

// The domain filter has no control of its own in the bar: it is set by
// clicking a link's domain or its "similar" button, and cleared from the
// notice under the bar. A permanently visible free-text domain box would be
// a worse version of the search field that already matches URLs.
let activeDomain = null;

function currentParams() {
  const params = new URLSearchParams();
  const q = document.getElementById("q").value;
  const category = document.getElementById("category").value;
  const sort = document.getElementById("sort").value;
  const alive = document.getElementById("aliveFilter").value;
  const channelId = document.getElementById("channelFilter").value;
  const language = document.getElementById("languageFilter").value;
  if (q) params.set("q", q);
  if (category) params.set("category", category);
  if (sort && sort !== "date") params.set("sort", sort);
  if (document.getElementById("favoriteOnly").checked) params.set("favorite", "true");
  if (alive) params.set("alive", alive);
  if (channelId) params.set("channel_id", channelId);
  if (language) params.set("language", language);
  if (activeDomain) params.set("domain", activeDomain);
  if (document.getElementById("includeArchived").checked) params.set("include_archived", "true");
  return params;
}

// Highlighting is done by splitting on the matched term and rebuilding with
// <mark>, never by string-replacing inside already-escaped HTML: doing it
// the other way round would let a message body containing "&lt;" reassemble
// into a real tag.
function highlighted(text, terms) {
  const escaped = escapeText(text);
  if (!terms.length) return escaped;
  const pattern = terms
    .map(t => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .filter(Boolean)
    .join("|");
  if (!pattern) return escaped;
  return escaped.replace(new RegExp("(" + pattern + ")", "gi"), "<mark>$1</mark>");
}

function positiveTerms() {
  // Mirrors app/search.py::parse_query — a "-term" is excluded, so
  // highlighting it would point at the very thing the user filtered out.
  return document.getElementById("q").value.split(/\s+/).filter(t => t && !t.startsWith("-"));
}

function contextLine(i, terms) {
  if (!i.raw_text) return "";
  const trimmed = i.raw_text.length > 240 ? i.raw_text.slice(0, 240) + "…" : i.raw_text;
  return `<div class="muted gap-top-s small">${highlighted(trimmed, terms)}</div>`;
}

// A link that is starred, confirmed working and collected in the last week
// is the best thing in a result list; nothing in the row said so.
function standoutBadge(i) {
  const weekAgo = Date.now() - 7 * 24 * 3600 * 1000;
  const fresh = i.created_at && new Date(i.created_at).getTime() >= weekAgo;
  if (!(i.is_favorite && i.is_alive === true && fresh)) return "";
  return `<span class="muted" title="مفضّلة وحيّة وحديثة">✨</span>`;
}

// Every automatic tier explained once, where the value is shown, rather
// than in documentation nobody opens.
const CLASSIFIER_EXPLANATION = {
  rules: "قواعد محلية: امتداد الملف أو كلمة مفتاحية في نص الرسالة. بلا شبكة وبلا تكلفة.",
  llm: "نموذج لغوي على الطبقة المجانية، يُستدعى فقط حين تكون ثقة القواعد منخفضة.",
  manual: "تصحيح بشري. لا تعيد أي طبقة تلقائية الكتابة فوقه.",
};

function classifierTitle(i) {
  return escapeText((CLASSIFIER_EXPLANATION[i.classified_by] || "") + " — " + classificationReason(i));
}

// Icon-only buttons need a real accessible name; the emoji alone reads as
// "star" or nothing at all. title= is a tooltip, not a name, so both are
// set — aria-label names it, title explains it on hover.
function linkCard(i, terms) {
  const favLabel = i.is_favorite ? "أزل من المفضّلة" : "أضف إلى المفضّلة";
  const archLabel = i.is_archived ? "أعد من الأرشيف" : "أرشف (إخفاء من النتائج دون حذف)";
  const compact = VIEW_MODE === "compact";
  return `<div class="card${compact ? " compact" : ""}">
       <a href="/links/${i.id}/open" target="_blank" rel="noopener" title="${escapeText(i.url)}">${highlighted(i.url, terms)}</a>
       ${compact ? "" : contextLine(i, terms)}
       ${noteLine(i)}
       <div class="wrap-row">
         <label class="sr-only" for="cat-${i.id}">تصنيف ${escapeText(i.domain)}</label>
         <select id="cat-${i.id}" data-action="recategorize" data-args='[${i.id}]' data-pass-value>
           ${CATEGORIES.map(c => `<option value="${c}" ${c === i.category ? "selected" : ""}>${c}</option>`).join("")}
         </select>
         <span class="muted" title="${classifierTitle(i)}">${i.classified_by} · ${(i.confidence*100).toFixed(0)}%</span>
         ${compact ? "" : originBadge(i)}
         <span class="muted" title="${vitalityTitle(i)}">${vitalityBadge(i)}</span>
         ${standoutBadge(i)}
         <button data-action="copyLink" data-args='["${escapeText(i.url)}"]' aria-label="انسخ الرابط" title="انسخ الرابط">⧉</button>
         <button data-action="editNote" data-args='[${i.id}]' aria-label="${i.notes ? "عدّل الملاحظة" : "أضف ملاحظة"}" title="${i.notes ? "عدّل الملاحظة" : "أضف ملاحظة"}">${i.notes ? "📝" : "✎"}</button>
         <button data-action="togglePin" data-args='[${i.id}, ${!i.is_pinned}]' aria-label="${i.is_pinned ? "أزل التثبيت" : "ثبّت كمرجع دائم"}" aria-pressed="${!!i.is_pinned}" title="${i.is_pinned ? "أزل التثبيت" : "ثبّت كمرجع دائم"}">${i.is_pinned ? "📌" : "📎"}</button>
         <button data-action="showSimilar" data-args='["${escapeText(i.domain)}", "${i.category}"]' title="روابط أخرى من نفس النطاق والتصنيف">مشابهة</button>
         <button data-action="toggleFavorite" data-args='[${i.id}, ${!i.is_favorite}]' aria-label="${favLabel}" aria-pressed="${!!i.is_favorite}" title="${favLabel}">${i.is_favorite ? "★" : "☆"}</button>
         <button data-action="toggleArchive" data-args='[${i.id}, ${!i.is_archived}]' aria-label="${archLabel}" title="${archLabel}">${i.is_archived ? "↩" : "🗄"}</button>
         <button data-action="removeLink" data-args='[${i.id}]' aria-label="احذف الرابط" class="push-end">حذف</button>
       </div>
     </div>`;
}

function noteLine(i) {
  if (!i.notes) return "";
  return `<div class="muted gap-top-s">📝 ${escapeText(i.notes)}</div>`;
}

async function editNote(id) {
  const current = (CURRENT_ITEMS[id] || {}).notes || "";
  const notes = prompt("ملاحظتك على هذا الرابط (اتركها فارغة لحذفها):", current);
  if (notes === null) return;
  await api(`/links/${id}/notes`, { method: "PATCH", body: JSON.stringify({ notes }) });
  announce(notes.trim() ? "حُفظت الملاحظة." : "حُذفت الملاحظة.");
  search(false);
}

async function togglePin(id, makePinned) {
  await api(`/links/${id}/pin?is_pinned=${makePinned}`, { method: "POST" });
  announce(makePinned ? "ثُبّت الرابط." : "أُزيل التثبيت.");
  search(false);
}

async function copyLink(url) {
  try {
    await navigator.clipboard.writeText(url);
    announce("نُسخ الرابط.");
  } catch (e) {
    prompt("انسخ الرابط يدوياً:", url);
  }
}

// One place that both shows a message and announces it, so a status can
// never end up visible-only or announced-only.
function announce(text) {
  document.getElementById("srStatus").textContent = text;
}

// Suggesting categories that *have* links beats an empty page: the usual
// cause of no results is a filter combination, not an empty collection.
async function emptyStateHtml() {
  const res = await api("/links/stats");
  const data = await res.json();
  const populated = Object.entries(data.by_category).sort((a, b) => b[1] - a[1]).slice(0, 5);
  if (!data.total_links) return "<p class='muted'>لا روابط بعد في مساحة العمل هذه.</p>";
  const chips = populated
    .map(([c, n]) => `<button data-action="jumpToCategory" data-args='["${c}"]'>${c} (${n})</button>`)
    .join(" ");
  return `<p class='muted'>لا نتائج بهذه الفلاتر. تصنيفات فيها روابط:</p><div class="row">${chips}</div>`;
}

function jumpToCategory(category) {
  document.getElementById("q").value = "";
  document.getElementById("category").value = category;
  clearDomain(false);
  search();
}

async function search(resetPage = true) {
  if (resetPage) currentPage = 1;
  const params = currentParams();
  params.set("page", currentPage);
  const res = await api("/links?" + params.toString());
  const data = await res.json();
  const el = document.getElementById("results");
  const terms = positiveTerms();
  CURRENT_ITEMS = Object.fromEntries(data.items.map(i => [i.id, i]));

  el.className = VIEW_MODE === "grid" ? "grid-results" : "";
  if (data.items.length) {
    el.innerHTML = data.items.map(i => linkCard(i, terms)).join("");
  } else {
    el.innerHTML = await emptyStateHtml();
  }

  renderActiveDomain();
  writePermalink(params);
  rememberFilters(params);
  document.getElementById("liveCount").textContent = "";
  document.getElementById("pageInfo").textContent =
    data.total === 0 ? "" : `صفحة ${data.page} — عرض ${data.items.length} من ${data.total}`;
  document.getElementById("prevBtn").disabled = data.page <= 1;
  document.getElementById("nextBtn").disabled = data.page * data.page_size >= data.total;
}

function showSimilar(domain, category) {
  activeDomain = domain;
  document.getElementById("category").value = category;
  document.getElementById("q").value = "";
  search();
}

function clearDomain(rerun = true) {
  activeDomain = null;
  if (rerun) search();
}

function renderActiveDomain() {
  const el = document.getElementById("activeDomain");
  if (!activeDomain) { el.textContent = ""; return; }
  el.innerHTML = `مقصور على النطاق: <strong>${escapeText(activeDomain)}</strong> `
    + `<button data-action="clearDomain">إلغاء</button>`;
}

// --- shareable filter permalink -------------------------------------------

function writePermalink(params) {
  // replaceState, not pushState: every keystroke-driven search would
  // otherwise add a history entry and make the back button useless.
  const query = params.toString();
  history.replaceState(null, "", query ? "?" + query : location.pathname);
}

function applyPermalink() {
  const params = new URLSearchParams(location.search);
  if (![...params.keys()].length) return false;
  const setValue = (id, key) => { const v = params.get(key); if (v !== null) document.getElementById(id).value = v; };
  setValue("q", "q");
  setValue("category", "category");
  setValue("sort", "sort");
  setValue("aliveFilter", "alive");
  setValue("channelFilter", "channel_id");
  setValue("languageFilter", "language");
  document.getElementById("favoriteOnly").checked = params.get("favorite") === "true";
  document.getElementById("includeArchived").checked = params.get("include_archived") === "true";
  activeDomain = params.get("domain");
  currentPage = Math.max(1, parseInt(params.get("page") || "1", 10) || 1);
  return true;
}

async function copyPermalink() {
  const url = location.origin + location.pathname + "?" + currentParams().toString();
  try {
    await navigator.clipboard.writeText(url);
    alert("نُسخ رابط الفلاتر.");
  } catch (e) {
    // Clipboard access is refused outside a secure context or without a
    // user gesture the browser accepts; showing the URL still gets the job
    // done rather than failing silently.
    prompt("انسخ الرابط يدوياً:", url);
  }
}

// --- saved searches --------------------------------------------------------

async function loadSavedSearches() {
  const res = await api("/links/saved");
  const rows = await res.json();
  const el = document.getElementById("savedSearches");
  if (!rows.length) { el.textContent = "—"; return; }
  el.innerHTML = rows.map(r =>
    `<button data-action="applySavedSearch" data-args='[${r.id}]'>${escapeText(r.name)}</button>`
    + `<button data-action="deleteSavedSearch" data-args='[${r.id}]' title="حذف البحث المحفوظ">×</button>`
  ).join(" ");
  SAVED_BY_ID = Object.fromEntries(rows.map(r => [r.id, r]));
}

let SAVED_BY_ID = {};
let CURRENT_ITEMS = {};

function applySavedSearch(id) {
  const row = SAVED_BY_ID[id];
  if (!row) return;
  const params = new URLSearchParams(row.filters);
  history.replaceState(null, "", "?" + params.toString());
  applyPermalink();
  search();
}

async function saveCurrentSearch() {
  const name = prompt("اسم البحث المحفوظ:");
  if (!name) return;
  const filters = Object.fromEntries(currentParams());
  delete filters.page;
  const res = await api("/links/saved", { method: "POST", body: JSON.stringify({ name, filters }) });
  if (!res.ok) { alert((await res.json()).detail || "تعذّر الحفظ."); return; }
  loadSavedSearches();
}

async function deleteSavedSearch(id) {
  await api(`/links/saved/${id}`, { method: "DELETE" });
  loadSavedSearches();
}

// --- view mode and discovery ----------------------------------------------

// Three densities, cycled by one button rather than three: list (full
// context), grid (cards side by side), compact (one row per link, for
// scanning a long result set). Declared before the initialiser below,
// which reads them.
const VIEW_MODES = ["list", "grid", "compact"];
const VIEW_LABEL = { list: "عرض شبكي", grid: "عرض مضغوط", compact: "عرض قائمة" };

let VIEW_MODE = "list";
try {
  const stored = localStorage.getItem("viewMode");
  // Validated, not trusted: a value left behind by an older build (or an
  // edited localStorage) would otherwise put the page in a mode that has
  // no styling and no way back except clearing site data.
  if (VIEW_MODES.includes(stored)) VIEW_MODE = stored;
} catch (e) { /* private mode */ }

// Three densities, cycled by one button rather than three: list (full
// context), grid (cards side by side), compact (one row per link, for
// scanning a long result set).
function toggleView() {
  VIEW_MODE = VIEW_MODES[(VIEW_MODES.indexOf(VIEW_MODE) + 1) % VIEW_MODES.length];
  try { localStorage.setItem("viewMode", VIEW_MODE); } catch (e) { /* private mode */ }
  refreshViewButton();
  search(false);
}

function refreshViewButton() {
  document.getElementById("viewBtn").textContent = VIEW_LABEL[VIEW_MODE] || VIEW_LABEL.list;
}

async function discover() {
  const res = await api("/links/random?count=5");
  const items = await res.json();
  const el = document.getElementById("results");
  el.className = VIEW_MODE === "grid" ? "grid-results" : "";
  el.innerHTML = items.length
    ? "<p class='muted'>خمسة روابط عشوائية:</p>" + items.map(i => linkCard(i, [])).join("")
    : "<p class='muted'>لا روابط بعد.</p>";
  document.getElementById("pageInfo").textContent = "";
}

let ACCOUNTS = [];

async function loadAccounts() {
  const res = await api("/channels/accounts");
  ACCOUNTS = await res.json();
  renderAccountHealth();
}

// A silently failing collecting account is the same class of problem as a
// stopped collector: everything looks fine and nothing arrives. This panel
// is the only place that says which account is broken and why.
function renderAccountHealth() {
  const el = document.getElementById("accountHealth");
  if (!ACCOUNTS.length) {
    el.innerHTML = "<p class='muted'>لا حسابات جمع مسجّلة. النظام يعمل يدوياً بدونها.</p>";
    return;
  }
  el.innerHTML = ACCOUNTS.map(a => {
    const when = t => t ? new Date(t).toLocaleString("ar") : "لم يحدث بعد";
    // An automatic disable and one a human chose need different responses,
    // so they are shown differently rather than both as "معطّل".
    const state = a.is_active
      ? "🟢 نشط"
      : (a.disabled_reason ? "🔴 عُطِّل تلقائياً" : "⚪ معطّل يدوياً");
    const problem = a.disabled_reason || a.last_error;
    return `<div class="card">
      <strong>${escapeText(a.label)}</strong> — ${state}
      <div class="muted gap-top-s">
        آخر نجاح: ${when(a.last_success_at)} ·
        آخر فشل: ${when(a.last_failure_at)} ·
        إخفاقات متتالية: ${a.consecutive_failures} ·
        القنوات: ${a.channel_count} ·
        الروابط المجموعة: ${a.links_collected.toLocaleString("ar")}
      </div>
      ${problem ? `<div class="error gap-top-s">${escapeText(problem)}</div>` : ""}
      ${a.is_active ? "" : `<button data-action="reactivateAccount" data-args='[${a.id}]' class="gap-top">أعد التفعيل بعد إصلاح السبب</button>`}
    </div>`;
  }).join("");
}

async function reactivateAccount(id) {
  await api(`/channels/accounts/${id}/reactivate`, { method: "POST" });
  announce("أُعيد تفعيل الحساب.");
  loadAccounts();
}

function accountOptions(selectedId) {
  const auto = `<option value="" ${selectedId == null ? "selected" : ""}>الحساب الافتراضي</option>`;
  return auto + ACCOUNTS.map(a =>
    `<option value="${a.id}" ${a.id === selectedId ? "selected" : ""}>${a.label}${a.is_active ? "" : " (معطّل)"}</option>`
  ).join("");
}

async function reassignChannel(channelId, value) {
  const account_id = value === "" ? null : Number(value);
  await api(`/channels/${channelId}`, { method: "PATCH", body: JSON.stringify({ account_id }) });
  loadChannels();
}

function fillChannelFilter(channels) {
  const select = document.getElementById("channelFilter");
  const previous = select.value;
  select.innerHTML = '<option value="">كل القنوات</option>' + channels.map(c =>
    `<option value="${c.id}">${c.title || c.username || c.tg_channel_id}</option>`
  ).join("");
  // Keep the active filter selected across reloads; if the channel is gone,
  // the value simply falls back to "all channels" rather than filtering on
  // an id that no longer exists.
  select.value = previous;
}

async function loadChannels() {
  const res = await api("/channels");
  const data = await res.json();
  fillChannelFilter(data);
  document.getElementById("channels").innerHTML = data.map(c =>
    `<div class="card">
       ${c.title || c.username || c.tg_channel_id}
       <span class="muted">${c.is_active ? "نشطة" : "متوقفة"}</span>
       ${ACCOUNTS.length > 1 ? `<select data-action="reassignChannel" data-args='[${c.id}]' data-pass-value class="indent-s">${accountOptions(c.account_id)}</select>` : ""}
     </div>`
  ).join("") || "<p class='muted'>لا توجد قنوات مضافة بعد</p>";
}

async function loadTotp() {
  const res = await api("/auth/totp");
  if (!res.ok) return;
  const s = await res.json();
  const status = document.getElementById("totpStatus");
  const controls = document.getElementById("totpControls");
  if (s.enabled) {
    const warn = s.recovery_codes_remaining === 0
      ? " <strong>لم يتبقَّ أي رمز استرداد — ولّد رموزاً جديدة الآن.</strong>"
      : "";
    status.innerHTML = `مفعّل · ${s.recovery_codes_remaining} رمز استرداد متبقٍّ${warn}`;
    controls.innerHTML =
      `<button data-action="regenerateRecovery">رموز استرداد جديدة</button>` +
      `<button data-action="disableTotp" class="danger">تعطيل</button>`;
  } else {
    status.textContent = "غير مفعّل — كلمة المرور وحدها تكفي للدخول.";
    controls.innerHTML = `<button data-action="startTotp">تفعيل التحقّق بخطوتين</button>`;
  }
}

async function startTotp() {
  const res = await api("/auth/totp/setup", { method: "POST" });
  if (!res.ok) { document.getElementById("totpStatus").textContent = (await res.json()).detail; return; }
  const data = await res.json();
  document.getElementById("totpSecret").textContent = data.secret;
  document.getElementById("totpUri").textContent = data.otpauth_uri;
  document.getElementById("totpSetup").hidden = false;
  document.getElementById("totpCode").focus();
}

async function enableTotp() {
  const code = document.getElementById("totpCode").value.trim();
  const res = await api("/auth/totp/enable", { method: "POST", body: JSON.stringify({ code }) });
  if (!res.ok) { document.getElementById("totpStatus").textContent = (await res.json()).detail; return; }
  const data = await res.json();
  document.getElementById("totpSetup").hidden = true;
  document.getElementById("totpCode").value = "";
  showRecoveryCodes(data.recovery_codes);
  loadTotp();
}

function showRecoveryCodes(codes) {
  document.getElementById("recoveryList").textContent = codes.join("\n");
  document.getElementById("recoveryCodes").hidden = false;
}

async function regenerateRecovery() {
  if (!confirm("سيتوقّف العمل بكل رموز الاسترداد القديمة. متابعة؟")) return;
  const res = await api("/auth/totp/recovery-codes", { method: "POST" });
  if (!res.ok) return;
  showRecoveryCodes((await res.json()).recovery_codes);
  loadTotp();
}

async function disableTotp() {
  const current_password = prompt("كلمة المرور الحالية لتأكيد التعطيل:");
  if (!current_password) return;
  const res = await api("/auth/totp/disable", { method: "POST", body: JSON.stringify({ current_password }) });
  document.getElementById("totpStatus").textContent = res.ok ? "" : "كلمة المرور غير صحيحة";
  if (res.ok) { document.getElementById("recoveryCodes").hidden = true; loadTotp(); }
}

function downloadSecurityLog() {
  // A plain navigation, so the browser handles Content-Disposition itself
  // rather than the page buffering the whole file into memory first.
  window.location.href = "/auth/me/security-export";
}

async function loadApiKeys() {
  const res = await api("/auth/api-keys");
  if (!res.ok) return;
  const keys = await res.json();
  document.getElementById("apiKeys").innerHTML = keys.map(k =>
    `<div class="row spread">
       <span><code>${escapeText(k.prefix)}…</code> ${escapeText(k.name)}
         <span class="muted">${k.use_count} استخدام${k.last_used_at ? " — آخره " + new Date(k.last_used_at).toLocaleString("ar") : " — لم يُستخدم بعد"}</span>
       </span>
       <button data-action="revokeApiKey" data-args='[${k.id}]'>إبطال</button>
     </div>`
  ).join("") || "<p class='muted'>لا توجد مفاتيح</p>";
}

async function createApiKey() {
  const nameField = document.getElementById("apiKeyName");
  const name = nameField.value.trim();
  const out = document.getElementById("apiKeyResult");
  if (!name) { out.textContent = "اكتب اسماً للمفتاح أولاً"; return; }

  const res = await api("/auth/api-keys", { method: "POST", body: JSON.stringify({ name }) });
  if (!res.ok) { out.textContent = (await res.json()).detail || "تعذّر الإنشاء"; return; }

  const data = await res.json();
  nameField.value = "";
  // Shown once and never again — the server keeps only a hash, so there
  // is no later screen that could display it.
  out.innerHTML = `<strong>انسخه الآن — لن يُعرض مرة أخرى:</strong> <code>${escapeText(data.key)}</code>`;
  loadApiKeys();
}

async function revokeApiKey(id) {
  if (!confirm("إبطال هذا المفتاح؟ أي سكربت يستخدمه سيتوقّف فوراً.")) return;
  await api(`/auth/api-keys/${id}`, { method: "DELETE" });
  document.getElementById("apiKeyResult").textContent = "";
  loadApiKeys();
}

async function loadWorkspace() {
  // /auth/me/summary carries the workspace name plus the counts below, so
  // this panel costs one request instead of four.
  const res = await api("/auth/me/summary");
  const data = await res.json();
  document.getElementById("wsName").value = data.workspace_name || "";
  const since = new Date(data.member_since).toLocaleDateString("ar");
  const accounts = data.disabled_accounts
    ? `${data.active_accounts} حساب تجميع نشط و${data.disabled_accounts} معطّل`
    : `${data.active_accounts} حساب تجميع`;
  document.getElementById("accountSummary").textContent =
    `${data.email} — منذ ${since} · ${data.total_links} رابطاً في ${data.total_channels} قناة · ` +
    `${accounts} · ${data.active_sessions} جلسة مفتوحة`;
}

async function renameWorkspace() {
  const name = document.getElementById("wsName").value.trim();
  const out = document.getElementById("wsResult");
  if (!name) { out.textContent = "الاسم لا يمكن أن يكون فارغاً."; return; }
  const res = await api("/auth/workspace", { method: "PATCH", body: JSON.stringify({ name }) });
  if (!res.ok) {
    const err = await res.json();
    out.textContent = (err.detail && err.detail.toString()) || "تعذّرت إعادة التسمية.";
    return;
  }
  out.textContent = `تم تغيير الاسم إلى «${(await res.json()).name}».`;
}

function exportWorkspace() {
  window.location = "/auth/me/export";
}

async function deleteWorkspace() {
  if (!confirm("سيُحذف كل شيء نهائياً: الروابط والقنوات والمستخدمون. لا يمكن التراجع. متابعة؟")) return;
  const current_password = prompt("أدخل كلمة المرور الحالية للتأكيد:");
  if (!current_password) return;
  const confirmText = prompt("اكتب DELETE بالأحرف الكبيرة لتأكيد الحذف النهائي:");
  if (confirmText !== "DELETE") { alert("لم يتم التأكيد — أُلغيت العملية."); return; }
  const res = await api("/auth/me/delete", { method: "POST", body: JSON.stringify({ current_password, confirm: confirmText }) });
  if (!res.ok) {
    const err = await res.json();
    alert(err.detail || "تعذّر الحذف.");
    return;
  }
  alert("تم حذف مساحة العمل.");
  window.location.href = "/login";
}

async function addChannel() {
  const tg_channel_id = document.getElementById("chId").value.trim();
  const username = document.getElementById("chUsername").value.trim() || null;
  if (!tg_channel_id) return;
  await api("/channels", { method: "POST", body: JSON.stringify({ tg_channel_id, username }) });
  document.getElementById("chId").value = "";
  document.getElementById("chUsername").value = "";
  loadChannels();
}

// Mirrors app/classifier/rules.py::URL_RE, including the excluded
// characters, so the number shown while typing is the number the server
// will actually store. A looser pattern here would promise links the
// extractor then discards.
const PASTE_URL_RE = /https?:\/\/[^\s<>"'()\[\]]+/gi;

function updatePasteCount() {
  const text = document.getElementById("pasteBox").value;
  // Counted as a set: the server dedupes within a single message, so
  // showing 5 for a message repeating one link would be a lie.
  const found = new Set((text.match(PASTE_URL_RE) || []).map(u => u.replace(/[.,،!?؟:;»)\]]+$/, "")));
  const el = document.getElementById("pasteCount");
  el.textContent = found.size ? `${found.size} رابط في النص` : "";
}

async function addLinks() {
  const box = document.getElementById("pasteBox");
  const text = box.value.trim();
  const out = document.getElementById("addResult");
  if (!text) { out.textContent = "الصق نصاً أولاً."; return; }
  out.textContent = "جارٍ التصنيف...";
  const res = await api("/links", { method: "POST", body: JSON.stringify({ text }) });
  const data = await res.json();
  if (data.found === 0) {
    out.textContent = "لم يُعثر على أي رابط في النص.";
    return;
  }
  out.textContent = `وُجد ${data.found} رابط — أُضيف ${data.stored} جديد، و${data.duplicates} موجود مسبقاً.`;
  box.value = "";
  loadStats();
  search();
  loadChannels();
}

async function linkBot() {
  const res = await api("/bot/link-code", { method: "POST" });
  const data = await res.json();
  document.getElementById("botCode").textContent = data.instructions;
}

async function changePassword() {
  const current_password = document.getElementById("curPass").value;
  const new_password = document.getElementById("newPass").value;
  const out = document.getElementById("passResult");
  const res = await api("/auth/change-password", { method: "POST", body: JSON.stringify({ current_password, new_password }) });
  if (!res.ok) {
    const err = await res.json();
    out.textContent = err.detail || "فشل تغيير كلمة المرور.";
    return;
  }
  const data = await res.json();
  out.textContent = `تم تغيير كلمة المرور. تم تسجيل الخروج من ${data.other_sessions_revoked} جلسة أخرى.`;
  document.getElementById("curPass").value = "";
  document.getElementById("newPass").value = "";
}

async function logoutAll() {
  if (!confirm("تسجيل الخروج من كل الأجهزة بما فيها هذا الجهاز؟")) return;
  await api("/auth/logout-all", { method: "POST" });
  window.location.href = "/login";
}

const SOURCE_LABEL = { text: "من نص الرسالة", button: "من زر", hyperlink: "من رابط مخفي" };

function classificationReason(i) {
  // Shown as a tooltip on the confidence badge: "why did it decide this?".
  // matched_rule is null for links stored before the column existed, and
  // saying so is more useful than an empty tooltip.
  const rule = i.matched_rule ? escapeText(i.matched_rule) : "غير مسجَّلة (رابط أقدم من هذه الميزة)";
  return `القاعدة: ${rule}`;
}

function originBadge(i) {
  // forwarded_from is a channel title chosen by whoever wrote it, so it is
  // escaped; source_type is one of a fixed set the server controls.
  const parts = [];
  if (i.source_type && i.source_type !== "text") {
    parts.push(`<span class="muted">${SOURCE_LABEL[i.source_type] || escapeText(i.source_type)}</span>`);
  }
  if (i.forwarded_from) {
    parts.push(`<span class="muted">محوَّل عن: ${escapeText(i.forwarded_from)}</span>`);
  }
  return parts.join(" ");
}

function escapeText(value) {
  // Session origin comes from client-supplied headers. Rendering it as
  // HTML would let a crafted User-Agent inject markup into this page.
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

function describeOrigin(s) {
  if (!s.ip_address && !s.user_agent) return "الأصل غير مسجَّل (جلسة أقدم من هذه الميزة)";
  const ip = s.ip_address ? escapeText(s.ip_address) : "عنوان غير معروف";
  const ua = s.user_agent ? escapeText(s.user_agent.slice(0, 80)) : "متصفح غير معروف";
  return `${ip} · ${ua}`;
}

async function loadSecurityActivity() {
  const res = await api("/auth/security-activity");
  const d = await res.json();
  const el = document.getElementById("securityActivity");
  if (d.failed_attempts === 0) {
    el.textContent = `لا محاولات دخول فاشلة في آخر ${d.window_minutes} دقيقة.`;
    return;
  }
  el.textContent =
    `${d.failed_attempts} محاولة دخول فاشلة من ${d.distinct_ip_count} عنوان في آخر ${d.window_minutes} دقيقة ` +
    `(الحجب عند ${d.lockout_threshold}). آخر محاولة: ${new Date(d.last_failed_at).toLocaleString("ar")}.`;
}

async function loadSessions() {
  const res = await api("/auth/sessions");
  const data = await res.json();
  document.getElementById("sessions").innerHTML = data.map(s =>
    `<div class="card">
       ${s.is_current ? "<strong>هذا الجهاز</strong>" : "جهاز آخر"}
       <span class="muted">— بدأت ${new Date(s.created_at).toLocaleString("ar")}</span>
       <div class="muted">${describeOrigin(s)}</div>
       ${s.is_current ? "" : `<button data-action="revokeSession" data-args='[${s.id}]' class="indent-s">إنهاء</button>`}
     </div>`
  ).join("");
}

async function revokeSession(id) {
  await api(`/auth/sessions/${id}`, { method: "DELETE" });
  loadSessions();
}

// A permalink may set filters that only exist once the channel list has
// loaded; applying it before the first search is still correct because the
// select simply has no matching option yet and falls back to "all".
document.getElementById("pasteBox").addEventListener("input", updatePasteCount);

// --- keyboard shortcuts ----------------------------------------------------
//
// Deliberately only two, both conventional: "/" focuses search (the same
// key GitHub, Gmail and Slack use) and Escape backs out. A page full of
// invented single-key bindings is a trap for anyone typing into a field.
document.addEventListener("keydown", (event) => {
  const inField = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);

  if (event.key === "/" && !inField) {
    event.preventDefault();
    document.getElementById("q").focus();
    return;
  }

  if (event.key === "Escape") {
    // Escape means "undo the last thing", in the order the user would
    // expect it: cancel a pending delete first, then clear the search.
    if (pendingDelete) { undoDelete(); return; }
    const q = document.getElementById("q");
    if (document.activeElement === q && q.value) {
      q.value = "";
      search();
      announce("أُفرغ حقل البحث.");
    } else if (document.activeElement === q) {
      q.blur();
    }
  }
});

// --- live result count while typing (#123) ---------------------------------
//
// A count, not a full re-render: page_size=1 makes the server do the same
// COUNT it does anyway and return one row instead of twenty-five, so the
// keystroke feedback costs a fraction of a real search. Debounced so a
// typed word is one request, not one per letter.
let countTimer = null;

document.getElementById("q").addEventListener("input", () => {
  clearTimeout(countTimer);
  countTimer = setTimeout(async () => {
    const params = currentParams();
    params.set("page_size", "1");
    params.set("page", "1");
    try {
      const res = await api("/links?" + params.toString());
      if (!res.ok) return;
      const total = (await res.json()).total;
      const hint = document.getElementById("liveCount");
      hint.textContent = `${total} نتيجة`;
      announce(`${total} نتيجة`);
    } catch (e) { /* a failed preview must never block typing */ }
  }, 300);
});

// --- remembered filters (#126) ---------------------------------------------
//
// Only consulted when the URL carries no filters of its own: a shared
// permalink must show what it says, not what this browser last looked at.
function rememberFilters(params) {
  try { localStorage.setItem("lastFilters", params.toString()); } catch (e) { /* private mode */ }
}

function restoreRememberedFilters() {
  try {
    const stored = localStorage.getItem("lastFilters");
    if (!stored) return false;
    history.replaceState(null, "", "?" + stored);
    return applyPermalink();
  } catch (e) {
    return false;
  }
}

if (!applyPermalink()) restoreRememberedFilters();
refreshViewButton();
loadStats();
search();
loadSavedSearches();
loadAccounts().then(loadChannels);
loadSessions();
loadSecurityActivity();
loadWorkspace();
loadApiKeys();
loadTotp();
