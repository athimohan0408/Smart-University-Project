// Wake on approach: notice that someone is walking up to the kiosk, so it can greet them without being touched.
"use strict";

// How it decides someone is there, best signal first:
//   1. a face of a useful size, when the browser has the (experimental) FaceDetector API: the bigger the face in the
//      picture, the closer the person, so a passer-by far down the corridor does not wake the kiosk;
//   2. otherwise movement: the share of the picture that changed between two samples.
// Either must last `hold_ms` to count (a flicker or a door swinging is ignored). Pictures are looked at in this
// page, on a 80x60 grey canvas, and thrown away: nothing is recorded, stored or sent anywhere.
//
// Without a webcam, or if the browser refuses it, touch, mouse and keyboard still count as "someone is here"
// (poke()), so the screen wakes and the sleep timer works either way.
const Wake = (() => {
  const W = 80, H = 60;
  let cfg = null, handlers = {}, stream = null, video = null, ctx = null, detector = null, timer = 0;
  let prev = null, seenSince = null, lastSeen = 0, present = false, asleep = false, started = 0, mode = "off";

  // Share (0-1) of pixels whose brightness differs by more than `delta` (of 255) between two grey frames.
  function motionShare(a, b, delta = 24) {
    if (!a || !b || a.length !== b.length || !a.length) return 0;
    let changed = 0;
    for (let i = 0; i < a.length; i++) if (Math.abs(a[i] - b[i]) > delta) changed++;
    return changed / a.length;
  }

  function grey(rgba) {
    const out = new Uint8Array(rgba.length / 4);
    for (let i = 0, j = 0; i < rgba.length; i += 4, j++) out[j] = (rgba[i] * 299 + rgba[i + 1] * 587 + rgba[i + 2] * 114) / 1000;
    return out;
  }

  // The state machine, fed one observation at a time (`seen`: is someone there right now). Exported for testing.
  function observe(seen, now, c = cfg) {
    if (seen) {
      lastSeen = now;
      if (seenSince === null) seenSince = now;
      if (!present && now - seenSince >= c.hold_ms) {
        present = true;
        if (asleep) { asleep = false; handlers.wake?.(); }
        handlers.approach?.();
      }
    } else {
      seenSince = null;
      if (present && now - lastSeen >= c.absent_s * 1000) { present = false; handlers.leave?.(); }
    }
    if (!asleep && c.sleep_after_s > 0 && !present && now - Math.max(lastSeen, started) >= c.sleep_after_s * 1000) {
      asleep = true;
      handlers.sleep?.();
    }
  }

  async function sample() {
    const now = performance.now();
    let seen = false;
    try {
      ctx.drawImage(video, 0, 0, W, H);
      const frame = grey(ctx.getImageData(0, 0, W, H).data);
      if (detector && video.videoWidth) {
        const faces = await detector.detect(video);
        const widest = Math.max(0, ...faces.map((f) => f.boundingBox.width)) / video.videoWidth;
        seen = widest >= cfg.min_face_ratio;
      } else {
        seen = motionShare(prev, frame) >= cfg.motion_threshold;
      }
      prev = frame;
    } catch { /* a dropped frame: not seen */ }
    observe(seen, now);
  }

  // Someone touched, moved the mouse or pressed a key: certainly someone is there.
  function poke() {
    const now = performance.now();
    lastSeen = now;
    if (asleep) { asleep = false; handlers.wake?.(); }
  }

  async function start(config, h) {
    cfg = config; handlers = h || {}; started = performance.now(); lastSeen = started;
    if (!cfg.enabled) return "off";
    // the sleep timer runs with or without a camera
    timer = setInterval(() => { if (mode !== "camera") observe(false, performance.now()); }, 1000);
    mode = "touch";
    if (!cfg.camera || !navigator.mediaDevices?.getUserMedia) return mode;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: { width: 160, height: 120, facingMode: "user" }, audio: false });
    } catch (err) {
      handlers.status?.("touch", `Camera not available (${err.name}); touch and keyboard still wake the kiosk.`);
      return mode;
    }
    video = document.createElement("video");
    video.muted = true; video.playsInline = true; video.srcObject = stream;
    await video.play().catch(() => {});
    const canvas = document.createElement("canvas");
    canvas.width = W; canvas.height = H;
    ctx = canvas.getContext("2d", { willReadFrequently: true });
    if ("FaceDetector" in window) {
      try { detector = new window.FaceDetector({ fastMode: true, maxDetectedFaces: 2 }); } catch { detector = null; }
    }
    clearInterval(timer);
    timer = setInterval(sample, cfg.sample_ms);
    mode = "camera";
    handlers.status?.("camera", detector ? "Watching for visitors by their face. Nothing is recorded or sent."
                                          : "Watching for movement. Nothing is recorded or sent.");
    return mode;
  }

  function stop() {
    clearInterval(timer);
    stream?.getTracks().forEach((t) => t.stop());
    stream = null; mode = "off";
  }

  return { start, stop, poke, observe, motionShare, grey, get mode() { return mode; },
           get present() { return present; }, get asleep() { return asleep; },
           _reset(c, h = {}) { cfg = c; present = false; asleep = false; seenSince = null; lastSeen = 0; started = 0; handlers = h; } };
})();

if (typeof module !== "undefined") module.exports = Wake;  // node: the logic is tested without a browser
