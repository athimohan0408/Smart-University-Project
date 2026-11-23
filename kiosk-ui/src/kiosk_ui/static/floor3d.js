// 3D view of the 8th floor (Three.js, MIT), built from the same map data as the 2D board (GET /api/map): rooms and
// cabins are raised into walls, the route is a glowing tube, and the camera flies along it like a drone following
// the visitor. Loaded on demand by view3d.js; "three" comes from the import map in kiosk.html.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const COLORS = {
  ground: 0x13254f, corridor: 0x2a4486, room: 0xf7f4ee, cabin: 0xffffff, edge: 0x13254f, area: 0xc5a25a,
  facility: 0x8fa3d6, stairs: 0x6f86c4, lift: 0xc5a25a, route: 0xc5a25a, here: 0xc8102e, walker: 0x4c8dff, hi: 0xc5a25a,
};
const HEIGHT = { room: 46, cabin: 34, facility: 26, stairs: 10, area: 4 };

function label(text, { size = 34, color = "#13254f", bg = "rgba(255,255,255,.92)" } = {}) {
  const pad = 14, canvas = document.createElement("canvas"), ctx = canvas.getContext("2d");
  ctx.font = `600 ${size}px "Public Sans", "Segoe UI", sans-serif`;
  const w = Math.ceil(ctx.measureText(text).width) + pad * 2, h = size + pad * 1.4;
  canvas.width = w; canvas.height = h;
  ctx.font = `600 ${size}px "Public Sans", "Segoe UI", sans-serif`;
  ctx.fillStyle = bg;
  ctx.beginPath(); ctx.roundRect(0, 0, w, h, 10); ctx.fill();
  ctx.fillStyle = color; ctx.textBaseline = "middle"; ctx.textAlign = "center";
  ctx.fillText(text, w / 2, h / 2 + 1);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, transparent: true }));
  sprite.scale.set(w * 0.42, h * 0.42, 1);
  sprite.renderOrder = 10;
  return sprite;
}

function kindOf(p) {
  if (p.kind === "room") return p.rect[2] < 90 ? "cabin" : "room";
  if (p.kind === "stairs") return "stairs";
  if (p.kind === "area") return "area";
  if (p.kind === "hall") return null;
  return "facility";
}

export function create(container, map, { onPlace } = {}) {
  const [vx, vy, vw, vh] = map.viewbox;
  const cx = vx + vw / 2, cz = vy + vh / 2;
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  container.replaceChildren(renderer.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x182f6b);
  scene.fog = new THREE.Fog(0x182f6b, 1800, 3200);
  const camera = new THREE.PerspectiveCamera(42, 1, 5, 6000);
  const home = { pos: new THREE.Vector3(cx, 760, cz + 640), target: new THREE.Vector3(cx, 0, cz) };
  camera.position.copy(home.pos);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.copy(home.target);
  controls.enableDamping = true;
  controls.maxPolarAngle = Math.PI * 0.46;
  controls.minDistance = 150;
  controls.maxDistance = 2600;
  controls.autoRotate = !matchMedia("(prefers-reduced-motion: reduce)").matches;
  controls.autoRotateSpeed = 0.35;

  scene.add(new THREE.HemisphereLight(0xffffff, 0x223366, 1.6));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6);
  sun.position.set(cx - 600, 1400, cz - 400);
  sun.castShadow = true;
  const small = Math.min(innerWidth, innerHeight) < 700;  // a phone: lighter shadows
  sun.shadow.mapSize.set(small ? 1024 : 2048, small ? 1024 : 2048);
  Object.assign(sun.shadow.camera, { left: -900, right: 900, top: 700, bottom: -700, near: 100, far: 3500 });
  sun.target.position.set(cx, 0, cz);
  scene.add(sun, sun.target);

  // ground and walkways
  const ground = new THREE.Mesh(new THREE.BoxGeometry(vw + 60, 8, vh + 60), new THREE.MeshStandardMaterial({ color: COLORS.ground }));
  ground.position.set(cx, -4, cz);
  ground.receiveShadow = true;
  scene.add(ground);
  const corridorMat = new THREE.MeshStandardMaterial({ color: COLORS.corridor });
  for (const [x, y, w, h] of map.corridor) {
    const m = new THREE.Mesh(new THREE.BoxGeometry(w, 1.5, h), corridorMat);
    m.position.set(x + w / 2, 0.8, y + h / 2);
    m.receiveShadow = true;
    scene.add(m);
  }

  // places
  const meshes = new Map(), pickable = [];
  for (const p of map.places) {
    const kind = kindOf(p);
    if (!kind) continue;
    const [x, y, w, h] = p.rect;
    const lift = /lift/i.test(p.id);
    const height = HEIGHT[kind];
    const color = lift ? COLORS.lift : COLORS[kind];
    let mesh;
    if (kind === "area") {  // the staff room: a gold outline on the floor, cabins inside
      const geo = new THREE.EdgesGeometry(new THREE.BoxGeometry(w, height, h));
      mesh = new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ color: COLORS.area }));
    } else {
      const mat = new THREE.MeshStandardMaterial({ color, transparent: kind !== "stairs", opacity: kind === "facility" ? 0.75 : 0.92,
        roughness: 0.7 });
      mesh = new THREE.Mesh(new THREE.BoxGeometry(w - 4, height, h - 4), mat);
      mesh.castShadow = mesh.receiveShadow = true;
      const edges = new THREE.LineSegments(new THREE.EdgesGeometry(mesh.geometry),
        new THREE.LineBasicMaterial({ color: COLORS.edge, transparent: true, opacity: 0.45 }));
      mesh.add(edges);
      mesh.userData = { place: p, color };
      pickable.push(mesh);
    }
    mesh.position.set(x + w / 2, height / 2 + 1, y + h / 2);
    scene.add(mesh);
    meshes.set(p.id.toLowerCase(), mesh);
    if (kind !== "area" && kind !== "stairs") {
      const text = p.kind === "room" ? p.label.split("·")[0].trim() : p.label;
      const tag = label(text, kind === "cabin" ? { size: 26 } : {});
      tag.position.set(x + w / 2, height + 22, y + h / 2);
      scene.add(tag);
    }
  }

  // you are here: a red pin
  const pin = new THREE.Group();
  const head = new THREE.Mesh(new THREE.SphereGeometry(14, 24, 16), new THREE.MeshStandardMaterial({ color: COLORS.here, emissive: 0x550000 }));
  head.position.y = 52;
  const stick = new THREE.Mesh(new THREE.CylinderGeometry(3, 3, 44), new THREE.MeshStandardMaterial({ color: 0xffffff }));
  stick.position.y = 24;
  pin.add(head, stick);
  const here = label("You are here", { size: 28, color: "#ffffff", bg: "rgba(200,16,46,.95)" });
  here.position.y = 92;
  pin.add(here);
  pin.position.set(map.start_point[0], 0, map.start_point[1]);
  scene.add(pin);

  // the route
  const routeGroup = new THREE.Group();
  scene.add(routeGroup);
  const walker = new THREE.Mesh(new THREE.SphereGeometry(11, 24, 16),
    new THREE.MeshStandardMaterial({ color: COLORS.walker, emissive: 0x1d4fbf, emissiveIntensity: 0.8 }));
  walker.visible = false;
  scene.add(walker);
  let path = null, fly = null, highlighted = null, stepRoute = null, nowTube = null;
  const curveOf = (pts) => {
    const curve = new THREE.CurvePath();
    for (let i = 1; i < pts.length; i++) {
      if (pts[i - 1].distanceToSquared(pts[i]) > 0.01) curve.add(new THREE.LineCurve3(pts[i - 1], pts[i]));
    }
    return curve.curves.length ? curve : null;
  };
  const tubeOf = (curve, radius, mat) => new THREE.Mesh(
    new THREE.TubeGeometry(curve, Math.max(32, curve.curves.length * 24), radius, 10, false), mat);
  const ghostMat = new THREE.MeshStandardMaterial({ color: COLORS.route, transparent: true, opacity: 0.35 });
  const nowMat = new THREE.MeshStandardMaterial({ color: COLORS.route, emissive: COLORS.route, emissiveIntensity: 0.7 });

  function clear() {
    routeGroup.clear();
    walker.visible = false;
    path = null;
    fly = null;
    stepRoute = null;
    nowTube = null;
    camGoal = targetGoal = null;
    pin.position.set(map.start_point[0], 0, map.start_point[1]);  // back at the kiosk (or this page's start)
    if (highlighted) { highlighted.material.color.setHex(highlighted.userData.color); highlighted.material.emissive?.setHex(0); }
    highlighted = null;
    controls.autoRotate = !matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function showRoute(route, { flyThrough = true } = {}) {
    clear();
    if (!route) return;
    const pts = route.points.map(([x, z]) => new THREE.Vector3(x, 8, z));
    pin.position.set(route.points[0][0], 0, route.points[0][1]);  // "You are here" where the route starts
    const curve = new THREE.CurvePath();
    for (let i = 1; i < pts.length; i++) curve.add(new THREE.LineCurve3(pts[i - 1], pts[i]));
    path = curve;
    stepRoute = null;
    const tube = new THREE.Mesh(new THREE.TubeGeometry(curve, Math.max(64, pts.length * 24), 5, 10, false),
      new THREE.MeshStandardMaterial({ color: COLORS.route, emissive: COLORS.route, emissiveIntensity: 0.55 }));
    routeGroup.add(tube);
    const end = pts[pts.length - 1];
    const ring = new THREE.Mesh(new THREE.TorusGeometry(18, 4, 12, 40), new THREE.MeshStandardMaterial({ color: COLORS.route, emissive: COLORS.route }));
    ring.rotation.x = Math.PI / 2;
    ring.position.set(end.x, 10, end.z);
    routeGroup.add(ring);
    const dest = meshes.get(route.place_id.toLowerCase());
    if (dest?.material?.color) {
      dest.material.color.setHex(COLORS.hi);
      dest.material.emissive?.setHex(0x3a2a00);
      highlighted = dest;
    }
    controls.autoRotate = false;
    walker.visible = true;
    walker.position.copy(pts[0]);
    fly = { t0: performance.now(), ms: Math.min(14000, Math.max(5000, route.length_m * 160)), follow: flyThrough, curve };
  }

  // One step of the walkthrough (the phone page, "Show me the way"): the whole route faint, this step bright, the
  // walker walks it and the camera follows from behind; the last step settles on the destination.
  function step(route, i, ms = 3000) {
    if (stepRoute !== route) {
      clear();
      stepRoute = route;
      pin.position.set(route.points[0][0], 0, route.points[0][1]);
      const all = curveOf(route.points.map(([x, z]) => new THREE.Vector3(x, 8, z)));
      if (all) routeGroup.add(tubeOf(all, 4, ghostMat));
      const [ex, ez] = route.points[route.points.length - 1];
      const ring = new THREE.Mesh(new THREE.TorusGeometry(18, 4, 12, 40), nowMat);
      ring.rotation.x = Math.PI / 2;
      ring.position.set(ex, 10, ez);
      routeGroup.add(ring);
      const dest = meshes.get(route.place_id.toLowerCase());
      if (dest?.material?.color) {
        dest.material.color.setHex(COLORS.hi);
        dest.material.emissive?.setHex(0x3a2a00);
        highlighted = dest;
      }
      controls.autoRotate = false;
      walker.visible = true;
    }
    if (nowTube) { routeGroup.remove(nowTube); nowTube.geometry.dispose(); nowTube = null; }
    const [a, b] = route.step_spans[i];
    const curve = curveOf(route.points.slice(a, b + 1).map(([x, z]) => new THREE.Vector3(x, 9, z)));
    const last = i === route.steps.length - 1;
    if (!curve) { fly = null; return; }
    nowTube = tubeOf(curve, 6, nowMat);
    routeGroup.add(nowTube);
    curve.getPointAt(0, walker.position);
    fly = { t0: performance.now(), ms, follow: true, curve, settle: last };
  }

  function resetView() {
    clear();
    camera.position.copy(home.pos);
    controls.target.copy(home.target);
  }

  // tap a place: its card
  const ray = new THREE.Raycaster(), ndc = new THREE.Vector2();
  let down = null;
  renderer.domElement.addEventListener("pointerdown", (e) => { down = [e.clientX, e.clientY]; controls.autoRotate = false; });
  renderer.domElement.addEventListener("pointerup", (e) => {
    if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 6 || !onPlace) return;
    const r = renderer.domElement.getBoundingClientRect();
    ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ndc, camera);
    const hit = ray.intersectObjects(pickable, false)[0];
    if (hit) onPlace(hit.object.userData.place);
  });

  function resize() {
    const w = container.clientWidth || 600, h = container.clientHeight || 400;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.fov = camera.aspect < 1 ? 55 : 42;  // portrait (a phone): a wider view
    camera.updateProjectionMatrix();
  }
  const observer = new ResizeObserver(resize);
  observer.observe(container);
  resize();

  // The camera glides towards these (a drone following the walker); a drag by the visitor lets go of them.
  const a0 = new THREE.Vector3(), a1 = new THREE.Vector3(), heading = new THREE.Vector3(0, 0, -1);
  let camGoal = null, targetGoal = null;
  renderer.domElement.addEventListener("pointerdown", () => { camGoal = targetGoal = null; });
  let raf = 0;
  function frame(now) {
    raf = requestAnimationFrame(frame);
    if (fly) {
      const t = Math.min(1, (now - fly.t0) / fly.ms);
      const e = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
      fly.curve.getPointAt(e, walker.position);
      walker.position.y = 14;
      if (fly.follow) {  // behind and above the walker, facing the way it walks
        fly.curve.getPointAt(Math.max(0, e - 0.04), a0);
        fly.curve.getPointAt(Math.min(1, e + 0.04), a1);
        a1.sub(a0).setY(0);
        if (a1.lengthSq() > 1) heading.copy(a1).normalize();
        const far = Math.min(1.6, 1 / Math.min(1, camera.aspect));  // a tall phone screen: stand further back
        camGoal = walker.position.clone().addScaledVector(heading, -330 * far).setY(430 * far);
        targetGoal = walker.position.clone().addScaledVector(heading, 60);
      }
      if (t >= 1) {
        const settle = fly.settle || !stepRoute;
        fly = null;
        if (settle) {  // arrived: a view of the destination from a little above
          const end = walker.position.clone();
          camGoal = end.clone().addScaledVector(heading, -200).add(new THREE.Vector3(80, 320, 0));
          targetGoal = end;
        }
      }
    }
    if (camGoal) {
      camera.position.lerp(camGoal, 0.07);
      controls.target.lerp(targetGoal, 0.1);
      if (!fly && camera.position.distanceToSquared(camGoal) < 4) camGoal = targetGoal = null;
    }
    head.position.y = 52 + Math.sin(now / 300) * 4;
    controls.update();
    renderer.render(scene, camera);
  }
  raf = requestAnimationFrame(frame);

  function dispose() {
    cancelAnimationFrame(raf);
    observer.disconnect();
    controls.dispose();
    renderer.dispose();
    container.replaceChildren();
  }

  return { showRoute, step, clear, resetView, resize, dispose, replay: (route) => showRoute(route) };
}

export function supported() {
  try {
    const c = document.createElement("canvas");
    return !!(window.WebGL2RenderingContext && c.getContext("webgl2"));
  } catch { return false; }
}
