// Runs before first paint, so it must be a blocking script in <head>:
// reading the stored theme after the body renders shows a flash of the
// wrong palette on every load.
//
// External rather than inline since the Content-Security-Policy dropped
// 'unsafe-inline' (idea 85). A plain <script src> in <head> blocks
// rendering exactly like the inline version did, so the anti-flash
// property is unchanged.
//
// "system" is stored as the absence of the attribute, so the CSS media
// query stays in charge unless the person made an explicit choice.
(function () {
  try {
    var t = localStorage.getItem("theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* private mode: fall back to the OS preference */ }
})();
