document.getElementById("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const res = await fetch("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: form.get("email"), password: form.get("password") }),
  });
  if (res.ok) { window.location.href = "/dashboard"; }
  else { const data = await res.json(); document.getElementById("err").textContent = data.detail || "فشل الدخول"; }
});
