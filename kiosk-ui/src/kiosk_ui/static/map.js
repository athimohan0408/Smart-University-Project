// Floor map drawing, shared by the kiosk and the dashboard. Data comes from GET /api/map.
"use strict";

const FloorMapView = (() => {
  const NS = "http://www.w3.org/2000/svg";

  function el(name, attrs = {}, text) {
    const node = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // Draw the map into `container`; returns {showRoute(route), clear()}.
  function render(container, map, { onPlace } = {}) {
    const [vx, vy, vw, vh] = map.viewbox;
    const svg = el("svg", { viewBox: `${vx} ${vy} ${vw} ${vh}`, "aria-hidden": "true" });
    for (const [x, y, w, h] of map.corridor) svg.append(el("rect", { x, y, width: w, height: h, rx: 6, class: "corridor" }));

    // kinds: room (destination), area (e.g. the staff room around the cabins), hall, stairs, facility, reception
    const rects = new Map();
    for (const p of map.places) {
      const [x, y, w, h] = p.rect;
      const cls = p.kind === "room" ? "room" : p.kind === "reception" ? "reception" : `area kind-${p.kind}`;
      const r = el("rect", { x, y, width: w, height: h, rx: p.kind === "room" ? 3 : 2, class: cls });
      r.append(el("title", {}, p.kind === "room" ? `Room ${p.label}` : p.label));
      svg.append(r);
      if (onPlace && p.photo) {
        r.classList.add("has-photo");
        r.addEventListener("click", () => onPlace(p));
      }
      const [lx, ly] = p.label_at || [x + w / 2, y + h / 2];
      const labelCls = p.kind === "room" ? (w < 90 ? "label cabin" : "label") : `label small kind-${p.kind}`;
      svg.append(el("text", { x: lx, y: ly, class: labelCls }, p.label));
      rects.set(p.id.toLowerCase(), r);
    }

    const routeLayer = el("g");
    svg.append(routeLayer);
    const [sx, sy] = map.start_point;
    svg.append(el("circle", { cx: sx, cy: sy, r: 14, class: "you" }));
    svg.append(el("text", { x: sx + 22, y: sy + 1, class: "label small here-label" }, "You are here"));
    container.replaceChildren(svg);

    function clear() {
      routeLayer.replaceChildren();
      for (const r of rects.values()) r.classList.remove("hi");
    }

    function showRoute(route) {
      clear();
      if (!route) return;
      const pts = route.points.map(([x, y]) => `${x},${y}`).join(" ");
      routeLayer.append(el("polyline", { points: pts, class: "route" }));
      const [dx, dy] = route.points[route.points.length - 1];
      routeLayer.append(el("circle", { cx: dx, cy: dy, r: 12, class: "dest" }));
      rects.get(route.place_id.toLowerCase())?.classList.add("hi");
    }

    function highlight(ids) {
      clear();
      for (const id of ids) rects.get(id.toLowerCase())?.classList.add("hi");
    }

    // ---- walkthrough ("like Google Maps navigation"): one step at a time ----
    const fmt = (pts) => pts.map(([x, y]) => `${x},${y}`).join(" ");
    const navLayer = el("g");
    svg.append(navLayer);
    let walker = null, walkRaf = 0, zoomRaf = 0;
    let view = [vx, vy, vw, vh];

    // The whole route faint, what's been walked solid, the current step bright, the walker at the step's start.
    function navStep(route, i) {
      clear();
      cancelAnimationFrame(walkRaf);
      navLayer.replaceChildren();
      const [a, b] = route.step_spans[i];
      const pts = route.points;
      navLayer.append(el("polyline", { points: fmt(pts), class: "route-ghost" }));
      if (a > 0) navLayer.append(el("polyline", { points: fmt(pts.slice(0, a + 1)), class: "route-done" }));
      navLayer.append(el("polyline", { points: fmt(pts.slice(a, b + 1)), class: "route-now" }));
      const [dx, dy] = pts[pts.length - 1];
      navLayer.append(el("circle", { cx: dx, cy: dy, r: 12, class: "dest" }));
      rects.get(route.place_id.toLowerCase())?.classList.add("hi");
      walker = el("circle", { cx: pts[a][0], cy: pts[a][1], r: 13, class: "walker" });
      navLayer.append(walker);
      return pts.slice(a, b + 1);
    }

    // Move the walker along `pts` in `ms` milliseconds; resolves when it arrives.
    function walk(pts, ms) {
      cancelAnimationFrame(walkRaf);
      const segs = pts.slice(1).map((p, k) => Math.hypot(p[0] - pts[k][0], p[1] - pts[k][1]));
      const total = segs.reduce((s, d) => s + d, 0) || 1;
      return new Promise((resolve) => {
        const t0 = performance.now();
        const frame = (now) => {
          const t = Math.min(1, (now - t0) / ms);
          let d = t * total, k = 0;
          while (k < segs.length - 1 && d > segs[k]) { d -= segs[k]; k++; }
          const f = segs[k] ? Math.min(1, d / segs[k]) : 1;
          walker?.setAttribute("cx", pts[k][0] + (pts[k + 1]?.[0] - pts[k][0] || 0) * f);
          walker?.setAttribute("cy", pts[k][1] + (pts[k + 1]?.[1] - pts[k][1] || 0) * f);
          if (t < 1) walkRaf = requestAnimationFrame(frame); else resolve();
        };
        walkRaf = requestAnimationFrame(frame);
      });
    }

    // Zoom to show `pts` (with room around them), like a navigation app following you; null = whole floor.
    function focus(pts, ms = 600) {
      cancelAnimationFrame(zoomRaf);
      let target = [vx, vy, vw, vh];
      if (pts && pts.length) {
        const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
        const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
        const w = Math.max(Math.max(...xs) - Math.min(...xs) + 260, vw * 0.36);
        const h = Math.max(Math.max(...ys) - Math.min(...ys) + 220, vh * 0.36);
        target = [cx - w / 2, cy - h / 2, w, h];
      }
      const from = view.slice(), t0 = performance.now();
      const frame = (now) => {
        const t = Math.min(1, (now - t0) / ms), e = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
        view = from.map((v, k) => v + (target[k] - v) * e);
        svg.setAttribute("viewBox", view.join(" "));
        if (t < 1) zoomRaf = requestAnimationFrame(frame);
      };
      zoomRaf = requestAnimationFrame(frame);
    }

    function navReset() {
      cancelAnimationFrame(walkRaf);
      cancelAnimationFrame(zoomRaf);
      navLayer.replaceChildren();
      view = [vx, vy, vw, vh];
      svg.setAttribute("viewBox", view.join(" "));
      clear();
    }

    return { showRoute, clear, highlight, navStep, walk, focus, navReset };
  }

  return { render };
})();
