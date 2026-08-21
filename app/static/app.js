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

document.addEventListener("click", function (event) {
  var el = event.target.closest("[data-action]");
  // Ignore controls whose action fires on change instead, so a click on a
  // <select> does not run its handler before a value has been chosen.
  if (el && el.tagName !== "SELECT") dispatchAction(el, event);
});

document.addEventListener("change", function (event) {
  var el = event.target.closest("[data-action]");
  if (el && (el.tagName === "SELECT" || el.type === "checkbox")) dispatchAction(el, event);
});

applyTheme(storedTheme());
