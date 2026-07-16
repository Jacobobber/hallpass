"""A self-contained admin dashboard: one static HTML page, no build step, no CDN.

It is a *pure client* of the gated control-plane API -- it holds no privilege of
its own. The operator pastes a bearer token; every panel fetches an ``/admin/*``
endpoint with that bearer, so the same verify -> admin-scope -> audit path gates
the dashboard exactly as it gates a direct API call. Serving the page itself is
unauthenticated (it is just a shell); nothing it shows arrives without the token.
"""

from __future__ import annotations

__all__ = ["DASHBOARD_HTML"]

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hallpass control</title>
<style>
  :root { color-scheme: light dark; font-family: ui-sans-serif, system-ui, sans-serif; }
  body { margin: 0; padding: 1.5rem; max-width: 60rem; margin-inline: auto; line-height: 1.5; }
  h1 { font-size: 1.25rem; margin: 0 0 1rem; }
  .muted { opacity: 0.7; font-size: 0.85rem; }
  .row { display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; margin-bottom: 1rem; }
  input, button { font: inherit; padding: 0.4rem 0.6rem; border-radius: 0.4rem;
    border: 1px solid gray; background: transparent; color: inherit; }
  input { flex: 1; min-width: 16rem; }
  button { cursor: pointer; }
  section { border: 1px solid; border-color: color-mix(in srgb, currentColor 20%, transparent);
    border-radius: 0.5rem; padding: 0.75rem 1rem; margin-bottom: 1rem; }
  section h2 { font-size: 0.95rem; margin: 0 0 0.5rem; }
  pre { overflow-x: auto; margin: 0; font-size: 0.8rem; white-space: pre-wrap; word-break: break-word; }
  .err { color: #c0392b; }
</style>
</head>
<body>
<h1>hallpass control plane</h1>
<p class="muted">A pure client of the gated <code>/admin/*</code> API. Paste an admin
bearer token; every action is verified, scope-checked, and audited server-side.
Deny is opaque (a missing scope reads as <code>not found</code>).</p>

<div class="row">
  <input id="tok" type="password" placeholder="Bearer token (with admin:* scopes)" autocomplete="off">
</div>

<section>
  <h2>Queue <button data-get="/admin/queue" data-out="queue">refresh</button></h2>
  <pre id="queue" class="muted">—</pre>
</section>

<section>
  <h2>Pending human gates <button data-get="/admin/gates" data-out="gates">refresh</button></h2>
  <pre id="gates" class="muted">—</pre>
  <div class="row">
    <input id="gate_id" placeholder="gate id">
    <button id="approve">approve</button>
    <button id="deny">deny</button>
  </div>
</section>

<section>
  <h2>Revoked agents <button data-get="/admin/revoked" data-out="revoked">refresh</button></h2>
  <pre id="revoked" class="muted">—</pre>
  <div class="row">
    <input id="subject" placeholder="agent subject">
    <button id="revoke">revoke</button>
    <button id="restore">restore</button>
  </div>
</section>

<section>
  <h2>Audit tail <button data-get="/admin/audit?limit=25" data-out="audit">refresh</button></h2>
  <pre id="audit" class="muted">—</pre>
</section>

<script>
const tok = () => document.getElementById("tok").value.trim();
const show = (id, data, err) => {
  const el = document.getElementById(id);
  el.className = err ? "err" : "";
  el.textContent = err ? err : JSON.stringify(data, null, 2);
};
async function call(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Authorization": "Bearer " + tok(),
               ...(body ? {"Content-Type": "application/json"} : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((data && data.error) || ("HTTP " + res.status));
  return data;
}
document.querySelectorAll("button[data-get]").forEach(b =>
  b.addEventListener("click", async () => {
    try { show(b.dataset.out, await call("GET", b.dataset.get)); }
    catch (e) { show(b.dataset.out, null, e.message); }
  }));
const post = async (path, body, out) => {
  try { show(out, await call("POST", path, body)); }
  catch (e) { show(out, null, e.message); }
};
document.getElementById("revoke").onclick = () =>
  post("/admin/revoke", { subject: document.getElementById("subject").value.trim() }, "revoked");
document.getElementById("restore").onclick = () =>
  post("/admin/restore", { subject: document.getElementById("subject").value.trim() }, "revoked");
document.getElementById("approve").onclick = () =>
  post("/admin/gates/decide", { gate_id: document.getElementById("gate_id").value.trim(), approved: true }, "gates");
document.getElementById("deny").onclick = () =>
  post("/admin/gates/decide", { gate_id: document.getElementById("gate_id").value.trim(), approved: false }, "gates");
</script>
</body>
</html>
"""
