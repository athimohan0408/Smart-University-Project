// Analytics dashboard: reads GET /api/analytics and draws tiles, bars, the hourly chart and tables.
"use strict";

const $ = (id) => document.getElementById(id);
const REASONS = {
  not_in_kb: "Not in the data", out_of_scope: "Outside what it covers", user_requested: "Visitor asked for a person",
  grounding_failed: "Answer failed checking", ambiguous_entity: "Unclear who was meant", low_confidence: "Low confidence",
  llm_error: "Language model error", rag_unavailable: "Service unreachable", kiosk_upstream_error: "Service unreachable",
};
const INTENTS = {
  find_person: "Find a person", person_availability: "Availability", find_by_subject: "Who teaches",
  find_room: "Find a room", schedule_query: "Timetable", programme_info: "Programme", event_info: "Events",
  facility_info: "Facilities", contact_info: "Contact details", escalate_to_human: "Asked for a person",
  out_of_scope: "Out of scope", unknown: "Unknown",
};
let mapView = null;

const fmt = (n) => (n ?? 0).toLocaleString();
const pct = (x) => (x == null ? "–" : `${(x * 100).toFixed(1)}%`);
const ms = (x) => (x == null ? "–" : x >= 1000 ? `${(x / 1000).toFixed(2)} s` : `${Math.round(x)} ms`);

function tip(e, text) {
  const t = $("tip");
  if (!text) { t.hidden = true; return; }
  t.textContent = text;
  t.hidden = false;
  t.style.left = `${Math.min(e.clientX + 12, innerWidth - t.offsetWidth - 8)}px`;
  t.style.top = `${e.clientY + 14}px`;
}

function tiles(d) {
  const t = d.totals;
  const items = [
    ["Questions", fmt(t.questions), `${fmt(t.sessions)} visitor sessions`],
    ["Answered", fmt(t.answered), t.questions ? `${pct(t.answered / t.questions)} of questions` : ""],
    ["Handed to receptionist", fmt(t.escalated), t.questions ? `${pct(t.escalated / t.questions)} of questions` : ""],
    ["Query completion", pct(t.completion_rate), "voice-memory's definition"],
    ["Avg response", ms(d.response_time_ms.kiosk_round_trip?.mean ?? d.response_time_ms.voice_turn_total?.mean
      ?? d.response_time_ms.rag_server_total?.mean), "what the visitor waited"],
    ["Routes shown", fmt(t.routes_shown), `${fmt(t.receptionist_button)} receptionist button presses`],
  ];
  $("tiles").replaceChildren(...items.map(([k, v, s]) => {
    const div = document.createElement("div");
    div.className = "tile";
    div.innerHTML = `<div class="k"></div><div class="v"></div><div class="s"></div>`;
    div.children[0].textContent = k; div.children[1].textContent = v; div.children[2].textContent = s;
    return div;
  }));
}

function bars(container, entries, label = (k) => k) {
  const box = $(container);
  if (!entries.length) { box.innerHTML = `<p class="empty">No data yet.</p>`; return; }
  const max = Math.max(...entries.map(([, v]) => v));
  const total = entries.reduce((s, [, v]) => s + v, 0);
  box.replaceChildren(...entries.map(([k, v]) => {
    const row = document.createElement("div");
    row.className = "bar-row";
    row.innerHTML = `<span class="name"></span><div class="track"><div class="fill"></div></div><span class="val"></span>`;
    row.children[0].textContent = label(k);
    row.children[0].title = label(k);
    row.querySelector(".fill").style.width = `${(v / max) * 100}%`;
    row.children[2].textContent = fmt(v);
    row.addEventListener("mousemove", (e) => tip(e, `${label(k)}: ${fmt(v)} (${pct(v / total)})`));
    row.addEventListener("mouseleave", (e) => tip(e, null));
    return row;
  }));
}

function hourChart(counts) {
  const W = 720, H = 200, L = 30, B = 22, T = 10;
  const max = Math.max(1, ...counts);
  const step = (W - L) / 24, bw = step - 2;
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Questions by hour of day");
  const add = (name, attrs, text) => {
    const n = document.createElementNS(ns, name);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text != null) n.textContent = text;
    svg.append(n);
    return n;
  };
  const y = (v) => H - B - (v / max) * (H - B - T);
  for (const v of [0, Math.ceil(max / 2), max]) {
    add("line", { x1: L, x2: W, y1: y(v), y2: y(v), class: "grid-line" });
    add("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end", class: "axis" }, v);
  }
  counts.forEach((c, h) => {
    const x = L + h * step + 1;
    const hit = add("rect", { x: x - 1, y: T, width: step, height: H - B - T, class: "hit" });
    if (c) {
      const top = y(c), r = Math.min(4, bw / 2, H - B - top);
      add("path", { class: "col", d: `M${x},${H - B} V${top + r} Q${x},${top} ${x + r},${top} H${x + bw - r} Q${x + bw},${top} ${x + bw},${top + r} V${H - B} Z` });
    } else add("g", {});
    const label = `${String(h).padStart(2, "0")}:00–${String(h).padStart(2, "0")}:59: ${fmt(c)} question${c === 1 ? "" : "s"}`;
    hit.addEventListener("mousemove", (e) => tip(e, label));
    hit.addEventListener("mouseleave", (e) => tip(e, null));
    if (h % 3 === 0) add("text", { x: x + bw / 2, y: H - 6, "text-anchor": "middle", class: "axis" }, `${h}h`);
  });
  $("by-hour").replaceChildren(svg);
}

function rows(tbody, items, cells) {
  const body = $(tbody);
  if (!items.length) {
    const cols = body.closest("table").querySelectorAll("th").length;
    body.innerHTML = `<tr><td colspan="${cols}" class="empty">No data yet.</td></tr>`;
    return;
  }
  body.replaceChildren(...items.map((it) => {
    const tr = document.createElement("tr");
    for (const [value, cls] of cells(it)) {
      const td = document.createElement("td");
      td.textContent = value;
      if (cls) td.className = cls;
      tr.append(td);
    }
    return tr;
  }));
}

function destLabel(id) {
  const [kind, rest] = id.split(":", 2);
  if (kind === "room") return `Room ${rest.toUpperCase()}`;
  return rest.split("-").map((w) => (w.length > 1 ? w[0].toUpperCase() + w.slice(1) : w.toUpperCase())).join(" ");
}

async function load() {
  const days = $("range").value;
  const d = await (await fetch(`/api/analytics${days ? `?days=${days}` : ""}`)).json();
  $("meta").textContent = `Updated ${new Date(d.generated_at).toLocaleString()} · from ${fmt(d.sources.rag_queries)} knowledge-base queries, `
    + `${fmt(d.sources.voice_turns)} conversation turns and ${fmt(d.sources.ui_events)} kiosk events.`;
  tiles(d);
  hourChart(d.by_hour);
  bars("categories", Object.entries(d.categories), (k) => INTENTS[k] || k);
  bars("reasons", Object.entries(d.escalation_reasons), (k) => REASONS[k] || k);
  const destNames = Object.fromEntries(d.top_destinations.map((x) => [x.id, x.name || destLabel(x.id)]));
  bars("destinations", d.top_destinations.map((x) => [x.id, x.count]), (id) => destNames[id]);
  rows("most-asked", d.most_asked, (q) => [[q.question], [fmt(q.count), "num"], [fmt(q.answered), "num"]]);
  rows("unanswered", d.unanswered, (q) => [[q.question], [fmt(q.count), "num"],
    [Object.keys(q.reasons).map((r) => REASONS[r] || r).join(", ")]]);
  const names = { kiosk_round_trip: "Kiosk (what the visitor waited)", voice_turn_total: "Voice + memory turn",
    rag_server_total: "Knowledge base (server)" };
  rows("timing", Object.entries(d.response_time_ms), ([k, t]) => [[names[k] || k], [ms(t.mean), "num"],
    [ms(t.p50), "num"], [ms(t.p95), "num"], [fmt(t.n), "num"]]);
  const st = d.rag_stage_mean_ms;
  $("stages").textContent = Object.keys(st).length
    ? `Knowledge-base stages (mean): understanding ${ms(st.nlu)}, search ${ms(st.retrieval)}, answer ${ms(st.generation)}, checking ${ms(st.scoring)}.`
    : "";
  if (mapView) mapView.highlight(d.top_destinations.map((x) => (x.id.startsWith("room:") ? x.id : x.room)).filter(Boolean));
}

async function start() {
  try {
    mapView = FloorMapView.render($("dest-map"), await (await fetch("/api/map")).json());
  } catch { /* map is optional here */ }
  $("range").addEventListener("change", load);
  $("refresh").addEventListener("click", load);
  await load();
  setInterval(load, 60000);
}

start().catch((err) => { $("meta").textContent = `Could not load analytics: ${err.message}`; });
