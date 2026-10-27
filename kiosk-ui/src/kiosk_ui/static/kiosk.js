// Kiosk screen: conversation, voice input, answer audio, map + directions, receptionist hand-off.
"use strict";

const $ = (id) => document.getElementById(id);
const INTENT_LABELS = {
  find_person: "Finding a faculty member", person_availability: "Consultation hours",
  find_by_subject: "Who teaches a subject", find_room: "Finding a room", schedule_query: "Timetable",
  programme_info: "Programme", event_info: "Event", facility_info: "Facility", contact_info: "Contact details",
  escalate_to_human: "Receptionist", out_of_scope: "Other question",
};

const state = { config: null, map: null, mapView: null, session: null, busy: false, idleTimer: null,
  audio: null, panoramas: [], panoIndex: 0, viewer: null, recorder: null, talked: false, voiceInput: null,
  greeting: null, speaking: null };

// End-of-speech detection, the same idea and numbers as voice-memory's own kiosk microphone (audio: in its settings).
const VAD = { silence_rms: 0.012, end_silence_s: 0.8, start_timeout_s: 8, max_utterance_s: 12 };
const NO_VOICE = "Voice input isn't switched on at this kiosk yet. Please type your question.";
const MIC_BLOCKED = "I can't use the microphone: the browser has blocked it. Allow the microphone for this page "
  + "(the icon in the address bar), or type your question.";

function newSessionId() {
  const rand = Math.random().toString(36).slice(2, 8);
  return `${state.config?.kiosk_id || "kiosk"}-${Date.now().toString(36)}-${rand}`;
}

async function api(path, body) {
  const opts = body instanceof FormData ? { method: "POST", body }
    : body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

// ---------- the robot: moods, captions, waving, eyes that follow the visitor ----------
const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const Robot = {
  el: () => $("robot"),
  mood(mode) { this.el().dataset.mode = mode; },
  wave() {
    const el = this.el();
    el.classList.remove("waving");
    void el.offsetWidth; // restart the animation
    el.classList.add("waving");
    clearTimeout(this._wave);
    this._wave = setTimeout(() => el.classList.remove("waving"), 2300);
  },
  hop() {
    const el = this.el();
    el.classList.remove("happy");
    void el.offsetWidth;
    el.classList.add("happy");
    setTimeout(() => el.classList.remove("happy"), 600);
  },
  caption(title, line) {
    if (title !== undefined) $("robot-title").textContent = title;
    if (line !== undefined) $("robot-line").textContent = line;
  },
  mouth(level) { this.el().style.setProperty("--mouth", level.toFixed(2)); },
  look(x, y) { // -1..1 each way
    if (this.el().dataset.mode === "thinking") return;
    this.el().querySelector(".r-gaze").style.transform = `translate(${(x * 7).toFixed(1)}px, ${(y * 5).toFixed(1)}px)`;
  },
};

function followPointer(e) {
  const r = Robot.el().getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height * 0.35;
  const dx = (e.clientX - cx) / Math.max(innerWidth / 2, 1), dy = (e.clientY - cy) / Math.max(innerHeight / 2, 1);
  Robot.look(Math.max(-1, Math.min(1, dx)), Math.max(-1, Math.min(1, dy)));
}

function startConversation() {
  $("welcome").hidden = true;
}

// ---------- two screens: the welcome (big robot + spoken greeting), then the chat ----------
const onIntro = () => !$("intro").hidden;

function moveRobot(slot) {
  const robot = Robot.el();
  if (robot.parentElement !== $(slot)) $(slot).append(robot);
}

// The welcome screen. withVoice: speak the greeting now (otherwise it waits for a touch: an attract screen).
function showIntro({ withVoice = true } = {}) {
  state.introToken = (state.introToken || 0) + 1;
  $("chat").hidden = true;
  $("chat").classList.remove("arriving");
  const intro = $("intro");
  intro.hidden = false;
  intro.classList.remove("leaving");
  intro.classList.add("arriving");
  moveRobot("intro-slot");
  const robot = Robot.el();
  robot.classList.remove("entering");
  void robot.offsetWidth;
  robot.classList.add("entering");
  setTimeout(() => robot.classList.remove("entering"), 1000);
  Robot.mood("idle");
  greetThenChat(withVoice);
}

async function greetThenChat(withVoice) {
  const token = state.introToken;
  const later = (ms) => setTimeout(() => { if (token === state.introToken && onIntro()) enterChat(); }, ms);
  if (!state.config?.speak) { greet({ withVoice: false }); return; } // no voice at this kiosk: the buttons lead on
  if (!withVoice) { greet({ withVoice: false }); showTapToStart(); return; }
  $("tap-start").hidden = true;
  const spoken = await greet();
  // after the greeting, on to the chat (unless the visitor already moved on); if nothing could be spoken, the
  // greeting is on screen as text: give them time to read it
  if (token !== state.introToken || !onIntro()) return;
  later(spoken ? 900 : 6000);
}

// From the welcome screen to the chat. mic: start listening straight away ("Speak to me").
function enterChat({ mic = false, focus = false } = {}) {
  if (!onIntro()) return;
  state.introToken = (state.introToken || 0) + 1;
  stopSpeaking();
  $("tap-start").hidden = true;
  const intro = $("intro");
  intro.classList.remove("arriving");
  intro.classList.add("leaving");
  setTimeout(() => {
    intro.hidden = true;
    intro.classList.remove("leaving");
    moveRobot("stage-slot");
    Robot.mood("idle");
    Robot.caption("How can I help you?", "Ask me where a faculty member sits, when you can meet them, "
      + "who teaches a subject, or how to reach a room.");
    const chat = $("chat");
    chat.hidden = false;
    chat.classList.add("arriving");
    touch();
    if (mic) toggleMic();
    else if (focus) $("question").focus();
  }, REDUCED_MOTION ? 0 : 520);
}

// ---------- conversation ----------
function bubble(cls, text) {
  const div = document.createElement("div");
  div.className = `bubble ${cls}`;
  if (text) div.textContent = text;
  $("thread").append(div);
  div.scrollIntoView({ behavior: "smooth", block: "end" });
  return div;
}

// Buttons under an earlier answer belong to that question: once the visitor moves on they stop working.
function spendOptions() {
  for (const box of document.querySelectorAll(".bubble .options:not(.spent)")) {
    box.classList.add("spent");
    for (const b of box.querySelectorAll("button")) b.disabled = true;
  }
}

function setBusy(busy) {
  state.busy = busy;
  for (const b of document.querySelectorAll("#ask button, .bubble .options:not(.spent) button, .chip")) b.disabled = busy;
}

async function ask(text, { chosen = false } = {}) {
  text = text.trim();
  if (!text || state.busy) return;
  touch();
  startConversation();
  spendOptions();
  bubble("visitor", text);
  const thinking = bubble("bot thinking", "Let me check…");
  stopSpeaking();
  Robot.mood("thinking");
  Robot.caption("Let me check that for you…", "");
  setBusy(true);
  try {
    showResult(await api("/api/turn", { session_id: state.session, text, chosen_option: chosen }), thinking);
  } catch (err) {
    thinking.className = "bubble bot escalate";
    thinking.textContent = "Sorry, I couldn't reach the help desk service. Please speak to the receptionist at the desk.";
    Robot.mood("sorry");
    Robot.caption("Sorry, I'm having trouble right now.", "Please speak to the receptionist at the desk.");
    setStatus(false);
  } finally {
    setBusy(false);
  }
}

function showResult(result, node) {
  const t = result.turn;
  state.talked = true;
  node.className = `bubble bot${t.escalate ? " escalate" : ""}`;
  node.textContent = t.answer;
  if (t.transcript && t.question && t.question.toLowerCase() !== t.transcript.toLowerCase()) {
    const u = document.createElement("span");
    u.className = "understood";
    u.textContent = `I understood: “${t.question}”`;
    node.append(u);
  }
  if (t.options?.length) {
    const box = document.createElement("div");
    box.className = "options";
    for (const opt of t.options) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "secondary";
      b.textContent = opt;
      b.addEventListener("click", () => ask(opt, { chosen: true }));
      box.append(b);
    }
    node.append(box);
  }
  if (t.ticket) {
    const h = document.createElement("div");
    h.className = "handoff";
    h.textContent = "The receptionist has been told and will help you shortly.";
    node.append(h);
  }
  node.scrollIntoView({ behavior: "smooth", block: "end" });
  setStatus(t.outcome !== "error");
  const after = t.escalate ? "sorry" : "idle";
  if (t.escalate) Robot.caption("Let me get you some help.", "The receptionist at the desk can help with this.");
  else if (t.outcome === "clarification") Robot.caption("Which one did you mean?", "Tap a name below, or say it.");
  else if (t.outcome === "answered") { Robot.caption("Here's what I found.", ""); Robot.hop(); }
  else Robot.caption("Happy to help!", "");
  playAudio(t.audio_wav_base64, t.speech || t.answer, after);
  showDetails(t, result.routes || [], result.off_map_rooms || []);
}

// ---------- details panel: map, directions, facts ----------
function showDetails(turn, routes, offMap) {
  const steps = $("steps");
  steps.replaceChildren();
  const route = routes[0];
  if (route) {
    state.mapView.showRoute(route);
    $("details-title").textContent = `Route to room ${route.label} · about ${Math.round(route.length_m)} m`;
    for (const s of route.steps) {
      const li = document.createElement("li");
      li.textContent = s;
      steps.append(li);
    }
    setPanoramas(route.panoramas || [], `Room ${route.label}`);
  } else {
    state.mapView.clear();
    $("details-title").textContent = "Floor map";
    if (offMap.length) {
      const li = document.createElement("li");
      li.textContent = "That room is not on this floor. Please see the answer for where it is, or ask the receptionist.";
      steps.append(li);
    }
    setPanoramas([], "");
  }

  const facts = $("facts");
  facts.replaceChildren();
  const add = (k, v) => {
    if (!v) return;
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    facts.append(dt, dd);
  };
  add("Topic", INTENT_LABELS[turn.intent]);
  const named = (type) => (turn.entities || []).filter((e) => e.type === type && e.canonical_id).map((e) => e.text);
  add("Person", named("faculty").join(", "));
  add("Room", (turn.room_ids || []).map((r) => r.split(":")[1].toUpperCase()).join(", "));
  add("Subject", named("course").join(", "));
  add("Day", named("day").join(", "));
  if (turn.outcome === "answered") add("Source", "Verified department data");
  else if (turn.escalate) add("Status", "Please check with the receptionist");
  $("facts-panel").hidden = !facts.children.length;
}

// ---------- 360-degree photos ----------
function setPanoramas(list, title) {
  state.panoramas = list;
  state.panoIndex = 0;
  $("pano-btn").hidden = !list.length;
  $("pano-title").textContent = title ? `360° view · ${title}` : "360° view";
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src; s.onload = resolve; s.onerror = reject;
    document.head.append(s);
  });
}

async function openPanorama() {
  touch();
  $("pano-overlay").hidden = false;
  if (!window.pannellum) {
    // Pannellum: a small open-source 360 viewer. Use the vendored copy when present (offline kiosk).
    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = "https://cdn.jsdelivr.net/npm/pannellum@2.5.6/build/pannellum.css";
    document.head.append(css);
    try { await loadScript("/static/vendor/pannellum.js"); }
    catch { await loadScript("https://cdn.jsdelivr.net/npm/pannellum@2.5.6/build/pannellum.js"); }
  }
  showPanorama();
}

function showPanorama() {
  const file = state.panoramas[state.panoIndex];
  $("pano-count").textContent = `${state.panoIndex + 1} / ${state.panoramas.length}`;
  state.viewer?.destroy();
  state.viewer = window.pannellum.viewer("pano", {
    type: "equirectangular", panorama: `/panoramas/${encodeURIComponent(file)}`, autoLoad: true, showControls: true,
  });
}

function stepPanorama(delta) {
  if (!state.panoramas.length) return;
  state.panoIndex = (state.panoIndex + delta + state.panoramas.length) % state.panoramas.length;
  showPanorama();
}

function closePanorama() {
  $("pano-overlay").hidden = true;
  state.viewer?.destroy();
  state.viewer = null;
}

// ---------- audio out ----------
// One AudioContext for everything the robot says; an analyser on it drives the mouth from the voice's loudness.
const voiceOut = { ctx: null, analyser: null, buf: null, raf: 0 };

function audioCtx() {
  if (!voiceOut.ctx) {
    voiceOut.ctx = new AudioContext();
    voiceOut.analyser = voiceOut.ctx.createAnalyser();
    voiceOut.analyser.fftSize = 1024;
    voiceOut.buf = new Float32Array(voiceOut.analyser.fftSize);
    voiceOut.analyser.connect(voiceOut.ctx.destination);
  }
  return voiceOut.ctx;
}

// Browsers only allow sound after the visitor has touched the page (unless the kiosk browser is started with
// --autoplay-policy=no-user-gesture-required). resume() never settles while blocked, hence the time limit.
async function soundAllowed() {
  const ctx = audioCtx();
  if (ctx.state !== "running") await Promise.race([ctx.resume().catch(() => {}), new Promise((r) => setTimeout(r, 400))]);
  return ctx.state === "running";
}

function lipSync(on) {
  cancelAnimationFrame(voiceOut.raf);
  if (!on) { Robot.mouth(1); return; }
  const step = () => {
    voiceOut.analyser.getFloatTimeDomainData(voiceOut.buf);
    let sum = 0;
    for (let i = 0; i < voiceOut.buf.length; i++) sum += voiceOut.buf[i] * voiceOut.buf[i];
    Robot.mouth(1 + Math.min(2.6, Math.sqrt(sum / voiceOut.buf.length) * 11));
    voiceOut.raf = requestAnimationFrame(step);
  };
  step();
}

function stopSpeaking() {
  state.speaking?.cancel();
  state.speaking = null;
}

// Say something: a WAV (voice-memory's Piper voice) with lip-sync, or the browser's own voice.
// onWord(fraction 0..1) lets a caption follow along. Resolves when finished (or stopped).
function speak({ src, text, onWord }) {
  stopSpeaking();
  if (!state.config?.speak || (!src && !text)) return Promise.resolve(false);
  const robot = Robot.el();
  return new Promise((resolve) => {
    let done = false, raf = 0;
    const finish = (ok) => {
      if (done) return;
      done = true;
      cancelAnimationFrame(raf);
      lipSync(false);
      robot.classList.remove("no-lipsync");
      if (robot.dataset.mode === "speaking") Robot.mood("idle");
      onWord?.(1);
      resolve(ok);
    };
    if (src) {
      const audio = new Audio(src);
      const ctx = audioCtx();
      ctx.createMediaElementSource(audio).connect(voiceOut.analyser);
      state.speaking = { cancel: () => { audio.pause(); finish(false); } };
      audio.onended = () => finish(true);
      audio.onerror = () => finish(false);
      Robot.mood("speaking");
      lipSync(true);
      if (onWord) {
        const follow = () => {
          if (audio.duration) onWord(Math.min(1, audio.currentTime / audio.duration));
          raf = requestAnimationFrame(follow);
        };
        follow();
      }
      audio.play().catch(() => finish(false));
      return;
    }
    if (!window.speechSynthesis) { finish(false); return; }
    const u = new SpeechSynthesisUtterance(text);
    const voices = speechSynthesis.getVoices();
    u.voice = voices.find((v) => v.lang === "en-IN") || voices.find((v) => v.lang.startsWith("en")) || null;
    u.rate = 0.95;
    u.onboundary = (e) => onWord?.(Math.min(1, (e.charIndex + (e.charLength || 0)) / text.length));
    u.onend = () => finish(true);
    u.onerror = () => finish(false);
    state.speaking = { cancel: () => { speechSynthesis.cancel(); finish(false); } };
    robot.classList.add("no-lipsync");
    Robot.mood("speaking");
    speechSynthesis.speak(u);
  });
}

// An answer from voice-memory: its WAV when it sent one, otherwise the browser reads the text.
function playAudio(b64, text, after = "idle") {
  const src = b64 ? `data:audio/wav;base64,${b64}` : null;
  speak({ src, text }).then(() => { if (Robot.el().dataset.mode === "idle") Robot.mood(after); });
  if (!state.config?.speak) Robot.mood(after);
}

// ---------- greeting ----------
async function greet({ withVoice = true } = {}) {
  const g = state.greeting;
  if (!g) return;
  Robot.wave();
  const words = g.text.split(/\s+/);
  const line = $("intro-line");
  const show = (fraction) => { // the caption appears as it is spoken
    const n = Math.max(1, Math.round(words.length * fraction));
    line.textContent = words.slice(0, n).join(" ");
  };
  if (!withVoice || REDUCED_MOTION) show(1);
  else show(0.02);
  if (!withVoice) return false;
  const ok = await speak({ src: g.audio_url, text: g.text, onWord: show });
  if (!ok) show(1);
  return ok;
}

function showTapToStart() {
  const btn = $("tap-start");
  btn.hidden = false;
  const token = state.introToken;
  const go = (e) => {
    document.removeEventListener("pointerdown", go, true);
    btn.hidden = true;
    audioCtx().resume().catch(() => {});
    if (token !== state.introToken || !onIntro()) return;
    if (e.target.closest(".intro-actions")) return; // "Ask me a question" / "Speak to me" go straight to the chat
    if (e.target.closest("#robot")) return; // the robot's own click handler greets
    greetThenChat(true);
  };
  document.addEventListener("pointerdown", go, true);
}

// ---------- voice in: record 16-bit PCM WAV for voice-memory's /turn/audio ----------
async function toggleMic() {
  touch();
  if (state.recorder) return stopMic(true);
  if (state.busy) return;
  if (state.voiceInput === false) {
    startConversation();
    bubble("bot escalate", NO_VOICE);
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    startConversation();
    bubble("bot escalate", "This browser can't record here (it needs localhost or HTTPS). Please type your question.");
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
  } catch (err) {
    startConversation();
    bubble("bot escalate", err?.name === "NotFoundError"
      ? "I can't find a microphone on this kiosk. Please type your question."
      : MIC_BLOCKED);
    return;
  }
  stopSpeaking();
  Robot.mood("listening");
  Robot.caption("I'm listening…", "Speak now. I'll answer when you stop.");
  const ctx = new AudioContext();
  const source = ctx.createMediaStreamSource(stream);
  const proc = ctx.createScriptProcessor(4096, 1, 1);
  const chunks = [];
  // Hands-free: send by itself once the visitor has spoken and then been quiet for end_silence_s.
  let heard = 0, quiet = 0;
  proc.onaudioprocess = (e) => {
    const data = new Float32Array(e.inputBuffer.getChannelData(0));
    chunks.push(data);
    const secs = data.length / ctx.sampleRate;
    let sum = 0;
    for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
    if (Math.sqrt(sum / data.length) >= VAD.silence_rms) { heard += secs; quiet = 0; } else if (heard) quiet += secs;
    const total = chunks.length * secs;
    if (heard >= 0.2 && quiet >= VAD.end_silence_s) stopMic(true);
    else if (!heard && total >= VAD.start_timeout_s) stopMic(false, "I didn't hear anything. Tap the microphone and speak, or type your question.");
    else if (total >= VAD.max_utterance_s) stopMic(true);
  };
  source.connect(proc);
  proc.connect(ctx.destination);
  state.recorder = { stream, ctx, proc, source, chunks };
  $("mic").classList.add("recording");
  $("mic").setAttribute("aria-label", "Stop and send");
  $("question").placeholder = "Listening… just speak, I'll answer when you stop";
}

async function stopMic(send, notice) {
  const r = state.recorder;
  if (!r) return;
  state.recorder = null;
  r.proc.onaudioprocess = null;
  r.proc.disconnect(); r.source.disconnect();
  r.stream.getTracks().forEach((t) => t.stop());
  const rate = r.ctx.sampleRate;
  await r.ctx.close();
  $("mic").classList.remove("recording");
  $("mic").setAttribute("aria-label", "Speak your question");
  $("question").placeholder = "Type your question, or tap the microphone";
  Robot.mood("idle");
  if (notice) { startConversation(); bubble("bot", notice); Robot.caption("I didn't catch that.", "Tap the microphone and try again."); }
  if (!send) return;
  const total = r.chunks.reduce((n, c) => n + c.length, 0);
  if (total < rate * 0.3) return; // under 0.3 s: a mis-tap
  Robot.mood("thinking");
  Robot.caption("Let me check that for you…", "");

  startConversation();
  spendOptions();
  const mine = bubble("visitor", "🎤 …");
  const thinking = bubble("bot thinking", "Let me check…");
  setBusy(true);
  try {
    const form = new FormData();
    form.append("session_id", state.session);
    form.append("audio", encodeWav(r.chunks, rate), "speech.wav");
    const result = await api("/api/turn/audio", form);
    mine.textContent = result.turn.transcript || "(I didn't catch that)";
    showResult(result, thinking);
  } catch {
    thinking.className = "bubble bot escalate";
    thinking.textContent = "Sorry, I couldn't hear that properly. Please type your question or ask the receptionist.";
    Robot.mood("sorry");
    Robot.caption("Sorry, I couldn't hear that properly.", "Please type your question or ask the receptionist.");
  } finally {
    setBusy(false);
  }
}

function encodeWav(chunks, rate) {
  const n = chunks.reduce((s, c) => s + c.length, 0);
  const buf = new ArrayBuffer(44 + n * 2);
  const v = new DataView(buf);
  const str = (o, s) => [...s].forEach((ch, i) => v.setUint8(o + i, ch.charCodeAt(0)));
  str(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); str(8, "WAVE"); str(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, "data");
  v.setUint32(40, n * 2, true);
  let o = 44;
  for (const c of chunks) for (let i = 0; i < c.length; i++, o += 2) {
    const s = Math.max(-1, Math.min(1, c[i]));
    v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

// ---------- receptionist, new visitor, idle reset ----------
async function callReceptionist() {
  touch();
  startConversation();
  const node = bubble("bot escalate", "Calling the receptionist…");
  Robot.mood("idle");
  Robot.wave();
  Robot.caption("I'm calling the receptionist for you.", "They'll be with you shortly.");
  try {
    const t = await api("/api/receptionist", { session_id: state.session });
    node.textContent = t.message || "The receptionist has been told and will be with you shortly. You can also walk over to the desk.";
  } catch {
    node.textContent = "Please walk over to the receptionist at the desk; they will help you.";
  }
  state.talked = true;
}

function newVisitor({ spoken = false } = {}) {
  const old = state.session;
  if (old && state.talked) api("/api/session/reset", { session_id: old }).catch(() => {});
  state.session = newSessionId();
  state.talked = false;
  stopMic(false);
  closePanorama();
  stopSpeaking();
  $("thread").replaceChildren();
  $("welcome").hidden = false;
  $("question").value = "";
  showDetails({}, [], []);
  showIntro({ withVoice: spoken }); // an idle reset greets silently: the visitor has walked away
}

function touch() {
  if (voiceOut.ctx?.state === "suspended") voiceOut.ctx.resume().catch(() => {}); // every touch may unlock sound
  clearTimeout(state.idleTimer);
  const s = state.config?.idle_reset_s || 90;
  state.idleTimer = setTimeout(() => { if (state.talked) newVisitor(); }, s * 1000);
}

// ---------- status + clock ----------
function setVoiceInput(available) {
  state.voiceInput = available;
  $("mic").classList.toggle("off", !available);
  $("mic").title = available ? "Tap to speak" : NO_VOICE;
}

function setStatus(ok) {
  $("status").className = `status ${ok ? "ok" : "bad"}`;
  $("status").title = ok ? "Connected" : "The receptionist service is not reachable";
  $("status").querySelector(".status-text").textContent = ok ? "Online" : "Offline";
}

async function checkHealth() {
  try {
    const h = await api("/api/health");
    const vm = typeof h.voice_memory === "object" && h.voice_memory !== null ? h.voice_memory : null;
    setStatus(!!vm);
    if (vm) setVoiceInput(vm.stt !== "TextSTT"); // voice-memory runs with typed input only
  } catch { setStatus(false); }
}

function tick() {
  const now = new Date();
  $("clock").innerHTML = "";
  const time = document.createElement("strong");
  time.textContent = now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const day = document.createElement("span");
  day.textContent = now.toLocaleDateString([], { weekday: "long", day: "numeric", month: "short" });
  $("clock").append(time, day);
}

// ---------- start ----------
async function start() {
  tick(); setInterval(tick, 15000);
  state.config = await api("/api/config");
  state.map = await api("/api/map");
  state.mapView = FloorMapView.render($("map"), state.map);
  $("map-warning").hidden = state.map.verified;
  $("map-warning").title = "Room positions are approximate until the floor plan is verified.";
  state.session = newSessionId();

  $("ask").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("question").value;
    $("question").value = "";
    ask(q);
  });
  $("question").addEventListener("input", touch);
  for (const chip of document.querySelectorAll(".chip")) chip.addEventListener("click", () => ask(chip.textContent));
  $("mic").addEventListener("click", toggleMic);
  $("human").addEventListener("click", callReceptionist);
  $("newvisitor").addEventListener("click", () => newVisitor({ spoken: true }));
  $("robot").addEventListener("click", () => {
    if (onIntro()) { $("tap-start").hidden = true; audioCtx().resume().catch(() => {}); greetThenChat(true); return; }
    touch();
    if (state.busy || state.recorder) return;
    Robot.wave(); Robot.hop();
  });
  $("intro-ask").addEventListener("click", () => enterChat({ focus: true }));
  $("intro-mic").addEventListener("click", () => enterChat({ mic: true }));
  document.addEventListener("pointermove", followPointer);
  document.addEventListener("pointerdown", followPointer);
  setInterval(() => { // nobody here: wave now and then to invite people over
    if ((onIntro() || !state.talked) && !state.busy && !state.recorder && !state.speaking && document.visibilityState === "visible") Robot.wave();
  }, 25000);
  $("pano-btn").addEventListener("click", openPanorama);
  $("pano-close").addEventListener("click", closePanorama);
  $("pano-prev").addEventListener("click", () => stepPanorama(-1));
  $("pano-next").addEventListener("click", () => stepPanorama(1));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePanorama(); });
  checkHealth(); setInterval(checkHealth, 30000);

  try { state.greeting = await api("/api/greeting"); }
  catch { state.greeting = { text: "Hello, and welcome to the eighth floor of Central Block! I'm your AI receptionist. "
    + "Tap the microphone and ask me where a faculty member sits or how to find a room.", audio_url: null }; }
  showIntro({ withVoice: !!state.config.speak && await soundAllowed() });
}

start().catch(() => setStatus(false));
