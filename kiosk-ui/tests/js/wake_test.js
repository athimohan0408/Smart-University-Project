// The wake-on-approach logic (static/wake.js) without a browser: motion measurement and the approach / leave /
// sleep state machine. Run by tests/test_origin_and_position.py, or: node tests/js/wake_test.js
"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const Wake = require(path.join(__dirname, "..", "..", "src", "kiosk_ui", "static", "wake.js"));

const cfg = { hold_ms: 800, absent_s: 6, sleep_after_s: 120, motion_threshold: 0.04, min_face_ratio: 0.14, sample_ms: 250 };
const log = [];
const handlers = {
  approach: () => log.push("approach"), leave: () => log.push("leave"),
  sleep: () => log.push("sleep"), wake: () => log.push("wake"),
};
const fresh = () => { log.length = 0; Wake._reset(cfg, handlers); };

// ---- motionShare: the share of pixels that changed
const still = new Uint8Array(100).fill(100);
const half = new Uint8Array(100).fill(100); for (let i = 0; i < 50; i++) half[i] = 200;
assert.equal(Wake.motionShare(still, still), 0);
assert.equal(Wake.motionShare(still, half), 0.5);
assert.equal(Wake.motionShare(still, new Uint8Array(10)), 0, "frames of different size: no motion, no crash");
assert.equal(Wake.motionShare(null, still), 0);
const slightly = new Uint8Array(100).fill(110);  // a light flicker, under the threshold of 24
assert.equal(Wake.motionShare(still, slightly), 0);

// ---- grey: RGBA to brightness
assert.deepEqual([...Wake.grey(new Uint8ClampedArray([255, 255, 255, 255, 0, 0, 0, 255]))], [255, 0]);

// ---- someone must stay for hold_ms before it counts as an approach
fresh();
Wake.observe(true, 1000);
Wake.observe(true, 1500);
assert.deepEqual(log, [], "0.5 s of movement is a flicker, not an approach");
Wake.observe(true, 1800);
assert.deepEqual(log, ["approach"]);
Wake.observe(true, 2300);
Wake.observe(true, 9000);
assert.deepEqual(log, ["approach"], "staying put is the same approach, not a new one");

// ---- a flicker that stops resets the clock
fresh();
Wake.observe(true, 1000);
Wake.observe(false, 1200);
Wake.observe(true, 1500);
Wake.observe(true, 2000);
assert.deepEqual(log, [], "the interrupted movement started again from 1500");
Wake.observe(true, 2300);
assert.deepEqual(log, ["approach"]);

// ---- leaving takes absent_s; coming back is a NEW approach
fresh();
Wake.observe(true, 0); Wake.observe(true, 900);
assert.deepEqual(log, ["approach"]);
Wake.observe(false, 3000);
assert.deepEqual(log, ["approach"], "gone for 2 s: probably still there");
Wake.observe(false, 7000);
assert.deepEqual(log, ["approach", "leave"]);
Wake.observe(true, 20000); Wake.observe(true, 21000);
assert.deepEqual(log, ["approach", "leave", "approach"]);

// ---- the screen sleeps after sleep_after_s with nobody, and wakes (then greets) when someone arrives
fresh();
Wake.observe(false, 60 * 1000);
assert.deepEqual(log, [], "a minute of nobody: not yet");
Wake.observe(false, 121 * 1000);
assert.deepEqual(log, ["sleep"]);
Wake.observe(false, 200 * 1000);
assert.deepEqual(log, ["sleep"], "it only falls asleep once");
Wake.observe(true, 300 * 1000); Wake.observe(true, 301 * 1000);
assert.deepEqual(log, ["sleep", "wake", "approach"], "wake first (brighten), then greet");

// ---- touching the screen wakes it without a camera
fresh();
Wake.observe(false, 121 * 1000);
assert.deepEqual(log, ["sleep"]);
Wake.poke();
assert.deepEqual(log, ["sleep", "wake"]);

// ---- sleep_after_s: 0 means never
fresh();
Wake.observe(false, 9999 * 1000, { ...cfg, sleep_after_s: 0 });
assert.deepEqual(log, []);

console.log("wake logic: all checks passed");
