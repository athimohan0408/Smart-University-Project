// Kiosk: place cards (tap any place on the map) and "Explore the floor" (every room, cabin, lift and washroom,
// searchable, with the nearest washroom / lift / water one tap away). Uses kiosk.js's state, api, touch,
// showDetails and openNav. Data: GET /api/places, /api/place/{id}, /api/route/{id}, /api/nearest/{what}.
"use strict";

const explore = { places: null, card: null };
const GROUP_TITLES = {
  rooms: "Classrooms, the lab and offices", cabins: "Faculty cabins · Faculty Staff Room C816",
  facilities: "Lifts, stairs, washrooms and more", areas: "Areas",
};
const GROUP_KICKERS = { rooms: "Room", cabins: "Faculty cabin", facilities: "On this floor", areas: "Area" };

const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : "");

async function loadPlaces() {
  if (!explore.places) explore.places = await api("/api/places");
  return explore.places;
}

// ---------- a place card ----------
async function openPlace(id) {
  touch();
  let card;
  try { card = await api(`/api/place/${encodeURIComponent(id)}`); } catch { return; }
  explore.card = card;
  state.mapView?.highlight([card.id]);
  $("place-kicker").textContent = GROUP_KICKERS[card.group] || "";
  $("place-name").textContent = card.name;
  $("place-where").textContent = card.where ? `${cap(card.where).replace(" · ", ", ")}, 8th floor` : "";
  $("place-closed").textContent = card.closed || "";
  $("place-closed").hidden = !card.closed;
  $("place-about").textContent = card.about || "";
  $("place-about").hidden = !card.about;

  const facts = $("place-facts");
  facts.replaceChildren();
  const add = (k, v) => {
    if (!v) return;
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    facts.append(dt, dd);
  };
  add("Class teacher", card.class_teacher);
  add("Hours", card.hours);
  add("Note", card.notes);
  facts.hidden = !facts.children.length;

  const staff = $("place-staff");
  staff.replaceChildren();
  for (const p of card.staff) {
    const li = document.createElement("li");
    const name = document.createElement("strong");
    name.textContent = p.name;
    li.append(name);
    const now = document.createElement("span");
    now.className = p.from_office ? "now office" : p.now ? "now" : "now quiet";
    now.textContent = p.now || "No class or consultation on the timetable right now";
    li.append(now);
    if (p.next_consultation) {
      const next = document.createElement("span");
      next.className = "next";
      next.textContent = `Next consultation hours: ${p.next_consultation}`;
      li.append(next);
    }
    staff.append(li);
  }
  if (card.group === "cabins" && !card.staff.length) {
    const li = document.createElement("li");
    li.className = "quiet";
    li.textContent = "The faculty sheet lists no one in this cabin. The receptionist can help.";
    staff.append(li);
  }
  staff.hidden = !staff.children.length;
  $("place-source").textContent = card.staff.length
    ? (card.staff.some((p) => p.from_office) ? "Status set by the department office. " : "")
      + "From the department's faculty sheet and timetable. The timetable says where they should be, not where they are."
    : card.source ? `Source: ${card.source}` : "";

  const photo = $("place-photo");
  if (card.photo) photo.src = `/photos/${encodeURIComponent(card.photo)}`; else photo.removeAttribute("src");
  photo.hidden = !card.photo;
  photo.alt = card.photo ? `Photo: ${card.name}` : "";

  $("place-go").hidden = !card.routable;
  const base = state.config?.public_url;
  $("place-qr").hidden = true;
  if (base && card.routable) {
    QR.render($("place-qr-code"), `${base}/go?from=kiosk&to=${encodeURIComponent(card.id)}`, { cell: 4 })
      .then((ok) => { $("place-qr").hidden = !ok; });
  }
  $("place-overlay").hidden = false;
}

function closePlace() {
  $("place-overlay").hidden = true;
  explore.card = null;
}

// Show a route on the board and walk it, full screen.
function goTo(route) {
  closePlace();
  closeExploreList();
  showDetails({}, [route], []);
  openNav(route);
}

async function wayToCard() {
  if (!explore.card) return;
  try { goTo(await api(`/api/route/${encodeURIComponent(explore.card.id)}`)); } catch { /* not routable */ }
}

async function nearest(what) {
  touch();
  try { goTo(await api(`/api/nearest/${encodeURIComponent(what)}`)); } catch { /* nothing on this floor */ }
}

// ---------- explore the floor ----------
async function openExplore() {
  touch();
  if (typeof onIntro === "function" && onIntro()) enterChat();
  $("explore-search").value = "";
  $("explore-overlay").hidden = false;
  try { renderList(await loadPlaces(), ""); } catch { $("explore-list").textContent = "The list isn't available right now."; }
}

function closeExploreList() {
  $("explore-overlay").hidden = true;
}

function closeExplore() {
  closePlace();
  closeExploreList();
  $("codes-overlay").hidden = true;
}

function matches(p, q) {
  if (!q) return true;
  const hay = `${p.name} ${p.label} ${p.people.join(" ")}`.toLowerCase();
  return q.toLowerCase().split(/\s+/).every((w) => hay.includes(w));
}

function renderList(places, q) {
  const list = $("explore-list");
  list.replaceChildren();
  for (const [group, title] of Object.entries(GROUP_TITLES)) {
    const items = places.filter((p) => p.group === group && matches(p, q) && (group !== "facilities" || p.routable || q));
    if (!items.length) continue;
    const h = document.createElement("h3");
    h.textContent = title;
    const ul = document.createElement("ul");
    for (const p of items) {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      const name = document.createElement("strong");
      name.textContent = p.name;
      b.append(name);
      if (p.people.length) {
        const who = document.createElement("span");
        who.textContent = p.people.join(", ");
        b.append(who);
      }
      b.addEventListener("click", () => openPlace(p.id));
      li.append(b);
      ul.append(li);
    }
    list.append(h, ul);
  }
  if (!list.children.length) {
    const p = document.createElement("p");
    p.className = "quiet";
    p.textContent = "Nothing on this floor matches. Try a room number or a name, or ask me in the chat.";
    list.append(p);
  }
}

// ---------- QR codes on the main screen ----------
// On the map board: with a route, a code that opens these directions on the visitor's phone; without one, a
// code that opens the phone page from the kiosk. Hidden when phones can't reach this server (no public_url).
let phoneQrFor = undefined;
function updatePhoneQr(route) {
  const base = state.config?.public_url;
  const key = route ? route.place_id : "";
  if (!base) { $("phone-qr").hidden = true; return; }
  if (key === phoneQrFor) return;
  phoneQrFor = key;
  const url = route ? `${base}/go?from=kiosk&to=${encodeURIComponent(route.place_id)}` : `${base}/go?from=kiosk`;
  $("phone-qr-title").textContent = route ? "Take these directions with you" : "Directions on your phone";
  $("phone-qr-text").textContent = route
    ? `Scan to follow the way to ${destinationName(route)} on your phone, step by step in 3D.`
    : "Scan to find any room, cabin or washroom on this floor from your phone.";
  QR.render($("phone-qr-code"), url, { cell: 4 }).then((ok) => { $("phone-qr").hidden = !ok; });
}

function openCodes() {
  touch();
  const base = state.config?.public_url;
  const grid = $("codes-grid");
  grid.replaceChildren();
  for (const s of state.map?.starts || []) {
    const card = document.createElement("figure");
    card.className = "code-card";
    const box = document.createElement("div");
    box.className = "qr-code";
    const cap = document.createElement("figcaption");
    cap.textContent = s.id === "kiosk" ? "At this kiosk" : `At ${s.name}`;
    card.append(box, cap);
    grid.append(card);
    QR.render(box, `${base}/go?from=${encodeURIComponent(s.id)}`, { cell: 5 });
  }
  $("codes-overlay").hidden = false;
}

$("codes-open").addEventListener("click", openCodes);
$("codes-close").addEventListener("click", () => { $("codes-overlay").hidden = true; });
$("codes-overlay").addEventListener("click", (e) => { if (e.target === $("codes-overlay")) $("codes-overlay").hidden = true; });
(function waitForConfig(tries = 0) {  // the general code, once kiosk.js has the config
  if (state.config && state.map) updatePhoneQr(state.route || null);
  else if (tries < 100) setTimeout(() => waitForConfig(tries + 1), 100);
})();
window.updatePhoneQr = updatePhoneQr;

$("explore-open").addEventListener("click", openExplore);
$("explore-btn").addEventListener("click", openExplore);
$("explore-close").addEventListener("click", closeExploreList);
$("explore-search").addEventListener("input", async (e) => { touch(); renderList(await loadPlaces(), e.target.value.trim()); });
for (const b of document.querySelectorAll("[data-nearest]")) b.addEventListener("click", () => nearest(b.dataset.nearest));
$("place-close").addEventListener("click", closePlace);
$("place-go").addEventListener("click", wayToCard);
$("place-overlay").addEventListener("click", (e) => { if (e.target === $("place-overlay")) closePlace(); });
$("explore-overlay").addEventListener("click", (e) => { if (e.target === $("explore-overlay")) closeExploreList(); });
window.openPlace = openPlace;
window.closeExplore = closeExplore;
