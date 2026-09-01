// The second factor has to be reachable from here.
//
// The server has required a TOTP code on login since two-factor shipped —
// ErrorCode.totp_required — and this page had no field to type one into.
// Anyone who switched two-factor on could not sign in through the browser
// at all; the only visible symptom was a login that kept "failing" with a
// message about a code the page never asked for.
//
// The field starts hidden because most accounts do not have a second
// factor, and revealing it only when the server asks keeps the common
// case a two-field form.
document.getElementById("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const err = document.getElementById("err");
  const totpField = document.getElementById("totpField");
  const code = (form.get("totp_code") || "").trim();

  const res = await fetch("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      email: form.get("email"),
      password: form.get("password"),
      // Omitted rather than sent empty: the server treats a blank code as
      // a wrong one, which would burn a throttle attempt on the first
      // submit of every two-factor account.
      totp_code: code || null,
    }),
  });

  if (res.ok) { window.location.href = "/dashboard"; return; }

  const data = await res.json().catch(() => ({}));
  if (res.headers.get("X-Error-Code") === "totp_required" || data.error_code === "totp_required") {
    totpField.hidden = false;
    document.getElementById("totpCode").focus();
    err.textContent = code
      ? "الرمز غير صحيح. جرّب الرمز التالي، أو أحد رموز الاسترداد."
      : "هذا الحساب محميّ بالتحقّق بخطوتين — أدخل الرمز أعلاه.";
    return;
  }
  err.textContent = data.detail || "فشل الدخول";
});
