// The admin dashboard (/admin): the department office changes the timetable each semester, faculty status,
// announcements, closed rooms, place info and door photos. API: /api/admin (admin_api.py).
"use strict";

const $ = (id) => document.getElementById(id);
const S = { me: null, places: [], place: null, faculty: [], pending: null };
const STATES = { on_leave: "On leave", out: "Out of office", meeting: "In a meeting", in: "In the cabin" };

async function call(path, { method = "GET", body, form } = {}) {
  const opts = { method, headers: {} };
  if (form) opts.body = form;
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  const res = await fetch(`/api/admin${path}`, opts);
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (res.status === 401 && S.me) { S.me = null; gate(); }
  if (!res.ok) {
    const detail = Array.isArray(data?.detail) ? data.detail.map((d) => d.msg).join("; ") : data?.detail;
    throw new Error(detail || `Something went wrong (HTTP ${res.status}).`);
  }
  return data;
}

function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined) n.append(kid.nodeType ? kid : String(kid));
  return n;
}

function flash(text, bad = false) {
  const f = $("flash");
  f.textContent = text;
  f.className = `flash${bad ? " bad" : ""}`;
  f.hidden = false;
  clearTimeout(flash.t);
  flash.t = setTimeout(() => { f.hidden = true; }, bad ? 9000 : 4000);
}

// run an action from a form or button: busy while it runs, the outcome in the flash line
async function act(button, fn, done) {
  if (button) button.disabled = true;
  try { const r = await fn(); if (done) flash(typeof done === "function" ? done(r) : done); return r; }
  catch (e) { flash(e.message, true); return undefined; }
  finally { if (button) button.disabled = false; }
}

// today as YYYY-MM-DD in local time (toISOString would give the UTC date: yesterday, just after midnight in India)
const localDay = (d = new Date()) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const when = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "");
const formData = (form) => Object.fromEntries([...new FormData(form)].map(([k, v]) => [k, typeof v === "string" ? v.trim() : v]));

// ---------- signing in ----------
async function gate() {
  $("app").hidden = true;
  $("who").hidden = $("signout").hidden = true;
  $("gate").hidden = false;
  const s = await call("/setup");
  $("setup").hidden = !(s.needs_setup && s.local);
  $("setup-remote").hidden = !(s.needs_setup && !s.local);
  $("signin").hidden = s.needs_setup;
  (s.needs_setup ? $("setup") : $("signin")).querySelector("input")?.focus();
}

async function signedIn(me) {
  S.me = me;
  $("gate").hidden = true;
  $("app").hidden = false;
  $("who").hidden = $("signout").hidden = false;
  $("who").textContent = `${me.name} · ${me.role === "admin" ? "department office" : "faculty"}`;
  for (const n of document.querySelectorAll("[data-admin]")) n.hidden = me.role !== "admin" || n.classList.contains("tab");
  show(me.role === "admin" ? (location.hash.slice(1) || "timetable") : "faculty");
}

for (const id of ["signin", "setup"]) {
  $(id).addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $(id).querySelector(".error");
    err.textContent = "";
    try { signedIn(await call(id === "signin" ? "/login" : "/setup", { method: "POST", body: formData($(id)) })); }
    catch (x) { err.textContent = x.message; }
  });
}
$("signout").addEventListener("click", async () => { await call("/logout", { method: "POST" }).catch(() => {}); S.me = null; gate(); });

// ---------- tabs ----------
const LOADERS = { timetable: loadTimetable, faculty: loadFaculty, announce: loadAnnouncements, places: loadPlaces,
  accounts: loadAccounts, log: loadLog };

function show(tab) {
  if (S.me?.role !== "admin" && !["faculty", "accounts"].includes(tab)) tab = "faculty";
  for (const b of document.querySelectorAll("[data-tab]")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  for (const s of document.querySelectorAll(".tab")) s.hidden = s.id !== `tab-${tab}`;
  if (S.me?.role === "admin") history.replaceState(null, "", `#${tab}`);
  LOADERS[tab]?.().catch((e) => flash(e.message, true));
}
for (const b of document.querySelectorAll("[data-tab]")) b.addEventListener("click", () => show(b.dataset.tab));

// ---------- the timetable ----------
async function loadTimetable() {
  const t = await call("/timetable");
  const cur = t.versions.find((v) => v.id === t.current);
  $("tt-current").replaceChildren(
    el("dt", {}, "File"), el("dd", {}, t.exists ? t.file : "None yet (rag-core's workbook is missing)"),
    el("dt", {}, "Faculty"), el("dd", {}, `${t.faculty} people`),
    el("dt", {}, "Published"), el("dd", {}, cur ? `${when(cur.uploaded_at)} by ${cur.uploaded_by}` : when(t.modified)),
    ...(cur?.note ? [el("dt", {}, "Note"), el("dd", {}, cur.note)] : []),
  );
  const body = $("tt-versions").querySelector("tbody");
  body.replaceChildren(...(t.versions.length ? t.versions.map((v) => el("tr", {},
    el("td", {}, when(v.uploaded_at), v.id === t.current ? el("span", { class: "badge ok" }, "in use") : null),
    el("td", {}, v.original_name), el("td", {}, v.uploaded_by), el("td", { class: "num" }, v.faculty),
    el("td", {}, v.note || ""),
    el("td", { class: "actions" },
      el("a", { href: `/api/admin/timetable/download/${encodeURIComponent(v.id)}` }, "Download"),
      v.id === t.current ? null : el("button", { class: "secondary small", type: "button", onclick: (e) => restore(v, e.target) }, "Use this version")),
  )) : [el("tr", {}, el("td", { colspan: 6, class: "empty" },
    "No versions yet. The first time you publish, the timetable in use now is kept as a version too."))]));
}

async function restore(v, button) {
  if (!confirm(`Go back to "${v.original_name}" (${when(v.uploaded_at)})? The receptionist will answer from it.`)) return;
  flash("Switching the timetable: rag-core is rebuilding its answers, this can take a minute or two…");
  await act(button, () => call("/timetable/restore", { method: "POST", body: { version: v.id } }), "Done: that version is in use again.");
  loadTimetable();
}

$("tt-upload").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData($("tt-upload"));
  const r = await act(e.submitter, () => call("/timetable/upload", { method: "POST", form }));
  if (!r) return;
  S.pending = r.pending;
  const c = r.changes;
  $("tt-problems").replaceChildren(
    ...r.problems.map((p) => el("p", { class: "problem" }, "✖ ", p)),
    ...(r.warnings.length ? [el("details", { class: "warnings" }, el("summary", {}, `${r.warnings.length} thing(s) to look at (they don't stop publishing)`),
      el("ul", {}, r.warnings.map((w) => el("li", {}, w))))] : []),
  );
  $("tt-summary").replaceChildren(
    el("span", {}, el("strong", {}, c.faculty_after), " faculty (", c.faculty_before, " now)"),
    el("span", {}, el("strong", {}, c.added.length), " added"),
    el("span", {}, el("strong", {}, c.removed.length), " removed"),
    el("span", {}, el("strong", {}, c.changed.length), " changes to rooms, hours or timetable"),
    el("span", {}, el("strong", {}, c.rooms.length), " classroom changes"),
  );
  const parts = [];
  if (c.added.length) parts.push(el("p", {}, el("strong", {}, "Added: "), c.added.join(", ")));
  if (c.removed.length) parts.push(el("p", {}, el("strong", {}, "Removed: "), c.removed.join(", ")));
  if (c.changed.length) {
    parts.push(el("table", { class: "table changes" },
      el("thead", {}, el("tr", {}, el("th", {}, "Who"), el("th", {}, "What"), el("th", {}, "Now"), el("th", {}, "After publishing"))),
      el("tbody", {}, c.changed.slice(0, 300).map((x) => el("tr", {},
        el("td", {}, x.name), el("td", {}, x.what), el("td", { class: "before" }, x.before || "—"), el("td", { class: "after" }, x.after || "—"))))));
    if (c.changed.length > 300) parts.push(el("p", { class: "hint" }, `…and ${c.changed.length - 300} more.`));
  }
  if (c.rooms.length) {
    parts.push(el("table", { class: "table changes" },
      el("thead", {}, el("tr", {}, el("th", {}, "Room"), el("th", {}, "Now"), el("th", {}, "After publishing"))),
      el("tbody", {}, c.rooms.map((x) => el("tr", {}, el("td", {}, x.room), el("td", { class: "before" }, x.before || "—"), el("td", { class: "after" }, x.after || "—"))))));
  }
  if (!c.added.length && !c.removed.length && !c.changed.length && !c.rooms.length && !r.problems.length) {
    parts.push(el("p", { class: "hint" }, "This file is the same as the timetable in use."));
  }
  $("tt-changes").replaceChildren(...parts);
  $("tt-publish").querySelector(".btn-gold").disabled = r.problems.length > 0;
  $("tt-preview").hidden = false;
  $("tt-preview").scrollIntoView({ behavior: "smooth" });
});

$("tt-publish").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!S.pending) return;
  flash("Publishing: rag-core is rebuilding its answers from the new timetable, this can take a minute or two…");
  const r = await act(e.submitter, () => call("/timetable/publish", { method: "POST", body: { pending: S.pending, note: formData($("tt-publish")).note } }),
    "Published: the receptionist now answers from the new timetable.");
  if (!r) return;
  S.pending = null;
  $("tt-preview").hidden = true;
  $("tt-upload").reset();
  $("tt-publish").reset();
  loadTimetable();
});

$("tt-discard").addEventListener("click", async () => {
  if (S.pending) await call("/timetable/discard", { method: "POST", body: { pending: S.pending } }).catch(() => {});
  S.pending = null;
  $("tt-preview").hidden = true;
  $("tt-upload").reset();
});

// ---------- faculty status ----------
async function loadFaculty() {
  S.faculty = await call("/faculty");
  renderFaculty();
}

function renderFaculty() {
  const q = $("fac-search").value.trim().toLowerCase();
  const rows = S.faculty.filter((f) => !q || `${f.name} ${f.room || ""}`.toLowerCase().includes(q));
  $("fac-search").hidden = S.me.role !== "admin";
  $("fac-table").querySelector("tbody").replaceChildren(...rows.map((f) => {
    const now = f.status ? el("span", { class: "office" }, f.status) : el("span", { class: "quiet" }, f.timetable_now || "Nothing on the timetable now");
    const tr = el("tr", {}, el("td", {}, f.name), el("td", {}, f.room || "—"), el("td", {}, now),
      el("td", { class: "actions" },
        el("button", { class: "secondary small", type: "button", onclick: () => statusForm(f, tr) }, "Set status"),
        f.status ? el("button", { class: "ghost small", type: "button", onclick: (e) => clearStatus(f, e.target) }, "Clear") : null));
    return tr;
  }));
}
$("fac-search").addEventListener("input", renderFaculty);

function statusForm(f, row) {
  document.querySelector(".status-form-row")?.remove();
  const today = localDay();
  const form = el("form", { class: "row-form status-form" },
    el("select", { name: "state" }, Object.entries(STATES).map(([k, v]) => el("option", { value: k, selected: k === f.state }, v))),
    el("label", {}, "until ", el("input", { name: "until_date", type: "date", value: today, min: today, required: true })),
    el("input", { name: "until_time", type: "time", title: "Empty: the end of that day" }),
    el("input", { name: "note", maxlength: 200, placeholder: "Note (optional), e.g. back after lunch" }),
    el("button", { class: "btn btn-navy", type: "submit" }, "Save"),
    el("button", { class: "ghost", type: "button", onclick: () => form.closest("tr").remove() }, "Cancel"));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const d = formData(form);
    const body = { state: d.state, until_date: d.until_date, until_time: d.until_time || null, note: d.note || null };
    if (await act(e.submitter, () => call(`/faculty/${encodeURIComponent(f.id)}/status`, { method: "PUT", body }), "Saved: the kiosk shows it now.")) loadFaculty();
  });
  row.after(el("tr", { class: "status-form-row" }, el("td", { colspan: 4 }, form)));
}

async function clearStatus(f, button) {
  if (await act(button, () => call(`/faculty/${encodeURIComponent(f.id)}/status`, { method: "DELETE" }), "Cleared.")) loadFaculty();
}

// ---------- announcements ----------
async function loadAnnouncements() {
  const list = await call("/announcements");
  const today = localDay();
  $("ann-list").replaceChildren(...(list.length ? list.map((a) => {
    const live = a.starts <= today && (!a.ends || today <= a.ends);
    return el("li", {}, el("p", {}, a.text),
      el("small", {}, live ? el("span", { class: "badge ok" }, "on the kiosk now") : el("span", { class: "badge" }, a.starts > today ? "later" : "ended"),
        ` ${a.starts}${a.ends ? ` to ${a.ends}` : " onwards"} · ${a.created_by}`),
      el("button", { class: "ghost small", type: "button", onclick: async (e) => {
        if (await act(e.target, () => call(`/announcements/${a.id}`, { method: "DELETE" }), "Removed.")) loadAnnouncements();
      } }, "Remove"));
  }) : [el("li", { class: "empty" }, "No announcements.")]));
}

$("ann-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const d = formData($("ann-form"));
  const body = { text: d.text, starts: d.starts || null, ends: d.ends || null };
  if (await act(e.submitter, () => call("/announcements", { method: "POST", body }), "Added.")) { $("ann-form").reset(); loadAnnouncements(); }
});

// ---------- rooms and places ----------
async function loadPlaces() {
  S.places = await call("/places");
  renderPlaces();
  if (S.place) pickPlace(S.places.find((p) => p.id === S.place.id));
}

function renderPlaces() {
  const q = $("pl-search").value.trim().toLowerCase();
  const order = { rooms: 0, cabins: 1, facilities: 2, areas: 3 };
  const rows = S.places.filter((p) => !q || `${p.name} ${p.label}`.toLowerCase().includes(q))
    .sort((a, b) => order[a.group] - order[b.group] || a.name.localeCompare(b.name, undefined, { numeric: true }));
  $("pl-list").replaceChildren(...rows.map((p) => el("li", {}, el("button", {
    type: "button", class: S.place?.id === p.id ? "on" : "", onclick: () => pickPlace(p) },
    el("strong", {}, p.name),
    p.closed ? el("span", { class: "badge bad" }, "closed") : null,
    p.edited ? el("span", { class: "badge" }, "edited") : null))));
}
$("pl-search").addEventListener("input", renderPlaces);

function pickPlace(p) {
  if (!p) return;
  S.place = p;
  renderPlaces();
  $("pl-edit").hidden = false;
  $("pl-title").textContent = p.name;
  const f = $("pl-info");
  for (const k of ["name", "about", "hours", "class_teacher", "notes"]) f.elements[k].value = p.info[k] || "";
  $("pl-closed").elements.reason.value = p.closed?.reason || "";
  $("pl-closed").elements.until.value = p.closed?.until || "";
  $("pl-reopen").hidden = !p.closed;
  const img = $("pl-photo");
  img.hidden = !p.photo;
  if (p.photo) img.src = `/photos/${encodeURIComponent(p.photo)}`;
  $("pl-photo-remove").hidden = !(p.photo || "").startsWith("admin-");
  if (innerWidth < 900) $("pl-edit").scrollIntoView({ behavior: "smooth" });
}

const placePath = (suffix) => `/places/${encodeURIComponent(S.place.id)}/${suffix}`;
$("pl-info").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (await act(e.submitter, () => call(placePath("info"), { method: "PUT", body: formData($("pl-info")) }), "Saved: the kiosk shows it now.")) loadPlaces();
});
$("pl-reset").addEventListener("click", async (e) => {
  if (await act(e.target, () => call(placePath("info"), { method: "DELETE" }), "Back to the original information.")) loadPlaces();
});
$("pl-closed").addEventListener("submit", async (e) => {
  e.preventDefault();
  const d = formData($("pl-closed"));
  if (!d.reason) { flash("Write why it's closed: visitors see the reason.", true); return; }
  if (await act(e.submitter, () => call(placePath("closed"), { method: "PUT", body: { reason: d.reason, until: d.until || null } }), "Marked closed.")) loadPlaces();
});
$("pl-reopen").addEventListener("click", async (e) => {
  if (await act(e.target, () => call(placePath("closed"), { method: "DELETE" }), "Reopened.")) loadPlaces();
});
$("pl-photo-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (await act(e.submitter, () => call(placePath("photo"), { method: "POST", form: new FormData($("pl-photo-form")) }), "Photo uploaded.")) {
    $("pl-photo-form").reset();
    loadPlaces();
  }
});
$("pl-photo-remove").addEventListener("click", async (e) => {
  if (await act(e.target, () => call(placePath("photo"), { method: "DELETE" }), "Removed.")) loadPlaces();
});

// ---------- accounts ----------
async function loadAccounts() {
  if (S.me.role !== "admin") return;
  const [users, faculty] = await Promise.all([call("/users"), call("/faculty")]);
  $("acc-table").querySelector("tbody").replaceChildren(...users.map((u) => el("tr", {},
    el("td", {}, u.username), el("td", {}, u.name),
    el("td", {}, u.role === "admin" ? "Department office" : `Faculty (${u.faculty_name || u.faculty_id})`),
    el("td", { class: "actions" },
      el("button", { class: "secondary small", type: "button", onclick: (e) => resetPassword(u, e.target) }, "New password"),
      u.username === S.me.username ? null : el("button", { class: "ghost small", type: "button", onclick: (e) => removeUser(u, e.target) }, "Remove")))));
  const sel = $("acc-form").elements.faculty_id;
  sel.replaceChildren(...faculty.map((f) => el("option", { value: f.id }, `${f.name}${f.room ? ` (${f.room})` : ""}`)));
}

$("acc-form").elements.role.addEventListener("change", (e) => { $("acc-fac-wrap").hidden = e.target.value !== "faculty"; });
$("acc-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const d = formData($("acc-form"));
  if (d.role !== "faculty") delete d.faculty_id;
  if (await act(e.submitter, () => call("/users", { method: "POST", body: d }), (r) => `Added ${r.username}.`)) {
    $("acc-form").reset();
    $("acc-fac-wrap").hidden = true;
    loadAccounts();
  }
});

async function resetPassword(u, button) {
  const password = prompt(`New password for ${u.username} (8 or more characters):`);
  if (!password) return;
  await act(button, () => call(`/users/${encodeURIComponent(u.username)}/password`, { method: "POST", body: { password } }), "Password changed.");
}

async function removeUser(u, button) {
  if (!confirm(`Remove the account ${u.username}? They won't be able to sign in.`)) return;
  if (await act(button, () => call(`/users/${encodeURIComponent(u.username)}`, { method: "DELETE" }), "Removed.")) loadAccounts();
}

$("pw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (await act(e.submitter, () => call("/me/password", { method: "POST", body: formData($("pw-form")) }), "Password changed: please sign in again.")) {
    S.me = null;
    gate();
  }
});

// ---------- change log ----------
async function loadLog() {
  const rows = await call("/audit");
  $("log-table").querySelector("tbody").replaceChildren(...(rows.length ? rows.map((r) => el("tr", {},
    el("td", {}, when(r.timestamp)), el("td", {}, r.user), el("td", {}, r.action), el("td", {}, r.detail || "")))
    : [el("tr", {}, el("td", { colspan: 4, class: "empty" }, "No changes yet."))]));
}

// ---------- start ----------
call("/me").then(signedIn).catch(() => gate());
