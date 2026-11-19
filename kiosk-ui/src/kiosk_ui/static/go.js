// The phone walkthrough: /go?from=lift-north&to=room:874. Opened from the QR stickers at the lifts and stairs, and
// from a place card's "Scan to take the directions on your phone" on the kiosk. Same routes as the kiosk.
"use strict";

const $ = (id) => document.getElementById(id);
const ARROWS = {  // 24x24 icons, as on the kiosk's walkthrough
  start: "M12 3l6 7h-4v11h-4V10H6z",
  straight: "M12 3l6 7h-4v11h-4V10H6z",
  left: "M9 5L3 11l6 6v-4h6a2 2 0 0 1 2 2v6h4v-6a6 6 0 0 0-6-6H9z",
  right: "M15 5l6 6-6 6v-4H9a2 2 0 0 0-2 2v6H3v-6a6 6 0 0 1 6-6h6z",
  around: "M6 21v-9a6 6 0 0 1 12 0v3h3l-5 6-5-6h3v-3a2 2 0 0 0-4 0v9z",
  enter: "M4 3h10v4h-2V5H6v14h6v-2h2v4H4zM15 8l5 4-5 4v-3H9v-2h6z",
  arrive: "M12 2a7 7 0 0 0-7 7c0 5.2 7 13 7 13s7-7.8 7-13a7 7 0 0 0-7-7zm0 9.5A2.5 2.5 0 1 1 12 6.5a2.5 2.5 0 0 1 0 5z",
};
const GROUPS = { rooms: "Classrooms, the lab and offices", cabins: "Faculty cabins", facilities: "Lifts, stairs, washrooms" };
const go = { from: "kiosk", map: null, overview: null, view: null, places: [], route: null, i: 0,
  three: null, view3d: null, use3d: true };
try { go.use3d = localStorage.getItem("go-3d") !== "0"; } catch { /* private mode */ }

async function getJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

function setUrl() {
  const q = new URLSearchParams({ from: go.from });
  if (go.route) q.set("to", go.route.place_id);
  history.replaceState(null, "", `/go?${q}`);
}

async function setFrom(from) {
  try { go.map = await getJson(`/api/map?from=${encodeURIComponent(from)}`); go.from = from; }
  catch { go.map = await getJson("/api/map"); go.from = "kiosk"; }
  $("from").value = go.from;
  go.overview = FloorMapView.render($("overview"), go.map, { allPlaces: true, onPlace: (p) => p.door && open(p.id) });
  go.view = null;
  go.view3d?.dispose();  // the "You are here" pin moves with the start
  go.view3d = null;
  setUrl();
}

// The 3D walkthrough (floor3d.js, Three.js), loaded on first use. False when this phone can't show it (no WebGL,
// or no network for Three.js): the 2D map is used instead.
async function ready3d() {
  if (!go.use3d) return false;
  try {
    go.three ||= await import("/static/floor3d.js");
    if (!go.three.supported()) throw new Error("no WebGL");
  } catch {
    go.use3d = false;
    $("dim").hidden = true;
    return false;
  }
  go.view3d ||= go.three.create($("map3d"), go.map);
  return true;
}

function showDim() {
  $("map").hidden = go.use3d;
  $("map3d").hidden = !go.use3d;
  $("dim").textContent = go.use3d ? "2D map" : "3D view";
  $("dim").setAttribute("aria-pressed", String(!go.use3d));
}

async function toggleDim() {
  go.use3d = !go.use3d;
  try { localStorage.setItem("go-3d", go.use3d ? "1" : "0"); } catch { /* private mode */ }
  if (go.use3d && !(await ready3d())) go.use3d = false;
  showDim();
  if (go.route) step(go.i);
}

// ---------- 1. where to ----------
function matches(p, q) {
  const hay = `${p.name} ${p.label} ${p.people.join(" ")}`.toLowerCase();
  return !q || q.toLowerCase().split(/\s+/).every((w) => hay.includes(w));
}

function renderList(q = "") {
  const list = $("list");
  list.replaceChildren();
  for (const [group, title] of Object.entries(GROUPS)) {
    const items = go.places.filter((p) => p.group === group && p.routable && matches(p, q)
      && p.id !== `facility:${go.from}`);
    if (!items.length) continue;
    const details = document.createElement("details");
    details.open = !!q || group === "rooms";
    const summary = document.createElement("summary");
    summary.textContent = `${title} (${items.length})`;
    details.append(summary);
    const ul = document.createElement("ul");
    for (const p of items) {
      const b = document.createElement("button");
      b.type = "button";
      b.innerHTML = "<strong></strong><span></span>";
      b.querySelector("strong").textContent = p.name;
      b.querySelector("span").textContent = p.people.join(", ");
      b.addEventListener("click", () => open(p.id));
      const li = document.createElement("li");
      li.append(b);
      ul.append(li);
    }
    details.append(ul);
    list.append(details);
  }
  if (!list.children.length) list.textContent = "Nothing on this floor matches. Try a room number or a name.";
}

async function open(placeId) {
  try { show(await getJson(`/api/route/${encodeURIComponent(placeId)}?from=${encodeURIComponent(go.from)}`)); }
  catch { /* not routable */ }
}

async function nearest(what) {
  try { show(await getJson(`/api/nearest/${what}?from=${encodeURIComponent(go.from)}`)); } catch { /* none */ }
}

// ---------- 2. step by step ----------
async function show(route) {
  go.route = route;
  setUrl();
  $("pick").hidden = true;
  $("walk").hidden = false;
  if (!go.view) go.view = FloorMapView.render($("map"), go.map);
  go.view.navReset();
  await ready3d();
  showDim();
  go.view3d?.clear();
  step(0);
  scrollTo({ top: 0 });
}

function destination(r) {
  const name = r.destination || `Room ${r.label}`;
  return name.charAt(0).toLowerCase() + name.slice(1);
}

function step(i) {
  const r = go.route, last = r.steps.length - 1, arrived = i === last;
  go.i = i;
  speechSynthesis?.cancel();
  const kind = r.step_kinds[i] || "straight";
  $("arrow").setAttribute("d", ARROWS[kind.startsWith("arrive") ? "arrive" : kind] || ARROWS.straight);
  $("walk").classList.toggle("arrived", arrived);
  $("dist").textContent = arrived ? "You have arrived" : `${Math.max(1, Math.round(r.step_metres[i]))} m`;
  $("instr").textContent = r.steps[i];
  $("then").textContent = arrived ? "" : `Then: ${r.steps[i + 1]}`;
  const photo = r.step_photos[i];
  $("photo-box").hidden = !photo;
  if (photo) {
    $("photo").src = `/photos/${encodeURIComponent(photo)}`;
    $("photo-cap").textContent = arrived
      ? (r.place_id.startsWith("room:") ? `Look for this door: room ${r.label}` : `Look for this: ${destination(r)}`)
      : i === 0 && r.start_id !== "kiosk" ? `Where you start: ${r.start_name}` : "Look for this";
  }
  $("progress").textContent = `Step ${i + 1} of ${r.steps.length} · to ${destination(r)}, about ${Math.round(r.length_m)} m`;
  $("prev").disabled = i === 0;
  $("next").disabled = arrived;
  const ms = Math.min(5000, Math.max(1400, r.step_metres[i] * 280));
  if (go.use3d && go.view3d) { go.view3d.step(r, i, ms * 1.3); return; }
  const pts = go.view.navStep(r, i);
  go.view.focus(arrived ? r.points.slice(-3) : pts);
  go.view.walk(pts, ms);
}

function sayStep() {
  if (!window.speechSynthesis || !go.route) return;
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(go.route.steps[go.i]);
  u.rate = 0.95;
  speechSynthesis.speak(u);
}

function backToPick() {
  speechSynthesis?.cancel();
  go.route = null;
  setUrl();
  $("walk").hidden = true;
  $("pick").hidden = false;
}

// ---------- start ----------
async function start() {
  const params = new URLSearchParams(location.search);
  const map = await getJson("/api/map");
  for (const s of map.starts) {
    const o = document.createElement("option");
    o.value = s.id;
    o.textContent = s.id === "kiosk" ? "the AI receptionist kiosk" : s.name;
    $("from").append(o);
  }
  await setFrom((params.get("from") || "kiosk").toLowerCase());
  go.places = await getJson("/api/places");
  renderList();
  $("from").addEventListener("change", async (e) => { await setFrom(e.target.value); renderList($("search").value.trim()); });
  $("search").addEventListener("input", (e) => renderList(e.target.value.trim()));
  for (const b of document.querySelectorAll("[data-nearest]")) b.addEventListener("click", () => nearest(b.dataset.nearest));
  $("prev").addEventListener("click", () => step(Math.max(0, go.i - 1)));
  $("next").addEventListener("click", () => step(Math.min(go.route.steps.length - 1, go.i + 1)));
  $("say").addEventListener("click", sayStep);
  $("again").addEventListener("click", backToPick);
  $("dim").addEventListener("click", toggleDim);
  if (params.get("to")) await open(params.get("to"));
}

start().catch(() => { $("list").textContent = "Directions aren't available right now. Please ask at the kiosk."; });
