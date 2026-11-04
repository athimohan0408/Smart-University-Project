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
  function render(container, map) {
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

    return { showRoute, clear, highlight };
  }

  return { render };
})();
