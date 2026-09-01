// Shared behaviour for every page: the theme control, and the event
// delegation that replaced 48 inline on*= attributes.
//
// Why delegation rather than 48 addEventListener calls: most of those
// attributes sat on markup this page rebuilds constantly (every search
// re-renders the whole result list), so per-element listeners would have
// to be re-attached after every render — the bug that pattern always
// produces is a button that silently stops working after the second
// search. One listener on the document survives any amount of re-rendering.

var THEMES = ["system", "light", "dark"];
var THEME_LABEL = { system: "🖥️ تلقائي", light: "☀️ فاتح", dark: "🌙 داكن" };

function storedTheme() {
  try { return localStorage.getItem("theme") || "system"; } catch (e) { return "system"; }
}

function applyTheme(theme) {
  if (theme === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme);
  try {
    if (theme === "system") localStorage.removeItem("theme");
    else localStorage.setItem("theme", theme);
  } catch (e) { /* private mode: the choice just does not persist */ }
  var btn = document.getElementById("themeToggle");
  if (btn) btn.textContent = THEME_LABEL[theme];
}

// Signing out lives here rather than in dashboard.js because the header
// that offers it is on every page, and dashboard.js is loaded on one.
// POST, not a link: a GET that ends a session is a link a preloader or a
// prefetching browser can follow on its own.
function logout() {
  fetch("/auth/logout", { method: "POST" })
    .catch(function () { /* a failed call still leaves the page below */ })
    .then(function () { window.location.href = "/login"; });
}

function cycleTheme() {
  applyTheme(THEMES[(THEMES.indexOf(storedTheme()) + 1) % THEMES.length]);
}

// Every clickable/changeable control names its handler in data-action, and
// its literal arguments in data-args (JSON). data-pass-value appends the
// element's own value, which is what `this.value` used to do inline.
function dispatchAction(el, event) {
  var name = el.getAttribute("data-action");
  var fn = window[name];
  if (typeof fn !== "function") {
    console.error("no handler named", name);
    return;
  }
  var args = [];
  var raw = el.getAttribute("data-args");
  if (raw) {
    try {
      var parsed = JSON.parse(raw);
      args = Array.isArray(parsed) ? parsed : [parsed];
    } catch (e) {
      console.error("bad data-args on", name, raw);
      return;
    }
  }
  if (el.hasAttribute("data-pass-value")) args.push(el.value);
  event.preventDefault();
  fn.apply(null, args);
}

// Controls whose action belongs to "change", not "click": a select must
// not fire before a value is chosen, and a checkbox reports its new state
// only once the change event has run.
function isChangeDriven(el) {
  return (
    el.tagName === "SELECT" ||
    (el.tagName === "INPUT" && (el.type === "checkbox" || el.type === "radio"))
  );
}

document.addEventListener("click", function (event) {
  var el = event.target.closest("[data-action]");
  // Checkboxes are excluded here for a reason that cost a shipped
  // regression: this listener calls preventDefault(), and preventDefault()
  // on a checkbox's *click* event reverts the tick. The filter checkboxes
  // therefore stopped working the moment delegation replaced their inline
  // handlers — one click left the box unchecked and the filter unapplied,
  // with nothing in the console to say so.
  if (el && !isChangeDriven(el)) dispatchAction(el, event);
});

document.addEventListener("change", function (event) {
  var el = event.target.closest("[data-action]");
  if (el && isChangeDriven(el)) dispatchAction(el, event);
});

applyTheme(storedTheme());


// --- transient confirmation messages (idea 164) ---------------------------
//
// alert() blocks the whole page, has to be dismissed before anything else
// can happen, and is the wrong weight for "your 42 links were deleted" —
// a result you want to see, not something you must acknowledge.
//
// confirm() and prompt() are deliberately *not* replaced. They are used
// here only before irreversible actions (deleting a workspace, revoking a
// key), and their blocking, unmissable, unstyleable nature is the right
// weight for exactly those — an in-page dialog that looks like the rest
// of the interface is easier to click through by reflex.

var TOAST_MS = 4000;

function toast(message, kind) {
  var host = document.getElementById("toasts");
  if (!host) return;

  var el = document.createElement("div");
  el.className = "toast" + (kind === "error" ? " toast-error" : "");
  el.textContent = message;
  host.appendChild(el);

  // The host is aria-live, so a screen reader announces this without the
  // focus theft alert() caused. Errors are assertive; results are polite.
  host.setAttribute("aria-live", kind === "error" ? "assertive" : "polite");

  window.setTimeout(function () {
    el.remove();
  }, TOAST_MS);
}
