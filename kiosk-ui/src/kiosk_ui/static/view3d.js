// Kiosk: the "3D view" / "2D map" switch on the map board. The 3D view (floor3d.js, Three.js) loads only when
// switched on; with no WebGL or no network for Three.js the button says so and the 2D map stays. The choice is
// remembered on this kiosk. kiosk.js calls window.map3d.showRoute / clear when an answer has a route.
"use strict";

const view3d = { ctl: null, module: null };

async function set3d(on) {
  const btn = $("view3d");
  if (on) {
    try {
      view3d.module ||= await import("/static/floor3d.js");
      if (!view3d.module.supported()) throw new Error("no WebGL");
    } catch {
      btn.textContent = "3D unavailable";
      btn.disabled = true;
      btn.title = "This screen can't show the 3D view (no WebGL, or Three.js couldn't load).";
      return;
    }
    $("map").hidden = true;
    $("map3d").hidden = false;
    view3d.ctl ||= view3d.module.create($("map3d"), state.map, { onPlace: (p) => window.openPlace?.(p.id) });
    window.map3d = view3d.ctl;
    if (state.route) view3d.ctl.showRoute(state.route); else view3d.ctl.resetView();
  } else {
    $("map").hidden = false;
    $("map3d").hidden = true;
    window.map3d = null;
  }
  btn.setAttribute("aria-pressed", String(on));
  btn.textContent = on ? "2D map" : "3D view";
  try { localStorage.setItem("kiosk-3d", on ? "1" : "0"); } catch { /* private mode */ }
}

$("view3d").addEventListener("click", () => { touch(); set3d($("view3d").getAttribute("aria-pressed") !== "true"); });

// 3D by default once the map has loaded (kiosk.js fills state.map), unless this kiosk was switched to 2D
(function waitForMap(tries = 0) {
  if (!state.map) { if (tries < 100) setTimeout(() => waitForMap(tries + 1), 100); return; }
  let saved = null;
  try { saved = localStorage.getItem("kiosk-3d"); } catch { /* private mode */ }
  if (saved !== "0") set3d(true);
})();
